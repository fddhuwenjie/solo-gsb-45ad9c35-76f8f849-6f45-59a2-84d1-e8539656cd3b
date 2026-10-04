"""逐条复核确认：以稳定条款 ID 逐条确认存活条款。

每份草稿可开启「复核」；开启后用户可对每个存活条款（按解析时的原路径稳定
ID，如 ``c-3-2``）提交确认。确认在保存时绑定条款的四要素：

- **正文**：条款正文逐字一致（编号标记本身不参与）；
- **父级**：实际存在的直接父条款稳定 ID（如 ``c-3-2`` 的父级为 ``c-3``）。
  原路径前缀对应的条款在原文中缺失时绑定的是「虚父级」而非真实条款——
  只有第3.1 / 3.2条而无第3条时，子条款绑定虚父级；之后补入真实第3条，
  父级绑定由虚变实，子条款确认即过期，须连同新父条款重新确认；
- **引用目标**：正文中每处引用（按扫描序）的状态与绑定的目标条款集合；
- **术语绑定**：条款内每处术语使用（按块内同名第 k 次）的状态与绑定的
  定义。定义与使用一律以**语义键**标识（定义 = 术语名 + 作用域 + 所在块
  稳定 ID + 同块同名序；使用 = 术语名 + 块内同名序），不依赖字符偏移：
  在定义条款的定义之前插入普通说明只移动字符位置，使用条款确认保持有效；
  定义删除 / 改绑 / 变歧义时，相关使用条款确认照常过期。

之后草稿再次保存时 :func:`migrate_confirmations` 逐条比对当前签名：

- 正文 / 父级变化、重排 / 删除 / 恢复 / 应用版本差异导致引用目标或术语绑定
  变化时，**仅相关条款**的确认变为过期（过期为粘性状态，须用户重新确认）；
- 单纯编号改写（同父兄弟换序、其他条款删除引起的连续重编号）四要素均不变，
  确认保持有效；
- 条款被删除（含级联）→ 该条款确认过期；删除 / 恢复会改变引用目标 / 术语
  绑定，相关条款的引用 / 术语签名同步变化、各自过期；
- 原路径编号在文本中消失（整块删除或改成其他编号）→ 记录保留为过期，
  同路径再次出现时仍须重新确认；过期确认不会因正文改回而自动复活。

历史确认只保存签名哈希，无法核实其当时的绑定口径；凡不带
:data:`SIG_VERSION` 标记的旧记录一律**保守标过期**，重新确认后方可生效。

签发时 :func:`evaluate_gate` 复查全部存活条款：缺失确认或存在过期确认即整体
拒绝（不产生版本）；未开启复核的草稿不走本关卡。保存草稿、应用版本差异、
复核页状态与签发关卡共用 :func:`build_parts` 这同一份绑定判定。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .parser import parse_contract
from .references import _scan, analyze, cascade_deleted, expand_range
from .terms import review_terms

# 签名维度 -> 过期原因（顺序即展示顺序）
REASON_LABELS = {
    "body": "条款正文已变化",
    "parent": "父级绑定已变化（虚父级补入真实父条款或父级条款改绑）",
    "refs": "引用目标已变化",
    "terms": "术语绑定已变化",
    "lifecycle_deleted": "条款已删除（含父条款级联删除）",
    "clause_missing": "条款已不存在（整块删除或原路径编号变化）",
    "legacy_sig": "旧版确认只存哈希、无法核实原绑定，需按当前条款重新确认",
}
_SIG_KEYS = ("body", "parent", "refs", "terms")

# 签名口径版本：随确认记录保存。签名 / 绑定判定口径调整时前进一号；
# 旧记录（无此字段）只存了哈希，无法核实当时绑定，一律保守标过期。
SIG_VERSION = 2


def _digest(obj: Any) -> str:
    if isinstance(obj, str):
        data = obj
    else:
        data = json.dumps(obj, ensure_ascii=False,
                          sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def bound_parent(path: tuple[int, ...],
                 path_to_id: dict[tuple[int, ...], str]) -> str | None:
    """四要素中的父级绑定：真实父条款的稳定 ID；父级条款在原文缺失时为虚父级。

    - 顶层条款 → ``None``（无父级）；
    - 路径前缀对应条款真实存在 → 该条款稳定 ID（如 ``c-3``）；
    - 前缀条款缺失（只有 3.1 / 3.2 而无第3条）→ ``"virtual:" + 前缀 slug``。

    因此「先有子条款、后补入真实父条款」会让子条款的父级绑定由虚变实，
    子条款确认过期；同父兄弟换序等纯编号变化不改变本绑定。
    """
    if len(path) <= 1:
        return None
    pref = path[:-1]
    real = path_to_id.get(pref)
    if real is not None:
        return real
    return "virtual:c-" + "-".join(str(p) for p in pref)


# ---------------------------------------------------------------- 当前签名

def _def_semantic_keys(term_result: dict, clauses) -> dict[str, tuple]:
    """定义 occ_id → 语义键 (术语名, 作用域, 所在块稳定 ID, 同块同名序)。

    与版本差异审计（audit._def_semantic_map）同口径：键中不含字符偏移，
    在定义所在块的定义之前插入普通说明不会改变键；同块新增同名定义则
    序位后移 / 变歧义，相关使用照常过期。
    """
    block_doc = {c.id: i for i, c in enumerate(clauses)}
    defs = sorted(
        (d for d in term_result.get("definitions", [])),
        key=lambda d: (block_doc.get(d["block_id"], 10 ** 9), d["start"]),
    )
    counters: dict[tuple, int] = {}
    id_to_key: dict[str, tuple] = {}
    for d in defs:
        base = (d["name"], d["scope"], d["block_id"])
        ordinal = counters.get(base, 0)
        counters[base] = ordinal + 1
        id_to_key[d["id"]] = base + (ordinal,)
    return id_to_key


def _parts_from_results(clauses, ref_result: dict,
                        term_result: dict,
                        deleted_set: set[str]) -> dict[str, dict]:
    """按 analyze / review_terms 的结果计算每个条款的当前四要素签名。"""
    view = {c["id"]: c for c in ref_result["clauses"]}
    raw = {c.id: _scan(c.text) for c in clauses}

    # 路径 -> id（重复路径取首条，与引用绑定语义一致）
    path_to_id: dict[tuple[int, ...], str] = {}
    for c in clauses:
        path_to_id.setdefault(c.path, c.id)

    term_block = {b["id"]: b for b in term_result["blocks"]
                  if b.get("kind") == "clause"}
    def_semantic = _def_semantic_keys(term_result, clauses)

    parts: dict[str, dict] = {}
    for c in clauses:
        # ---- 引用：每处（扫描序, 状态, 目标集合）----
        marks = view[c.id]["refs"]
        ref_entries: list[list[Any]] = []
        for i, (r, m) in enumerate(zip(raw[c.id], marks)):
            if r.kind == "single":
                tid = path_to_id.get(r.a)  # type: ignore[arg-type]
                targets = [tid] if tid else []
            elif r.kind == "range":
                ids, _missing = expand_range(r.a, r.b, path_to_id)  # type: ignore
                targets = ids
            else:
                targets = []
            ref_entries.append([i, m["status"], sorted(set(targets))])

        # ---- 术语：使用按「块内同名第 k 次」对齐（不随字符位移），
        #      绑定的定义用语义键（术语名+作用域+所在块+同块同名序）----
        use_entries: list[list[Any]] = []
        name_ord: dict[str, int] = {}
        tb = term_block.get(c.id)
        if tb:
            use_marks = sorted(
                (m for m in tb.get("marks", []) if m.get("kind") == "use"),
                key=lambda m: m["start"],
            )
            for m in use_marks:
                ordinal = name_ord.get(m["name"], 0)
                name_ord[m["name"]] = ordinal + 1
                target_keys = sorted(
                    def_semantic[did] for did in (m.get("target_ids") or [])
                    if did in def_semantic
                )
                use_entries.append([
                    m["name"], ordinal, m["status"], target_keys,
                ])

        sig = {
            "body": _digest(c.text),
            "parent": _digest(bound_parent(c.path, path_to_id) or ""),
            "refs": _digest(ref_entries),
            "terms": _digest(use_entries),
        }
        alive = c.id not in deleted_set
        struct = next((x for x in ref_result["result"] if x["id"] == c.id), None)
        parts[c.id] = {
            "sig": sig,
            "alive": alive,
            "label": c.label,
            "heading": c.heading,
            "new_label": struct["new_label"] if struct else "",
            "position": struct["new_position"] if struct else 0,
            "doc": next((i for i, x in enumerate(clauses) if x.id == c.id), 0),
        }
    return parts


def build_parts(content: str, order_in: list[str],
                deleted_in: list[str]):
    """重跑解析 / 引用改写 / 术语审校并计算条款签名。

    入参规整与 /api/analyze、validate_snapshot 同口径：剔除解析不到的 id，
    删除父条款按级联展开。返回
    ``(前置说明, clauses, ref_result, term_result, parts)``。
    """
    preamble, clauses = parse_contract(content)
    known = {c.id for c in clauses}
    order = [cid for cid in order_in if cid in known]
    deleted = [cid for cid in deleted_in if cid in known]
    ref_result = analyze(clauses, order, deleted)
    deleted_set = cascade_deleted(clauses, deleted)
    term_result = review_terms(preamble, clauses, deleted_set)
    parts = _parts_from_results(clauses, ref_result, term_result, deleted_set)
    return preamble, clauses, ref_result, term_result, parts


# ---------------------------------------------------------------- 保存时迁移

def _diff_reasons(old_sig: dict, new_sig: dict) -> list[str]:
    return [k for k in _SIG_KEYS if old_sig.get(k) != new_sig.get(k)]


def _is_legacy(rec: dict) -> bool:
    """旧口径确认（只存哈希、无签名版本标记）：无法核实原绑定，保守处理。"""
    return rec.get("sig_version") != SIG_VERSION


def migrate_confirmations(records: list[dict], parts: dict[str, dict],
                          revision: int) -> list[dict]:
    """草稿保存为新修订后迁移确认记录（不碰数据库，由调用方随事务写入）。

    - 当前存活且签名一致的有效确认保持有效（跨修订号，单纯编号变化不撤销）；
    - 任一要素变化 → 仅该条款确认过期（粘性，保留确认时签名与首次过期修订号）；
    - 条款当前不存活（删除 / 级联）→ 其既有确认过期；
    - 已过期确认保持过期，过期原因按当前签名重新计算；
    - 条款彻底消失（无当前 parts）→ 记录保留为过期（clause_missing）；
    - 从无确认记录的条款不产生行（状态接口按「待确认」呈现）；
    - 旧口径确认（无 :data:`SIG_VERSION` 标记，历史上只存哈希）无法核实
      当时绑定，无论签名是否碰巧一致都**保守标过期**，重新确认后方可生效；
    - 已过期确认不会因正文 / 顺序改回原样而自动复活（过期粘性）。
    """
    existing = {r["clause_id"]: r for r in records}
    out: list[dict] = []

    for cid, part in parts.items():
        rec = existing.get(cid)
        if rec is None:
            continue
        rec = dict(rec)
        if not part["alive"]:
            rec["expired"] = True
            rec.setdefault("expire_revision", revision)
            rec["expire_reasons"] = ["lifecycle_deleted"]
            out.append(rec)
            continue
        if not _is_legacy(rec) and not rec.get("expired"):
            reasons = _diff_reasons(rec.get("sig", {}), part["sig"])
            if reasons:
                rec["expired"] = True
                rec["expire_revision"] = revision
                rec["expire_reasons"] = reasons
            out.append(rec)
            continue
        # 已过期（粘性）或旧口径确认：保持过期。旧口径给 legacy_sig；
        # 其余过期原因按当前签名刷新，首次过期修订号保留。
        rec["expired"] = True
        rec.setdefault("expire_revision", revision)
        if _is_legacy(rec):
            rec["expire_reasons"] = ["legacy_sig"]
        else:
            rec["expire_reasons"] = (
                _diff_reasons(rec.get("sig", {}), part["sig"])
                or rec.get("expire_reasons") or list(_SIG_KEYS))
        out.append(rec)

    for cid, rec in existing.items():
        if cid in parts:
            continue
        rec = dict(rec)
        rec["expired"] = True
        rec.setdefault("expire_revision", revision)
        rec["expire_reasons"] = ["clause_missing"]
        out.append(rec)

    return out


# ---------------------------------------------------------------- 状态与关卡

def _clause_state(rec: dict | None, part: dict) -> str:
    if not part["alive"]:
        return "deleted"
    if rec is None:
        return "pending"
    if rec.get("expired") or _is_legacy(rec):
        return "expired"
    if _diff_reasons(rec.get("sig", {}), part["sig"]):
        return "expired"
    return "valid"


def _clause_desc(cid: str, state: str, part: dict,
                 rec: dict | None) -> dict:
    reasons = []
    if state == "expired":
        if rec is not None and _is_legacy(rec):
            codes = ["legacy_sig"]
        else:
            codes = (rec.get("expire_reasons") if rec else None) \
                or _diff_reasons((rec or {}).get("sig", {}), part["sig"])
        reasons = codes or list(_SIG_KEYS)
    return {
        "clause_id": cid,
        "state": state,
        "label": part["label"],
        "new_label": part["new_label"],
        "heading": part["heading"],
        "position": part["position"] if state != "deleted" else 0,
        "confirmed_revision": (rec or {}).get("revision"),
        "confirmed_at": (rec or {}).get("confirmed_at"),
        "expire_revision": (rec or {}).get("expire_revision"),
        "reasons": reasons,
        "reason_texts": [REASON_LABELS[r] for r in reasons],
    }


def build_status(enabled: bool, records: list[dict], parts: dict[str, dict],
                 revision: int) -> dict:
    """复核状态（供页面与签发校验共用）：逐条 待确认 / 有效 / 过期 / 已删除。"""
    recmap = {r["clause_id"]: r for r in records}
    clauses: list[dict] = []
    counts = {"pending": 0, "valid": 0, "expired": 0, "deleted": 0}

    ordered = sorted(
        parts,
        key=lambda cid: (
            0 if parts[cid]["alive"] else 1,
            parts[cid]["position"] if parts[cid]["alive"] else parts[cid]["doc"],
            cid,
        ))
    for cid in ordered:
        part = parts[cid]
        rec = recmap.get(cid)
        state = _clause_state(rec, part)
        counts[state] += 1
        clauses.append(_clause_desc(cid, state, part, rec))

    return {
        "enabled": enabled,
        "revision": revision,
        "counts": {k: counts[k] for k in ("pending", "valid", "expired")},
        "deleted_count": counts["deleted"],
        "total_alive": counts["pending"] + counts["valid"] + counts["expired"],
        "clauses": clauses,
    }


def evaluate_gate(records: list[dict], parts: dict[str, dict]) -> dict:
    """签发关卡：开启复核时全部存活条款须存在有效确认。

    返回 ``{"ok": bool, "missing": [...], "expired": [...]}``；
    ``missing`` 为从未确认（含从未保存确认记录）的存活条款，
    ``expired`` 为确认已过期（或防御性比对发现签名不符）的存活条款。
    """
    status = build_status(True, records, parts, revision=0)
    missing, expired = [], []
    for c in status["clauses"]:
        if c["state"] == "pending":
            missing.append(c)
        elif c["state"] == "expired":
            expired.append(c)
    return {"ok": not missing and not expired,
            "missing": missing, "expired": expired}
