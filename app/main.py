"""FastAPI 入口：解析/重排分析 + 草稿持久化（SQLite）。"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .parser import parse_contract
from .references import analyze, cascade_deleted
from .terms import review_terms
from .preview import preview_deletion
from .issuance import (
    build_summary,
    has_blockers,
    snapshot_fingerprint,
)
from .audit import audit_versions
from .merge import (
    MergeConflict,
    apply_merge,
    build_apply_preview,
)
from .review import (
    build_parts,
    build_status,
    evaluate_gate,
    migrate_confirmations,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "..", "data"))
DB_PATH = os.path.join(DATA_DIR, "drafts.db")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                order_json TEXT NOT NULL DEFAULT '[]',
                deleted_json TEXT NOT NULL DEFAULT '[]',
                term_ignores_json TEXT NOT NULL DEFAULT '[]',
                revision INTEGER NOT NULL DEFAULT 1,
                review_enabled INTEGER NOT NULL DEFAULT 0,
                review_json TEXT NOT NULL DEFAULT '[]',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        # 兼容旧库：补列（SQLite 不支持 IF NOT EXISTS 加列，先查再补）
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(drafts)")}
        if "term_ignores_json" not in cols:
            conn.execute(
                "ALTER TABLE drafts ADD COLUMN term_ignores_json "
                "TEXT NOT NULL DEFAULT '[]'"
            )
        if "revision" not in cols:
            conn.execute(
                "ALTER TABLE drafts ADD COLUMN revision "
                "INTEGER NOT NULL DEFAULT 1"
            )
        if "review_enabled" not in cols:
            conn.execute(
                "ALTER TABLE drafts ADD COLUMN review_enabled "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "review_json" not in cols:
            conn.execute(
                "ALTER TABLE drafts ADD COLUMN review_json "
                "TEXT NOT NULL DEFAULT '[]'"
            )

        # 生效版本：快照四要素（正文/顺序/删除/忽略）+ 签发时的编号、改写与绑定；
        # 一经写入不可修改，后续编辑只产生新版本号，不覆盖旧版本。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                draft_id INTEGER NOT NULL REFERENCES drafts(id),
                version_no INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                order_json TEXT NOT NULL DEFAULT '[]',
                deleted_json TEXT NOT NULL DEFAULT '[]',
                term_ignores_json TEXT NOT NULL DEFAULT '[]',
                ref_result_json TEXT NOT NULL,
                term_result_json TEXT NOT NULL,
                summary_json TEXT NOT NULL,
                issued_at INTEGER NOT NULL,
                UNIQUE(draft_id, fingerprint),
                UNIQUE(draft_id, version_no)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_versions_draft "
            "ON versions(draft_id, version_no)"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="条款引用重排台", lifespan=lifespan)


# ---------------------------------------------------------------- 请求模型

class AnalyzeRequest(BaseModel):
    text: str = ""
    order: list[str] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)


class DraftIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    content: str = ""
    order: list[str] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)
    term_ignores: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- API

@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.post("/api/analyze")
def analyze_text(req: AnalyzeRequest) -> dict:
    preamble, clauses = parse_contract(req.text)
    known = {c.id for c in clauses}
    # order 仅作「同父兄弟换序」的意图输入：未列出的条款、跨父级拖动等
    # 不合法排列都交由 analyze 按父子树规整（未列出者回到解析顺序）。
    order = [cid for cid in req.order if cid in known]
    # 删除父条款会在 analyze 中级联删除整棵子树
    deleted = [cid for cid in req.deleted if cid in known]
    result = analyze(clauses, order, deleted)
    result["preamble"] = preamble
    return result


class TermsRequest(BaseModel):
    text: str = ""
    deleted: list[str] = Field(default_factory=list)


class DeletePreviewRequest(BaseModel):
    text: str = ""
    order: list[str] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)


@app.post("/api/delete-preview")
def delete_preview(req: DeletePreviewRequest) -> dict:
    """删除影响预演（只读）：勾选存活条款 → 级联集 + 删除前后绑定差异 + 原因链。"""
    preamble, clauses = parse_contract(req.text)
    known = {c.id for c in clauses}
    order = [cid for cid in req.order if cid in known]
    deleted = [cid for cid in req.deleted if cid in known]
    candidates = [cid for cid in req.candidates if cid in known]
    try:
        return preview_deletion(preamble, clauses, order, deleted, candidates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/terms")
def review(req: TermsRequest) -> dict:
    """定义术语审校：以当前删除状态（含级联删除）为准。"""
    preamble, clauses = parse_contract(req.text)
    known = {c.id for c in clauses}
    deleted = [cid for cid in req.deleted if cid in known]
    deleted_set = cascade_deleted(clauses, deleted)
    result = review_terms(preamble, clauses, deleted_set)
    result["deleted_clauses"] = [
        {"id": c.id, "label": c.label}
        for c in clauses if c.id in deleted_set
    ]
    return result


@app.get("/api/drafts")
def list_drafts() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT d.id, d.title, d.revision, d.created_at, d.updated_at, "
            "COUNT(v.id) AS version_count "
            "FROM drafts d LEFT JOIN versions v ON v.draft_id = d.id "
            "GROUP BY d.id ORDER BY d.updated_at DESC"
        ).fetchall()
    out = []
    for r in rows:
        row = dict(r)
        row["version_count"] = row.get("version_count") or 0
        out.append(row)
    return out


@app.post("/api/drafts")
def create_draft(req: DraftIn) -> dict:
    import json
    now = int(time.time())
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO drafts (title, content, order_json, deleted_json, "
            "term_ignores_json, revision, review_enabled, review_json, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, 0, '[]', ?, ?)",
            (req.title, req.content, json.dumps(req.order, ensure_ascii=False),
             json.dumps(req.deleted, ensure_ascii=False),
             json.dumps(req.term_ignores, ensure_ascii=False), now, now),
        )
        return {"id": cur.lastrowid, "revision": 1}


def _review_records(row: sqlite3.Row) -> list[dict]:
    import json
    try:
        data = json.loads(row["review_json"] or "[]")
    except (ValueError, TypeError):
        data = []
    return data if isinstance(data, list) else []


def _draft_payload(row: sqlite3.Row) -> dict:
    import json
    return {
        "id": row["id"],
        "title": row["title"],
        "content": row["content"],
        "order": json.loads(row["order_json"]),
        "deleted": json.loads(row["deleted_json"]),
        "term_ignores": json.loads(row["term_ignores_json"] or "[]"),
        "revision": row["revision"],
        "review_enabled": bool(row["review_enabled"]),
        "review": _review_records(row),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@app.get("/api/drafts/{draft_id}")
def get_draft(draft_id: int) -> dict:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM drafts WHERE id = ?", (draft_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="草稿不存在")
    return _draft_payload(row)


@app.put("/api/drafts/{draft_id}")
def update_draft(draft_id: int, req: DraftIn) -> dict:
    import json
    now = int(time.time())
    with db() as conn:
        old = conn.execute(
            "SELECT * FROM drafts WHERE id=?", (draft_id,)
        ).fetchone()
        if old is None:
            raise HTTPException(status_code=404, detail="草稿不存在")

        conn.execute(
            "UPDATE drafts SET title=?, content=?, order_json=?, "
            "deleted_json=?, term_ignores_json=?, "
            "revision=revision+1, updated_at=? WHERE id=?",
            (req.title, req.content, json.dumps(req.order, ensure_ascii=False),
             json.dumps(req.deleted, ensure_ascii=False),
             json.dumps(req.term_ignores, ensure_ascii=False), now, draft_id),
        )
        new_row = conn.execute(
            "SELECT * FROM drafts WHERE id=?", (draft_id,)
        ).fetchone()
        new_rev = new_row["revision"]

        # 逐条复核确认随保存迁移：四要素变化的相关条款确认过期，
        # 单纯编号变化（仅同父兄弟换序等）签名不变、确认保持有效。
        if old["review_enabled"]:
            _pre, _cl, _ref, _term, parts = build_parts(
                req.content, req.order, req.deleted)
            migrated = migrate_confirmations(
                _review_records(old), parts, new_rev)
            conn.execute(
                "UPDATE drafts SET review_json=? WHERE id=?",
                (json.dumps(migrated, ensure_ascii=False), draft_id),
            )
    return {"ok": True, "revision": new_rev}


@app.delete("/api/drafts/{draft_id}")
def delete_draft(draft_id: int) -> dict:
    with db() as conn:
        cur = conn.execute("DELETE FROM drafts WHERE id=?", (draft_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="草稿不存在")
    # 已签发版本保留（不可变凭证，不随草稿删除而销毁）
    return {"ok": True}


# ---------------------------------------------------------------- 逐条复核

class ReviewToggleRequest(BaseModel):
    # 开启 / 关闭草稿复核。开关本身不改变草稿修订号；开启不清空历史确认
    # （若草稿此前曾开启，仍有效的确认继续生效，已过期的须重新确认）。
    enabled: bool


class ReviewConfirmRequest(BaseModel):
    # 乐观并发：确认须对应库内当前修订号；过期提交不写入任何确认。
    revision: int
    clause_ids: list[str] = Field(default_factory=list)


def _review_status_response(row: sqlite3.Row) -> dict:
    draft = _draft_payload(row)
    _pre, _cl, _ref, _term, parts = build_parts(
        draft["content"], draft["order"], draft["deleted"])
    return build_status(draft["review_enabled"], draft["review"], parts,
                        draft["revision"])


@app.get("/api/drafts/{draft_id}/review")
def get_review(draft_id: int) -> dict:
    """逐条复核状态：逐条给出 待确认 / 有效 / 过期（已删除单列），含当前签名。"""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM drafts WHERE id=?", (draft_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="草稿不存在")
    return _review_status_response(row)


@app.post("/api/drafts/{draft_id}/review")
def set_review(draft_id: int, req: ReviewToggleRequest) -> dict:
    """开启 / 关闭逐条复核（不改变草稿修订号，不产生版本）。"""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM drafts WHERE id=?", (draft_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="草稿不存在")
        conn.execute(
            "UPDATE drafts SET review_enabled=? WHERE id=?",
            (1 if req.enabled else 0, draft_id),
        )
        saved = conn.execute(
            "SELECT * FROM drafts WHERE id=?", (draft_id,)
        ).fetchone()
    return _review_status_response(saved)


@app.post("/api/drafts/{draft_id}/review/confirm")
def confirm_review(draft_id: int, req: ReviewConfirmRequest) -> dict:
    """提交一个或多个存活条款的复核确认。

    - 仅开启复核的草稿可提交；确认须对应库内当前草稿修订号；
    - 过期提交（修订号不符）→ 409，不写入任何确认；
    - 确认在保存时固定条款正文、父级、引用目标与术语绑定四要素签名，
      之后相关条款变化则在下次保存时仅该条款确认过期。
    """
    import json
    now = int(time.time())

    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM drafts WHERE id=?", (draft_id,)
        ).fetchone()
        if row is None:
            conn.rollback()
            raise HTTPException(status_code=404, detail="草稿不存在")
        if not row["review_enabled"]:
            conn.rollback()
            raise HTTPException(status_code=400,
                                detail="该草稿未开启逐条复核")
        current_rev = row["revision"]
        if current_rev != req.revision:
            conn.rollback()
            raise HTTPException(status_code=409, detail={
                "message": "草稿已被保存为其他修订号，确认已过期，"
                           "请重新载入并复核后再提交（未写入任何确认）",
                "current_revision": current_rev,
            })

        draft = _draft_payload(row)
        _pre, _cl, _ref, _term, parts = build_parts(
            draft["content"], draft["order"], draft["deleted"])

        ids = list(dict.fromkeys(req.clause_ids))
        if not ids:
            conn.rollback()
            raise HTTPException(status_code=400, detail="未选择任何条款")
        unknown = [cid for cid in ids if cid not in parts]
        if unknown:
            conn.rollback()
            raise HTTPException(status_code=400,
                                detail=f"条款不存在或无法解析：{'、'.join(unknown)}")
        dead = [cid for cid in ids if not parts[cid]["alive"]]
        if dead:
            conn.rollback()
            raise HTTPException(status_code=400,
                                detail=f"已删除条款不能确认：{'、'.join(dead)}")

        records = {r["clause_id"]: r for r in draft["review"]}
        for cid in ids:
            records[cid] = {
                "clause_id": cid,
                "revision": current_rev,
                "confirmed_at": now,
                "sig": parts[cid]["sig"],
                "expired": False,
            }
        merged = sorted(records.values(),
                        key=lambda r: r["clause_id"])
        conn.execute(
            "UPDATE drafts SET review_json=? WHERE id=?",
            (json.dumps(merged, ensure_ascii=False), draft_id),
        )
        conn.commit()
        saved = conn.execute(
            "SELECT * FROM drafts WHERE id=?", (draft_id,)
        ).fetchone()

    return _review_status_response(saved)


# ---------------------------------------------------------------- 生效版本签发

class IssueRequest(BaseModel):
    # 乐观并发：前端持有的草稿修订号；与库内不一致则拒绝（不产生版本）
    revision: int


def _version_row_to_dict(row: sqlite3.Row, include_snapshot: bool) -> dict:
    import json
    out = {
        "id": row["id"],
        "draft_id": row["draft_id"],
        "version_no": row["version_no"],
        "revision": row["revision"],
        "title": row["title"],
        "issued_at": row["issued_at"],
        "summary": json.loads(row["summary_json"]),
    }
    if include_snapshot:
        out.update({
            "content": row["content"],
            "order": json.loads(row["order_json"]),
            "deleted": json.loads(row["deleted_json"]),
            "term_ignores": json.loads(row["term_ignores_json"] or "[]"),
            "ref_result": json.loads(row["ref_result_json"]),
            "term_result": json.loads(row["term_result_json"]),
        })
    return out


@app.get("/api/drafts/{draft_id}/versions")
def list_versions(draft_id: int) -> list[dict]:
    with db() as conn:
        # 草稿删除后其不可变版本仍保留、可读：这里不再要求草稿行存在。
        rows = conn.execute(
            "SELECT * FROM versions WHERE draft_id=? "
            "ORDER BY version_no DESC", (draft_id,)
        ).fetchall()
    return [_version_row_to_dict(r, include_snapshot=False) for r in rows]


@app.post("/api/drafts/{draft_id}/issue", status_code=201)
def issue_version(draft_id: int, req: IssueRequest) -> dict:
    """从当前草稿签发生效版本。

    一次操作内固定 正文 / 顺序 / 删除状态 / 术语忽略项，重跑引用与术语终审：
    - 存在无法解析 / 目标已删除 / 循环引用 / 未忽略术语问题 → 422 拒绝，草稿不变；
    - 校验期间草稿修订号已变化（乐观并发）→ 409 拒绝，不产生版本；
    - 相同快照重复提交 → 返回原版本（幂等，200 语义由 issued=false 表达）。
    """
    import json

    with db() as conn:
        row = conn.execute(
            "SELECT * FROM drafts WHERE id=?", (draft_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="草稿不存在")
        draft = _draft_payload(row)

        if not draft["content"].strip():
            raise HTTPException(status_code=400, detail="草稿为空，无法签发")

        # 1) 固定快照并重跑引用 + 术语终审（此处尚未写库，任何拒绝都不改动草稿）
        preamble, clauses, ref_result, term_result, parts = build_parts(
            draft["content"], draft["order"], draft["deleted"])
        ref_result["preamble"] = preamble

        ignore_set = set(draft["term_ignores"])
        blockers = {
            "reference_issues": list(ref_result["issues"]),
            "term_issues": [
                it for it in term_result["issues"]
                if it["occ_id"] not in ignore_set
            ],
        }
        if has_blockers(blockers):
            raise HTTPException(status_code=422, detail={
                "message": "存在未通过的引用或术语问题，已拒绝签发（草稿未修改）",
                "blockers": blockers,
            })

        # 1b) 开启复核的草稿：在现有引用与术语审校后复查全部存活条款的确认
        if draft["review_enabled"]:
            review_gate = evaluate_gate(draft["review"], parts)
            if not review_gate["ok"]:
                raise HTTPException(status_code=422, detail={
                    "message": "逐条复核未完成：存在待确认或已过期的存活条款，"
                               "已拒绝签发（草稿未修改）",
                    "review": {
                        "missing": review_gate["missing"],
                        "expired": review_gate["expired"],
                    },
                })

        fingerprint = snapshot_fingerprint(
            draft["content"], draft["order"], draft["deleted"],
            draft["term_ignores"],
        )

        # 2) 加事务锁后复查：并发期间草稿被改 / 已被同一快照抢先签发
        conn.execute("BEGIN IMMEDIATE")
        cur_row = conn.execute(
            "SELECT revision FROM drafts WHERE id=?", (draft_id,)
        ).fetchone()
        if cur_row is None:
            conn.rollback()
            raise HTTPException(status_code=404, detail="草稿不存在")
        current_rev = cur_row[0]
        if current_rev != req.revision or current_rev != draft["revision"]:
            conn.rollback()
            raise HTTPException(status_code=409, detail={
                "message": "草稿在校验期间已被修改（修订号变化），已拒绝签发",
                "current_revision": current_rev,
            })

        # 锁内复查复核关卡：杜绝「复核检查通过后、加锁前草稿被另一保存改动」
        if draft["review_enabled"]:
            latest_row = conn.execute(
                "SELECT * FROM drafts WHERE id=?", (draft_id,)
            ).fetchone()
            latest = _draft_payload(latest_row)
            _p2, _c2, _r2, _t2, latest_parts = build_parts(
                latest["content"], latest["order"], latest["deleted"])
            latest_gate = evaluate_gate(latest["review"], latest_parts)
            if not latest_gate["ok"]:
                conn.rollback()
                raise HTTPException(status_code=422, detail={
                    "message": "逐条复核在签发事务内复查未通过（确认与当前草稿"
                               "不一致），已拒绝签发（草稿未修改）",
                    "review": {
                        "missing": latest_gate["missing"],
                        "expired": latest_gate["expired"],
                    },
                })

        existing = conn.execute(
            "SELECT * FROM versions WHERE draft_id=? AND fingerprint=?",
            (draft_id, fingerprint),
        ).fetchone()
        if existing is not None:
            conn.rollback()
            payload = _version_row_to_dict(existing, include_snapshot=False)
            payload["issued"] = False
            payload["reused"] = True
            return payload

        summary = build_summary(ref_result, term_result, draft["term_ignores"])
        max_no = conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) FROM versions WHERE draft_id=?",
            (draft_id,),
        ).fetchone()[0]
        version_no = max_no + 1
        now = int(time.time())

        cur = conn.execute(
            "INSERT INTO versions (draft_id, version_no, revision, fingerprint, "
            "title, content, order_json, deleted_json, term_ignores_json, "
            "ref_result_json, term_result_json, summary_json, issued_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (draft_id, version_no, current_rev, fingerprint, draft["title"],
             draft["content"], json.dumps(draft["order"], ensure_ascii=False),
             json.dumps(draft["deleted"], ensure_ascii=False),
             json.dumps(draft["term_ignores"], ensure_ascii=False),
             json.dumps(ref_result, ensure_ascii=False),
             json.dumps(term_result, ensure_ascii=False),
             json.dumps(summary, ensure_ascii=False), now),
        )
        new_id = cur.lastrowid
        conn.commit()
        saved = conn.execute(
            "SELECT * FROM versions WHERE id=?", (new_id,)
        ).fetchone()

    payload = _version_row_to_dict(saved, include_snapshot=False)
    payload["issued"] = True
    return payload


@app.get("/api/versions/{version_id}")
def get_version(version_id: int) -> dict:
    """查看单个不可变版本的完整内容（含编号、引用改写与术语绑定快照）。"""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM versions WHERE id=?", (version_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="版本不存在")
    payload = _version_row_to_dict(row, include_snapshot=True)
    payload["issued"] = False
    return payload


class AuditRequest(BaseModel):
    # 审计方向固定为 old_version → new_version（均须为同一草稿的已签发版本）
    old_version_id: int
    new_version_id: int


@app.post("/api/versions/audit")
def audit(req: AuditRequest) -> dict:
    """版本差异审计：对齐同一草稿的两份不可变快照。

    只读取 versions 行：草稿后续编辑 / 删除均不影响审计结果。
    两版非同草稿或指向同一版本 → 400；版本不存在 → 404。
    """
    with db() as conn:
        rows = {
            r["id"]: r
            for r in conn.execute(
                "SELECT * FROM versions WHERE id IN (?, ?)",
                (req.old_version_id, req.new_version_id),
            ).fetchall()
        }
    old_row = rows.get(req.old_version_id)
    new_row = rows.get(req.new_version_id)
    if old_row is None or new_row is None:
        missing = req.old_version_id if old_row is None else req.new_version_id
        raise HTTPException(status_code=404, detail=f"版本不存在：{missing}")
    try:
        return audit_versions(old_row, new_row)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class ApplySelection(BaseModel):
    clause_id: str
    dimensions: list[str] = Field(default_factory=list)


class ApplyAuditPreviewRequest(BaseModel):
    """应用到当前草稿（预览）：只读，返回可勾选条目 / 冲突 / 三侧快照。"""
    old_version_id: int
    new_version_id: int


class ApplyAuditRequest(BaseModel):
    """应用到当前草稿（提交）：乐观锁 + 两版本快照校验，一次事务落库。"""
    old_version_id: int
    new_version_id: int
    revision: int
    old_fingerprint: str
    new_fingerprint: str
    selections: list[ApplySelection] = Field(default_factory=list)


def _load_apply_versions(conn: sqlite3.Connection,
                         old_id: int, new_id: int,
                         draft_id: int) -> tuple:
    rows = {
        r["id"]: r
        for r in conn.execute(
            "SELECT * FROM versions WHERE id IN (?, ?)", (old_id, new_id)
        ).fetchall()
    }
    old_row, new_row = rows.get(old_id), rows.get(new_id)
    if old_row is None or new_row is None:
        missing = old_id if old_row is None else new_id
        raise HTTPException(status_code=404, detail=f"版本不存在：{missing}")
    if old_id == new_id:
        raise HTTPException(status_code=400, detail="请选择两个不同的版本")
    if old_row["draft_id"] != draft_id or new_row["draft_id"] != draft_id:
        raise HTTPException(status_code=400,
                            detail="只能把该草稿自己签发的版本应用到当前草稿")
    return old_row, new_row


@app.post("/api/drafts/{draft_id}/apply-preview")
def apply_preview(draft_id: int, req: ApplyAuditPreviewRequest) -> dict:
    """应用到当前草稿的只读预览：以旧版为基线、新版为候选逐条比对草稿。

    不写任何状态；草稿不存在 → 404，版本异常 → 400/404。
    """
    with db() as conn:
        drow = conn.execute(
            "SELECT * FROM drafts WHERE id=?", (draft_id,)
        ).fetchone()
        if drow is None:
            raise HTTPException(status_code=404, detail="草稿不存在")
        old_row, new_row = _load_apply_versions(
            conn, req.old_version_id, req.new_version_id, draft_id)
        draft = _draft_payload(drow)
    return build_apply_preview(old_row, new_row, draft)


@app.post("/api/drafts/{draft_id}/apply-audit")
def apply_audit_to_draft(draft_id: int, req: ApplyAuditRequest) -> dict:
    """把审计差异按勾选应用到当前草稿。

    全部校验在写库前完成，过期 / 阻塞 / 任一冲突均整体拒绝且不写部分结果；
    通过后在 ``BEGIN IMMEDIATE`` 单事务内保存为草稿的新修订（revision+1）。
    """
    import json

    with db() as conn:
        drow = conn.execute(
            "SELECT * FROM drafts WHERE id=?", (draft_id,)
        ).fetchone()
        if drow is None:
            raise HTTPException(status_code=404, detail="草稿不存在")
        old_row, new_row = _load_apply_versions(
            conn, req.old_version_id, req.new_version_id, draft_id)

        if old_row["fingerprint"] != req.old_fingerprint \
                or new_row["fingerprint"] != req.new_fingerprint:
            raise HTTPException(status_code=409, detail={
                "message": "审计版本快照与当前签发版本不一致（版本行已变化），"
                           "请重新生成审计与应用预览",
                "current_revision": drow["revision"],
            })

        draft = _draft_payload(drow)

        # 1) 计算合并（尚未加锁、尚未写库）：选择冲突 / 非法在此整体拒绝
        try:
            merged = apply_merge(
                old_row, new_row, draft,
                [s.model_dump() for s in req.selections])
        except MergeConflict as exc:
            raise HTTPException(status_code=422, detail={
                "message": "存在与当前草稿冲突的勾选项，已整体拒绝（草稿未修改）",
                "conflicts": exc.conflicts,
            })
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        # 2) 合并结果重算后的阻塞项（引用 / 术语；术语阻塞扣除草稿既有忽略项）
        ignore_set = set(draft["term_ignores"])
        blockers = {
            "reference_issues": list(merged["ref_result"]["issues"]),
            "term_issues": [
                it for it in merged["term_result"]["issues"]
                if it["occ_id"] not in ignore_set
            ],
        }
        if blockers["reference_issues"] or blockers["term_issues"]:
            raise HTTPException(status_code=422, detail={
                "message": "合并后的草稿仍有未通过的引用或术语问题，"
                           "已整体拒绝（草稿未修改）",
                "blockers": blockers,
                "conflicts": [],
            })

        # 3) 加事务锁后复查修订号：校验期间草稿被改 → 409 回滚
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "SELECT revision FROM drafts WHERE id=?", (draft_id,)
        ).fetchone()
        if cur is None:
            conn.rollback()
            raise HTTPException(status_code=404, detail="草稿不存在")
        current_rev = cur[0]
        if current_rev != req.revision or current_rev != draft["revision"]:
            conn.rollback()
            raise HTTPException(status_code=409, detail={
                "message": "草稿在校验期间已被修改（修订号变化），"
                           "已拒绝应用且未写入任何内容",
                "current_revision": current_rev,
            })

        now = int(time.time())
        conn.execute(
            "UPDATE drafts SET title=?, content=?, order_json=?, "
            "deleted_json=?, term_ignores_json=?, "
            "revision=revision+1, updated_at=? WHERE id=?",
            (merged["title"], merged["content"],
             json.dumps(merged["order"], ensure_ascii=False),
             json.dumps(merged["deleted"], ensure_ascii=False),
             json.dumps(merged["term_ignores"], ensure_ascii=False),
             now, draft_id),
        )
        new_rev = conn.execute(
            "SELECT revision FROM drafts WHERE id=?", (draft_id,)
        ).fetchone()[0]

        # 逐条复核：应用版本差异改变了正文 / 父子顺序 / 删除状态，
        # 在合并结果（已重算的引用目标与术语绑定）上迁移确认，仅相关条款过期。
        review_payload = []
        if drow["review_enabled"]:
            _p, _c, _r, _t, mparts = build_parts(
                merged["content"], merged["order"], merged["deleted"])
            old_review = json.loads(drow["review_json"] or "[]")
            migrated_review = migrate_confirmations(
                old_review, mparts, new_rev)
            conn.execute(
                "UPDATE drafts SET review_json=? WHERE id=?",
                (json.dumps(migrated_review, ensure_ascii=False), draft_id),
            )
            review_payload = migrated_review
        conn.commit()

    return {
        "ok": True,
        "revision": new_rev,
        "applied": merged["applied"],
        "draft": {
            "content": merged["content"],
            "order": merged["order"],
            "deleted": merged["deleted"],
            "term_ignores": merged["term_ignores"],
        },
        "review_migrated": review_payload,
    }


# ---------------------------------------------------------------- 静态页面

STATIC_DIR = os.path.join(BASE_DIR, "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
