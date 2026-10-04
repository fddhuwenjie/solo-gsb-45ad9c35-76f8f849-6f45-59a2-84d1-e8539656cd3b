"""逐条复核确认：以稳定条款 ID 逐条确认存活条款。

每份草稿可开启「复核」；开启后用户可对每个存活条款（按解析时的原路径稳定
ID，如 ``c-3-2``）提交确认。确认在保存时绑定条款的四要素：

- **正文**：条款正文逐字一致（编号标记本身不参与）；
- **父级**：原路径**最近的真实祖先条款**稳定 ID——直接父级编号实际成条款时
  即该条款（如 ``c-3-2`` 的父级为 ``c-3``）；父级编号未单列成条款（虚父节点）
  时向上取最近存在的祖先，均无则为空。因此补入真实父条款（如原本只有
  第3.1/3.2条时补入第3条）会改变子条款的父级要素，使其确认过期；
- **引用目标**：正文中每处引用（按扫描序）的状态与绑定的目标条款集合；
- **术语绑定**：条款内每处术语使用（同块同名第 k 次，与版本差异审计对齐
  口径一致）的状态与绑定的定义；定义以「术语名 + 作用域 + 所在块稳定 ID +
  同块同名序」的语义键标识——在定义前插入说明文字只会改变定义的字符偏移，
  不会改变语义键，使用条款的确认保持有效；定义被删除 / 改绑 / 变歧义时
  语义键或绑定状态改变，相关确认照常过期。

之后草稿再次保存时 :func:`migrate_confirmations` 逐条比对当前签名：

- 正文 / 父级变化、重排 / 删除 / 恢复 / 应用版本差异导致引用目标或术语绑定
  变化时，**仅相关条款**的确认变为过期（过期为粘性状态，须用户重新确认，
  文本改回也不会自动恢复有效）；
- 单纯编号改写（同父兄弟换序、其他条款删除引起的连续重编号）四要素均不变，
  确认保持有效；
- 条款被删除（含级联）→ 该条款确认过期；删除 / 恢复会改变引用目标 / 术语
  绑定，相关条款的引用 / 术语签名同步变化、各自过期；
- 原路径编号在文本中消失（整块删除或改成其他编号）→ 记录保留为过期，
  同路径再次出现时仍须重新确认。

确认记录只保存四要素的**哈希签名**并带签名口径版本 :data:`SIG_VERSION`；
口径升级后历史确认无法按新口径核实原绑定，一律**保守标记为过期**
（原因 ``scheme``），人工重新确认后方可恢复有效。

签发时 :func:`evaluate_gate` 复查全部存活条款：缺失确认或存在过期确认即整体
拒绝（不产生版本）；未开启复核的草稿不走本关卡。保存草稿、应用版本差异、
复核页状态与签发关卡均经由 :func:`build_parts` 的同一套签名判定。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .parser import parse_contract
from .references import _scan, analyze, cascade_deleted, expand_range
from .terms import review_terms

# 签名口径版本：确认记录只存哈希，口径升级后历史签名无法核实，
# 一律按过期处理（须重新确认）。v2：父级取最近真实祖先；术语定义改用语义键。
SIG_VERSION = 2

# 签名维度 -> 过期原因（顺序即展示顺序）
REASON_LABELS = {
    "body": "条款正文已变化",
    "parent": "父级层级已变化",
    "refs": "引用目标已变化",
    "terms": "术语绑定已变化",
    "lifecycle_deleted": "条款已删除（含父条款级联删除）",
    "clause_missing": "条款已不存在（整块删除或原路径编号变化）",
    "scheme": "确认签名口径已升级，历史确认无法核实，需重新确认",
}
_SIG_KEYS = ("body", "parent", "refs", "terms")


def _digest(obj: Any) -> str:
    if isinstance(obj, str):
        data = obj
    else:
        data = json.dumps(obj, ensure_ascii=False,
                          sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _nearest_parent_id(path: tuple[int, ...],
                       path_to_id: dict[tuple[int, ...], str]) -> str | None:
    """最近的真实祖先条款稳定 ID（虚父节点不算；顶层 / 无真实祖先为 None）。

    只按原路径前缀在**实际解析出的条款**中查找：原文未单列的父级编号
    （如只有第3.1条时的「第3条」）不构成父级；补入真实父条款后该要素
    随之改变，子条款确认过期。
    """
    for depth in range(len(path) - 1, 0, -1):
        pid = path_to_id.get(path[:depth])
        if pid is not None:
            return pid
    return None


def _definition_keys(term_result: dict,
                     clauses) -> dict[str, tuple]:
    """定义 occ_id -> 语义键（术语名, 作用域, 所在块稳定 ID, 同块同名序）。

    与版本差异审计的定义对齐口径一致：不随定义在块内的字符偏移漂移，
    定义删除 / 新增同名定义导致序变化时才会改变。
    """
    doc = {c.id: i for i, c in enumerate(clauses)}
    defs = sorted(term_result.get("definitions", []),
                  key=lambda d: (doc.get(d["block_id"], -1), d["start"]))
    counters: dict[tuple, int] = {}
    out: dict[str, tuple] = {}
    for d in defs:
        base = (d["name"], d["scope"], d["block_id"])
        ordinal = counters.get(base, 0)
        counters[base] = ordinal + 1
        out[d["id"]] = base + (ordinal,)
    return out


# ---------------------------------------------------------------- 当前签名

def _parts_from_results(clauses, ref_result: dict,
                        term_result: dict,
                        deleted_set: set[str]) -> dict[str, dict]:
    """按 analyze / review_terms 的结果计算每个条款的当前四要素签名。"""
    by_id = {c.id: c for c in clauses}
    view = {c["id"]: c for c in ref_result["clauses"]}
    raw = {c.id: _scan(c.text) for c in clauses}

    # 路径 -> id（重复路径取首条，与引用绑定语义一致）
    path_to_id: dict[tuple[int, ...], str] = {}
    for c in clauses:
        path_to_id.setdefault(c.path, c.id)

    # 定义 occ_id -> 语义键（抗字符偏移，与审计口径一致）
    def_key = _definition_keys(term_result, clauses)

    term_block = {b["id"]: b for b in term_result["blocks"]
                  if b.get("kind") == "clause"}

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

        # ---- 术语：每处使用（同块同名第 k 次, 名称, 状态, 绑定定义的语义键）----
        term_entries: list[list[Any]] = []
        tb = term_block.get(c.id)
        if tb:
            counters: dict[str, int] = {}
            uses = sorted((m for m in tb.get("marks", [])
                           if m.get("kind") == "use"),
                          key=lambda m: m["start"])
            for m in uses:
                k = counters.get(m["name"], 0)
                counters[m["name"]] = k + 1
                targets = sorted(
                    def_key.get(t, ("§raw", t, "", -1))
                    for t in (m.get("target_ids") or []))
                term_entries.append([m["name"], k, m["status"], targets])
            term_entries.sort()

        sig = {
            "body": _digest(c.text),
            "parent": _digest(
                _nearest_parent_id(c.path, path_to_id) or ""),
            "refs": _digest(ref_entries),
            "terms": _digest(term_entries),
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


def _current_reasons(rec: dict, part: dict) -> list[str]:
    """按当前签名重算过期原因。

    历史确认只存哈希：签名口径（``sig_v``）与当前不一致时无法核实原绑定，
    不再逐维度比对，保守给出 ``scheme``（须重新确认后方可生效）。
    """
    if rec.get("sig_v") != SIG_VERSION:
        return ["scheme"]
    return _diff_reasons(rec.get("sig", {}), part["sig"])


def migrate_confirmations(records: list[dict], parts: dict[str, dict],
                          revision: int) -> list[dict]:
    """草稿保存为新修订后迁移确认记录（不碰数据库，由调用方随事务写入）。

    - 当前存活且签名一致的有效确认保持有效（跨修订号，单纯编号变化不撤销）；
    - 任一要素变化 → 仅该条款确认过期（粘性，保留确认时签名与首次过期修订号）；
    - 历史口径（sig_v 不符）的确认无法核实原绑定 → 保守标过期（scheme），
      须重新确认后方可生效；
    - 条款当前不存活（删除 / 级联）→ 其既有确认过期；
    - 已过期确认保持过期（文本恢复也不自动生效），过期原因按当前签名重新计算；
    - 条款彻底消失（无当前 parts）→ 记录保留为过期（clause_missing）；
    - 从无确认记录的条款不产生行（状态接口按「待确认」呈现）。
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
        if rec.get("expired"):
            # 粘性过期：原因按当前签名刷新，首次过期修订号保留
            reasons = _current_reasons(rec, part) \
                or rec.get("expire_reasons") or list(_SIG_KEYS)
            rec["expire_reasons"] = reasons
            rec.setdefault("expire_revision", revision)
            out.append(rec)
            continue
        if rec.get("sig_v") != SIG_VERSION:
            # 口径升级：历史确认只存哈希、无法核实原绑定 → 保守标过期
            rec["expired"] = True
            rec["expire_revision"] = revision
            rec["expire_reasons"] = ["scheme"]
            out.append(rec)
            continue
        reasons = _diff_reasons(rec.get("sig", {}), part["sig"])
        if reasons:
            rec["expired"] = True
            rec["expire_revision"] = revision
            rec["expire_reasons"] = reasons
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
    if rec.get("expired"):
        return "expired"
    if rec.get("sig_v") != SIG_VERSION:
        # 历史口径确认：无法核实原绑定，保守按过期处理
        return "expired"
    if _diff_reasons(rec.get("sig", {}), part["sig"]):
        return "expired"
    return "valid"


def _clause_desc(cid: str, state: str, part: dict,
                 rec: dict | None) -> dict:
    reasons = []
    if state == "expired":
        codes = (rec.get("expire_reasons") if rec else None) \
            or _current_reasons(rec or {}, part)
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
