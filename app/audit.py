"""版本差异审计：对齐同一草稿的两份不可变签发快照。

两份快照均来自 ``versions`` 表（不可变），审计本身只读取版本行，不依赖也不
修改草稿——草稿后续被编辑甚至删除，都不影响已生成的审计。

对齐方式：

- 条款以**原条款稳定 ID**（解析时的原路径 slug，如 ``c-3-2``）对齐，逐条区分
  正文改变、层级 / 顺序调整、删除 / 新增、删除后恢复与仅编号改写；
- 引用按出现处对齐（位置优先，正文改动后按同条款内序号兜底），逐条比较
  **目标集合、签发时的区间表达与原文**；
- 术语使用按「块内同名第 k 次」对齐，比较绑定定义（用语义键而非会随正文
  漂移的 occ_id），区分绑定丢失（旧定义在新版消失 / 所在条款删除）、
  变歧义与改绑。

对「目标已删除」或「术语绑定改变」的差异，在**新版存活引用图**上求强连通
分量（SCC，含自环）并收缩为 DAG：循环组合并为一个影响项，沿反向引用多源
BFS 给出到根因的最短受影响链；等长链按新版文档顺序取字典序最小者。
所有输出均为稳定排序，并带两版定位。
"""
from __future__ import annotations

from collections import deque
from typing import Any

from .parser import parse_contract
from .preview import _scc
from .references import _scan, expand_range
from .terms import QUOTE_RE

# 差异大类 -> 列表次级排序权重
_CLAUSE_KIND_ORDER = {
    "removed": 0,
    "lifecycle_deleted": 0,
    "added": 1,
    "lifecycle_restored": 1,
    "body_changed": 2,
    "reorder": 3,
    "numbering_only": 4,
}
_REF_KIND_ORDER = {
    "ref_removed": 0,
    "ref_added": 1,
    "ref_changed": 2,
}
_TERM_KIND_ORDER = {
    "term_use_removed": 0,
    "term_use_added": 1,
    "term_binding_lost": 2,
    "term_binding_ambiguous": 3,
    "term_binding_changed": 4,
}


# ---------------------------------------------------------------- 快照装载

class _Side:
    """一份不可变版本快照解析后的工作视图。"""

    def __init__(self, version_id: int, version_no: int, revision: int,
                 title: str, issued_at: int, content: str,
                 ref_result: dict, term_result: dict):
        self.id = version_id
        self.version_no = version_no
        self.revision = revision
        self.title = title
        self.issued_at = issued_at
        self.content = content
        self.preamble, self.clauses = parse_contract(content)
        self.ref = ref_result
        self.term = term_result

        self.by_id = {c.id: c for c in self.clauses}
        self.doc = {c.id: i for i, c in enumerate(self.clauses)}
        self.path_to_id: dict[tuple[int, ...], str] = {}
        for c in self.clauses:
            self.path_to_id.setdefault(c.path, c.id)

        self.alive: set[str] = set(ref_result.get("order", []))
        self.deleted: set[str] = {
            c["id"] for c in ref_result.get("deleted_clauses", [])
        }
        # 存活条款的签发结构：id -> {new_label, new_position, path, depth}
        self.struct = {c["id"]: c for c in ref_result.get("result", [])}
        self.orig_view = {c["id"]: c for c in ref_result.get("clauses", [])}
        self.raw = {c.id: _scan(c.text) for c in self.clauses}

    def position(self, cid: str) -> int:
        """新版文档先序位置；非存活条款退化为解析序（+大数，排后）。"""
        info = self.struct.get(cid)
        if info:
            return info["new_position"]
        return 10 ** 6 + self.doc.get(cid, 10 ** 6)

    def issued_label(self, cid: str) -> str:
        """条款在该版签发时的编号（存活取新编号，否则取原编号）。"""
        info = self.struct.get(cid)
        if info:
            return info.get("new_label") or ""
        view = self.orig_view.get(cid)
        return view["label"] if view else ""

    def heading(self, cid: str) -> str:
        c = self.by_id.get(cid)
        return c.heading if c else ""

    def clause_loc(self, cid: str | None) -> dict | None:
        if not cid or cid not in self.by_id:
            return None
        return {
            "present": True,
            "alive": cid in self.alive,
            "label": self.by_id[cid].label,
            "issued_label": self.issued_label(cid),
            "position": self.position(cid) if cid in self.alive else 0,
            "heading": self.heading(cid),
        }

    def ref_occurrences(self, cid: str) -> list[dict]:
        """条款内按扫描顺序的可绑定引用（单条 / 区间），附签发表达与目标。"""
        view = self.orig_view.get(cid)
        marks = view["refs"] if view else []
        out: list[dict] = []
        for r, m in zip(self.raw.get(cid, []), marks):
            if r.kind not in ("single", "range"):
                continue
            if r.kind == "single":
                targets = ([self.path_to_id.get(r.a)]
                           if self.path_to_id.get(r.a) else [])
            else:
                targets, _ = expand_range(r.a, r.b, self.path_to_id)
            out.append({
                "start": r.start,
                "end": r.end,
                "text": r.text,
                "kind": r.kind,
                "expr": m.get("new_text") or r.text,
                "targets": list(targets),
            })
        return out


def _load_side(row: dict) -> _Side:
    import json
    return _Side(
        version_id=row["id"], version_no=row["version_no"],
        revision=row["revision"], title=row["title"],
        issued_at=row["issued_at"], content=row["content"],
        ref_result=json.loads(row["ref_result_json"]),
        term_result=json.loads(row["term_result_json"]),
    )


# ---------------------------------------------------------------- 对齐工具

def _parent_slug(cid: str, side: _Side) -> str | None:
    """稳定父 ID：按原路径前缀（如 c-3-2 的父为 c-3，顶层为 None）。"""
    c = side.by_id.get(cid)
    if not c or len(c.path) <= 1:
        return None
    return "c-" + "-".join(str(p) for p in c.path[:-1])


def _pair_by_key(old_items: list[dict], new_items: list[dict]):
    """出现处对齐：先按 (start,end,text) 精确配对，剩余按文档顺序兜底配对。

    返回 [(old|None, new|None), ...]，保持两侧各自文档顺序。
    """
    pairs: list[tuple] = []
    new_pool = {i: n for i, n in enumerate(new_items)}
    used_new: set[int] = set()
    for o in old_items:
        key = (o["start"], o["end"], o["text"])
        hit = next((i for i, n in enumerate(new_items)
                    if i not in used_new
                    and (n["start"], n["end"], n["text"]) == key), None)
        if hit is not None:
            used_new.add(hit)
            pairs.append((o, new_items[hit]))
    left_old = [o for o in old_items
                if not any(p[0] is o for p in pairs)]
    left_new = [n for i, n in enumerate(new_items) if i not in used_new]
    left_old.sort(key=lambda x: x["start"])
    left_new.sort(key=lambda x: x["start"])
    for o, n in zip(left_old, left_new):
        pairs.append((o, n))
    for o in left_old[len(left_new):]:
        pairs.append((o, None))
    for n in left_new[len(left_old):]:
        pairs.append((None, n))
    return pairs


# ---------------------------------------------------------------- 条款差异

def _sibling_ranks(side: _Side, common_alive: set[str]) -> dict[str, int]:
    """共同存活条款在各自原兄弟序列中的名次（仅在共同存活兄弟间比较）。"""
    groups: dict[str | None, list[str]] = {}
    for cid in common_alive:
        groups.setdefault(_parent_slug(cid, side), []).append(cid)
    ranks: dict[str, int] = {}
    for sibs in groups.values():
        sibs.sort(key=lambda x: side.position(x))
        for i, cid in enumerate(sibs):
            ranks[cid] = i
    return ranks


def _clause_diffs(old: _Side, new: _Side) -> list[dict]:
    common = set(old.by_id) & set(new.by_id)
    common_alive = {cid for cid in common
                    if cid in old.alive and cid in new.alive}
    rank_old = _sibling_ranks(old, common_alive)
    rank_new = _sibling_ranks(new, common_alive)

    diffs: list[dict] = []

    for cid in common:
        o_alive, n_alive = cid in old.alive, cid in new.alive
        oc, nc = old.by_id[cid], new.by_id[cid]
        if o_alive and not n_alive:
            kind = "lifecycle_deleted"
            message = "该条款在旧版存活，新版中被删除（含级联）"
        elif not o_alive and n_alive:
            kind = "lifecycle_restored"
            message = "该条款在旧版被删除，新版恢复存活"
        elif not o_alive and not n_alive:
            continue  # 两版均删除：无差异
        else:
            body_changed = oc.text != nc.text
            hierarchy_changed = (
                len(old.struct[cid]["path"]) != len(new.struct[cid]["path"])
                or _parent_slug(cid, old) != _parent_slug(cid, new)
            )
            order_changed = rank_old.get(cid) != rank_new.get(cid)
            numbering_changed = (
                old.struct[cid]["new_label"] != new.struct[cid]["new_label"]
            )
            if not (body_changed or hierarchy_changed
                    or order_changed or numbering_changed):
                continue
            if body_changed:
                kind = "body_changed"
                message = "条款正文已修改"
            elif hierarchy_changed:
                kind = "reorder"
                message = "层级 / 顺序已调整（所在层级变化）"
            elif order_changed:
                kind = "reorder"
                message = "层级 / 顺序已调整（同父兄弟间相对顺序变化）"
            else:
                kind = "numbering_only"
                message = "仅编号改写：正文与相对顺序不变，编号随重排 / 其他条款删除而变化"
            diffs.append({
                "kind": kind,
                "clause_id": cid,
                "flags": {
                    "body_changed": body_changed,
                    "hierarchy_changed": hierarchy_changed,
                    "order_changed": order_changed,
                    "numbering_changed": numbering_changed,
                },
                "message": message,
                "sort_pos": new.position(cid),
                "old": {
                    "present": True, "alive": True,
                    "label": oc.label, "issued_label": old.issued_label(cid),
                    "position": old.position(cid), "heading": oc.heading,
                },
                "new": {
                    "present": True, "alive": True,
                    "label": nc.label, "issued_label": new.issued_label(cid),
                    "position": new.position(cid), "heading": nc.heading,
                },
            })
            continue

        diffs.append({
            "kind": kind,
            "clause_id": cid,
            "flags": {"body_changed": False, "hierarchy_changed": False,
                      "order_changed": False, "numbering_changed": False},
            "message": message,
            "sort_pos": new.position(cid) if n_alive
            else 10 ** 6 + new.doc.get(cid, old.doc.get(cid, 0)),
            "old": old.clause_loc(cid),
            "new": new.clause_loc(cid),
        })

    # 仅旧版存在 → 删除；仅新版存在 → 新增（按原路径稳定 ID 视为不同条款）
    for cid in set(old.by_id) - set(new.by_id):
        diffs.append({
            "kind": "removed",
            "clause_id": cid,
            "flags": {"body_changed": False, "hierarchy_changed": False,
                      "order_changed": False, "numbering_changed": False},
            "message": "条款在新版中删除（原路径编号已不存在）",
            "sort_pos": 10 ** 6 + old.doc[cid],
            "old": old.clause_loc(cid),
            "new": None,
        })
    for cid in set(new.by_id) - set(old.by_id):
        diffs.append({
            "kind": "added",
            "clause_id": cid,
            "flags": {"body_changed": False, "hierarchy_changed": False,
                      "order_changed": False, "numbering_changed": False},
            "message": "新版新增条款（旧版无相同原路径编号）",
            "sort_pos": new.position(cid),
            "old": None,
            "new": new.clause_loc(cid),
        })

    diffs.sort(key=lambda d: (d["sort_pos"], _CLAUSE_KIND_ORDER[d["kind"]],
                              d["clause_id"]))
    for d in diffs:
        d.pop("sort_pos", None)
    return diffs


# ---------------------------------------------------------------- 引用差异

def _target_locs(ids: set[str], old: _Side, new: _Side) -> list[dict]:
    out = []
    for tid in sorted(ids, key=lambda x: (new.position(x), old.position(x))):
        out.append({
            "clause_id": tid,
            "old": old.clause_loc(tid),
            "new": new.clause_loc(tid),
        })
    return out


def _ref_diffs(old: _Side, new: _Side) -> list[dict]:
    diffs: list[dict] = []
    common = set(old.by_id) & set(new.by_id)
    for cid in common:
        # 任一侧该条款已删除的，引用差异随条款删除 / 恢复行表达，不重复计入
        if cid not in old.alive or cid not in new.alive:
            continue
        o_items = old.ref_occurrences(cid)
        n_items = new.ref_occurrences(cid)
        for o, n in _pair_by_key(o_items, n_items):
            if o is None and n is None:
                continue
            if o is None:
                kind, targets_lost, targets_gained = "ref_added", set(), set(n["targets"])  # type: ignore[index]
                target_changed = bool(n["targets"])  # type: ignore[index]
            elif n is None:
                kind, targets_lost, targets_gained = "ref_removed", set(o["targets"]), set()  # type: ignore[index]
                target_changed = bool(o["targets"])  # type: ignore[index]
            else:
                ot, nt = set(o["targets"]), set(n["targets"])
                targets_lost, targets_gained = ot - nt, nt - ot
                target_changed = ot != nt
                text_changed = o["text"] != n["text"]
                expr_changed = o["expr"] != n["expr"]
                if not (target_changed or text_changed or expr_changed):
                    continue
                kind = "ref_changed"

            if kind == "ref_added":
                message = f"新版新增引用「{n['text']}」"  # type: ignore[index]
                text_changed = expr_changed = False
            elif kind == "ref_removed":
                message = f"旧版引用「{o['text']}」在新版中消失"  # type: ignore[index]
                text_changed = expr_changed = False
            else:
                parts = []
                gone_labels = [old.issued_label(t) or t for t in sorted(
                    targets_lost, key=lambda x: old.position(x))]
                new_labels = [new.issued_label(t) or t for t in sorted(
                    targets_gained, key=lambda x: new.position(x))]
                if gone_labels:
                    parts.append("目标移除：" + "、".join(gone_labels))
                if new_labels:
                    parts.append("新指向：" + "、".join(new_labels))
                if expr_changed and not target_changed:
                    if o["kind"] == "range" or n["kind"] == "range":
                        why = "区间表达改写"
                    else:
                        why = "编号随重排改写"
                    parts.append(
                        f"{why}：{o['expr']} → {n['expr']}")  # type: ignore[index]
                if text_changed and not target_changed and not expr_changed:
                    parts.append(
                        f"引用原文改写：{o['text']} → {n['text']}")  # type: ignore[index]
                message = "；".join(parts) or "引用发生变化"

            changed_targets = targets_lost | targets_gained
            diffs.append({
                "kind": kind,
                "clause_id": cid,
                "owner_old_label": old.issued_label(cid),
                "owner_new_label": new.issued_label(cid),
                "target_changed": target_changed,
                "expression_changed": bool(expr_changed),
                "text_changed": bool(text_changed),
                "targets_removed": sorted(
                    targets_lost, key=lambda x: old.position(x)),
                "targets_added": sorted(
                    targets_gained, key=lambda x: new.position(x)),
                "targets": _target_locs(changed_targets, old, new),
                "message": message,
                "old": None if o is None else {
                    "start": o["start"], "text": o["text"], "expr": o["expr"],
                    "target_ids": o["targets"],
                },
                "new": None if n is None else {
                    "start": n["start"], "text": n["text"], "expr": n["expr"],
                    "target_ids": n["targets"],
                },
                "sort_owner": new.position(cid),
                "sort_start": (n or o)["start"],
            })

    diffs.sort(key=lambda d: (d["sort_owner"], d["sort_start"],
                              _REF_KIND_ORDER[d["kind"]]))
    for d in diffs:
        d.pop("sort_owner", None)
        d.pop("sort_start", None)
    return diffs


# ---------------------------------------------------------------- 术语绑定差异

def _def_semantic_map(side: _Side) -> tuple[dict[str, tuple], dict[tuple, dict]]:
    """定义 occ_id -> 语义键；语义键 -> 定义摘要。

    语义键不随正文漂移：(术语名, global/local, 所在块稳定 ID, 同块同名序)。
    """
    id_to_key: dict[str, tuple] = {}
    key_to_def: dict[tuple, dict] = {}
    counters: dict[tuple, int] = {}
    defs = sorted(side.term.get("definitions", []),
                  key=lambda d: (side.doc.get(d["block_id"], 10 ** 6),
                                 d["start"]))
    for d in defs:
        base = (d["name"], d["scope"], d["block_id"])
        ordinal = counters.get(base, 0)
        counters[base] = ordinal + 1
        key = base + (ordinal,)
        id_to_key[d["id"]] = key
        key_to_def[key] = {
            "name": d["name"],
            "scope": d["scope"],
            "scope_label": d.get("scope_label", ""),
            "block_id": d["block_id"],
            "label": d.get("label", ""),
            "alive_clause": (d["block_id"] in side.alive
                             if d["block_id"] != "preamble" else True),
        }
    return id_to_key, key_to_def


def _uses_by_block(side: _Side) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    block_text = {b["id"]: b.get("text", "")
                  for b in side.term.get("blocks", [])}
    for b in side.term.get("blocks", []):
        for m in b.get("marks", []):
            if m.get("kind") != "use":
                continue
            out.setdefault(b["id"], []).append({
                "occ_id": m["occ_id"],
                "name": m["name"],
                "start": m["start"],
                "status": m["status"],
                "target_ids": list(m.get("target_ids", [])),
                "scope_label": m.get("scope_label", ""),
                "quoted": bool(m.get("quoted")),
                "text": block_text.get(b["id"], ""),
            })
    for items in out.values():
        items.sort(key=lambda x: x["start"])
    return out


def _use_brief(u: dict | None, side: _Side,
               id_to_key: dict[str, tuple],
               key_to_def: dict[tuple, dict]) -> dict | None:
    if u is None:
        return None
    bound = None
    if u["status"] == "ok" and u["target_ids"]:
        bound = key_to_def.get(id_to_key.get(u["target_ids"][0]))  # type: ignore[arg-type]
    cands = []
    if u["status"] == "ambiguous":
        cands = [key_to_def[id_to_key[d]] for d in u["target_ids"]
                 if d in id_to_key and id_to_key[d] in key_to_def]
    return {
        "start": u["start"],
        "status": u["status"],
        "scope_label": u.get("scope_label", ""),
        "bound_definition": bound,
        "candidate_definitions": cands,
    }


def _recover_unquoted_uses(side: _Side, bid: str,
                           missing_names: set[str]) -> list[dict]:
    """在新版块文本中找回「定义消失后不再被扫描」的无引号使用。

    术语扫描器只对「仍存在定义」的名称扫描无引号出现；旧版唯一绑定的使用
    若因定义移除而变未定义，其无引号出现会从新版 marks 中整体消失。这里在
    新文本中按最长优先找回这些名称（引号片段、已被其他 mark 覆盖的区间除外）。
    """
    block = next((b for b in side.term.get("blocks", []) if b["id"] == bid),
                 None)
    if block is None:
        return []
    text = block.get("text", "")
    covered: list[tuple[int, int]] = [
        (m["start"], m["end"]) for m in block.get("marks", [])
    ]
    for q in QUOTE_RE.finditer(text):
        covered.append((q.start(), q.end()))

    def covered_pos(i: int) -> bool:
        return any(s <= i < e for s, e in covered)

    names = sorted(missing_names, key=len, reverse=True)
    recovered: list[dict] = []
    i = 0
    while i < len(text):
        if covered_pos(i):
            i += 1
            continue
        hit = next((nm for nm in names if text.startswith(nm, i)), None)
        if hit is None:
            i += 1
            continue
        recovered.append({
            "occ_id": f"recovered-{bid}-{i}",
            "name": hit,
            "start": i,
            "end": i + len(hit),
            "status": "undefined",
            "target_ids": [],
            "scope_label": "",
            "quoted": False,
            "text": text,
        })
        i += len(hit)
    return recovered


def _term_diffs(old: _Side, new: _Side
                ) -> tuple[list[dict], list[dict]]:
    """返回 (术语差异行, 直接绑定影响) ；后者供最短链计算。"""
    old_id2key, old_key2def = _def_semantic_map(old)
    new_id2key, new_key2def = _def_semantic_map(new)
    old_uses, new_uses = _uses_by_block(old), _uses_by_block(new)

    # 仅比较两侧都出现的块（preamble 恒在；条款块存在即存活）。
    # 块单侧缺失（条款删除 / 新增）由条款差异行表达，不重复报使用增减。
    common_blocks = (set(old_uses) & set(new_uses))
    diffs: list[dict] = []
    direct: list[dict] = []

    def block_pos(bid: str) -> int:
        return 0 if bid == "preamble" else new.position(bid)

    for bid in sorted(common_blocks,
                      key=lambda x: (x != "preamble", new.position(x))):
        ou, nu = old_uses[bid], new_uses[bid]
        # 找回旧版唯一绑定、新版因定义消失而未被扫描的无引号使用
        old_bound_unquoted = {
            u["name"] for u in ou
            if u["status"] == "ok" and not u.get("quoted")
        }
        new_scanned_names = {u["name"] for u in nu}
        recovered = _recover_unquoted_uses(
            new, bid, old_bound_unquoted - new_scanned_names)
        nu = nu + recovered

        # 块内按「同名第 k 次」对齐：可抵抗插入 / 删除导致的位移
        old_groups: dict[str, list[dict]] = {}
        new_groups: dict[str, list[dict]] = {}
        for u in ou:
            old_groups.setdefault(u["name"], []).append(u)
        for u in nu:
            new_groups.setdefault(u["name"], []).append(u)
        names = sorted(set(old_groups) | set(new_groups),
                       key=lambda nm: min(
                           [u["start"] for u in old_groups.get(nm, [])]
                           + [u["start"] for u in new_groups.get(nm, [])]))
        for name in names:
            ol, nl = old_groups.get(name, []), new_groups.get(name, [])
            for i in range(max(len(ol), len(nl))):
                o = ol[i] if i < len(ol) else None
                n = nl[i] if i < len(nl) else None
                ob = _use_brief(o, old, old_id2key, old_key2def)
                nb = _use_brief(n, new, new_id2key, new_key2def)

                kind = None
                if o and not n:
                    kind = "term_use_removed"
                    message = f"术语“{name}”的该处在新版中消失"
                elif n and not o:
                    kind = "term_use_added"
                    message = f"新版新增术语“{name}”的使用"
                elif o and n:
                    if ob["status"] == "ok" and nb["status"] == "undefined":  # type: ignore[index]
                        kind = "term_binding_lost"
                        d = ob["bound_definition"]  # type: ignore[index]
                        message = (
                            f"术语“{name}”绑定丢失：原绑定的"
                            f"{d['scope_label']}定义（{d['label'] or '前置说明'}）"
                            f"在新版不可见，本处变为未定义"
                        )
                    elif ob["status"] == "ok" and nb["status"] == "ambiguous":  # type: ignore[index]
                        kind = "term_binding_ambiguous"
                        d = ob["bound_definition"]  # type: ignore[index]
                        message = (
                            f"术语“{name}”绑定改变：原{ d['scope_label']}定义"  # type: ignore[index]
                            f"（{d['label'] or '前置说明'}）在新版失配，"  # type: ignore[index]
                            f"本处命中 {len(nb['candidate_definitions'])} 个同名定义，变歧义"  # type: ignore[index]
                        )
                    elif (ob["status"] == "ok" and nb["status"] == "ok"
                          and ob["bound_definition"] != nb["bound_definition"]):  # type: ignore[index]
                        kind = "term_binding_changed"
                        od, nd = ob["bound_definition"], nb["bound_definition"]  # type: ignore[index]
                        message = (
                            f"术语“{name}”改绑："
                            f"{od['scope_label']}定义（{od['label'] or '前置说明'}）"  # type: ignore[index]
                            f" → {nd['scope_label']}定义（{nd['label'] or '前置说明'}）"  # type: ignore[index]
                        )
                if kind is None:
                    continue

                # 最短链根因：旧绑定定义所在条款（可能在新版被删 / 被编辑）
                terminal = None
                if kind in ("term_binding_lost", "term_binding_ambiguous"):
                    d = ob["bound_definition"]  # type: ignore[index]
                    terminal = d["block_id"] if d else None
                elif kind == "term_binding_changed":
                    d = ob["bound_definition"]  # type: ignore[index]
                    terminal = d["block_id"] if d else None

                row = {
                    "kind": kind,
                    "block_id": bid,
                    "in_preamble": bid == "preamble",
                    "name": name,
                    "message": message,
                    "old": ob,
                    "new": nb,
                    "terminal_id": terminal,
                    "sort_pos": block_pos(bid),
                    "sort_start": (n or o)["start"],
                }
                diffs.append(row)
                if bid != "preamble" and kind.startswith("term_binding"):
                    direct.append(row)

    diffs.sort(key=lambda d: (d["sort_pos"], d["sort_start"],
                              _TERM_KIND_ORDER[d["kind"]]))
    for d in diffs:
        d.pop("sort_pos", None)
        d.pop("sort_start", None)
    return diffs, direct


# ---------------------------------------------------------------- 最短受影响链

def _clause_brief(side: _Side, cid: str) -> dict:
    c = side.by_id.get(cid)
    return {
        "id": cid,
        "label": side.issued_label(cid) or (c.label if c else cid),
        "heading": c.heading if c else "",
    }


def _impact_chains(old: _Side, new: _Side, ref_diffs: list[dict],
                   term_direct: list[dict]) -> list[dict]:
    """新版存活引用图：SCC 收缩 + 反向多源 BFS 最短链。"""
    survivors = list(new.ref.get("order", []))
    survivors_set = set(survivors)

    edges: dict[str, set[str]] = {cid: set() for cid in survivors}
    for cid in survivors:
        for occ in new.ref_occurrences(cid):
            for tid in occ["targets"]:
                # 与主分析一致：区间包含自身不计边，显式自引用计自环
                if tid in survivors_set and not (
                        occ["kind"] == "range" and tid == cid):
                    edges[cid].add(tid)

    comp = _scc(survivors, edges, new.doc)
    members: dict[int, list[str]] = {}
    for cid in survivors:
        members.setdefault(comp[cid], []).append(cid)
    for mem in members.values():
        mem.sort(key=lambda x: new.doc[x])
    comps = sorted(members, key=lambda k: new.doc[members[k][0]])
    cdoc = {k: new.doc[members[k][0]] for k in comps}
    cpos = {k: min(new.struct[m]["new_position"] for m in members[k])
            for k in comps}
    cyclic = {
        k: len(mem) > 1 or mem[0] in edges[mem[0]]
        for k, mem in members.items()
    }
    cedges: dict[int, set[int]] = {k: set() for k in comps}
    for u, vs in edges.items():
        for v in vs:
            if comp[u] != comp[v]:
                cedges[comp[u]].add(comp[v])

    # ---- 直接影响（分量内引用失效按被删目标去重；术语按出现处保留）----
    direct_comp: dict[int, list[dict]] = {}
    merged_ref: dict[int, set] = {}
    terminals_dead: dict[int, set[str]] = {}   # 新版不存活的根因条款
    terminals_alive: dict[int, set[str]] = {}  # 新版存活但被改（定义移除等）

    def add_direct(owner: str, detail: dict,
                   dead: set[str], alive: set[str]) -> None:
        k = comp[owner]
        if detail["kind"].startswith("ref_"):
            key = ("ref", detail.get("lost_ids"))
            if key in merged_ref.setdefault(k, set()):
                return
            merged_ref[k].add(key)
        direct_comp.setdefault(k, []).append(
            dict(detail, owner_id=owner))
        terminals_dead.setdefault(k, set()).update(dead)
        terminals_alive.setdefault(k, set()).update(alive)

    for d in ref_diffs:
        owner = d["clause_id"]
        if owner not in survivors_set:
            continue
        lost = [t for t in d["targets_removed"]
                if t not in survivors_set]
        if not lost:
            continue
        gained_alive = [t for t in d["targets_added"]
                        if t in survivors_set]
        if gained_alive:
            msg = (f"引用改指：「{(d['old'] or {}).get('text') or ''}」"
                   f"原目标在新版失效，现指向 "
                   + "、".join(new.issued_label(t) for t in gained_alive))
        else:
            msg = ("引用目标在新版被删除："
                   + "、".join((old.issued_label(t) or t) for t in lost))
        add_direct(owner, {
            "kind": "ref_target_deleted",
            "message": msg,
            "start": (d["old"] or d["new"] or {}).get("start", 0),
            "lost_ids": tuple(lost),
        }, dead=set(lost), alive=set())

    for d in term_direct:
        owner = d["block_id"]
        if owner not in survivors_set:
            continue
        terminal = d.get("terminal_id")
        dead, alive = set(), set()
        if terminal and terminal != "preamble":
            if terminal in survivors_set:
                alive.add(terminal)
            elif terminal in new.by_id or terminal in old.by_id:
                dead.add(terminal)
        add_direct(owner, {
            "kind": d["kind"],
            "message": d["message"],
            "start": (d["new"] or d["old"] or {}).get("start", 0),
            "lost_ids": (),
            "name": d["name"],
        }, dead=dead, alive=alive)

    roots_comp = set(direct_comp)

    # ---- 收缩 DAG 上反向多源 BFS ----
    crev: dict[int, list[int]] = {k: [] for k in comps}
    for u, vs in cedges.items():
        for v in vs:
            crev[v].append(u)
    for vs in crev.values():
        vs.sort(key=lambda x: cpos[x])

    dist: dict[int, int] = {k: 0 for k in roots_comp}
    dq: deque[int] = deque(sorted(roots_comp, key=lambda x: cpos[x]))
    while dq:
        cur = dq.popleft()
        for nxt in crev[cur]:
            if nxt not in dist:
                dist[nxt] = dist[cur] + 1
                dq.append(nxt)

    best: dict[int, list[int]] = {k: [k] for k in roots_comp}
    max_dist = max(dist.values(), default=0)
    for level in range(1, max_dist + 1):
        for n in sorted((n for n in dist if dist[n] == level),
                        key=lambda x: cpos[x]):
            preds = [p for p in cedges[n] if dist.get(p) == level - 1]
            chosen = min(preds, key=lambda p: [cpos[q] for q in best[p]])
            best[n] = [n] + best[chosen]

    def step(k: int, terminal: bool = False,
             terminal_id: str | None = None,
             alive_now: bool = False) -> dict:
        if terminal_id is not None:
            return {
                "kind": "clause", "cycle": False, "terminal": True,
                "alive_new": alive_now,
                "members": [_root_brief(terminal_id)],
            }
        mem = members[k]
        return {
            "kind": "group" if len(mem) > 1 else "clause",
            "cycle": cyclic[k], "terminal": terminal, "alive_new": True,
            "members": [_clause_brief(new, x) for x in mem],
        }

    def _root_brief(cid: str) -> dict:
        oc = old.by_id.get(cid)
        nc = new.by_id.get(cid)
        return {
            "id": cid,
            "label": new.issued_label(cid)
            or (nc.label if nc else oc.label if oc else cid),
            "heading": (nc.heading if nc else oc.heading if oc else ""),
            "old_present": oc is not None,
            "new_present": nc is not None,
            "alive_new": cid in survivors_set,
        }

    impacts: list[dict] = []
    for k in sorted(dist, key=lambda x: (dist[x], cpos[x])):
        mem = members[k]
        chain = [step(x) for x in best[k]]
        root_k = best[k][-1]
        for tid in sorted(terminals_dead.get(root_k, set()),
                          key=lambda x: new.doc.get(x, old.doc.get(x, 0))):
            chain.append(step(0, terminal=True, terminal_id=tid,
                              alive_now=False))
        for tid in sorted(terminals_alive.get(root_k, set()),
                          key=lambda x: new.doc.get(x, 0)):
            chain.append(step(0, terminal=True, terminal_id=tid,
                              alive_now=True))

        dets = sorted(
            direct_comp.get(k, []),
            key=lambda d: (new.doc.get(d["owner_id"], 10 ** 9),
                           d["start"], 0 if d["kind"].startswith("ref") else 1))

        root_causes = []
        for tid in sorted(terminals_dead.get(root_k, set())
                          | terminals_alive.get(root_k, set()),
                          key=lambda x: (new.doc.get(x, 10 ** 9),
                                         old.doc.get(x, 0))):
            root_causes.append({
                "clause_id": tid,
                "alive_new": tid in survivors_set,
                "old": old.clause_loc(tid),
                "new": new.clause_loc(tid),
            })

        impacts.append({
            "clause_id": mem[0],
            "member_ids": list(mem),
            "cycle_group": len(mem) > 1,
            "cycle": cyclic[k],
            "label": ("循环组（" + "、".join(new.issued_label(x) for x in mem)
                      + "）") if len(mem) > 1
            else new.issued_label(mem[0]),
            "heading": " ↔ ".join(dict.fromkeys(
                new.by_id[x].heading for x in mem if new.by_id[x].heading)),
            "distance": dist[k],
            "chain": chain,
            "root_causes": root_causes,
            "details": [{
                "kind": d["kind"],
                "message": d["message"],
                "owner_id": d["owner_id"],
                "start": d["start"],
            } for d in dets],
        })

    # 前置说明中的绑定改变（不构成引用图节点）：单独成行放在最前
    pre_dets = [d for d in term_direct if d["block_id"] == "preamble"]
    if pre_dets:
        chain = [{
            "kind": "preamble", "cycle": False, "terminal": False,
            "alive_new": True,
            "members": [{"id": "preamble", "label": "前置说明", "heading": ""}],
        }]
        for d in pre_dets:
            tid = d.get("terminal_id")
            if tid and tid in old.by_id:
                chain.append(step(0, terminal=True, terminal_id=tid,
                                  alive_now=tid in survivors_set))
        impacts.insert(0, {
            "clause_id": "preamble",
            "member_ids": ["preamble"],
            "cycle_group": False,
            "cycle": False,
            "label": "前置说明",
            "heading": "",
            "distance": 0,
            "chain": chain,
            "root_causes": [],
            "details": [{
                "kind": d["kind"],
                "message": d["message"],
                "owner_id": "preamble",
                "start": (d["new"] or d["old"] or {}).get("start", 0),
            } for d in sorted(pre_dets,
                              key=lambda x: (x["new"] or x["old"])["start"])],
        })
    return impacts


# ---------------------------------------------------------------- 主入口

def _side_meta(s: _Side) -> dict:
    return {
        "id": s.id, "version_no": s.version_no, "revision": s.revision,
        "title": s.title, "issued_at": s.issued_at,
    }


def _side_snapshot(s: _Side) -> dict:
    """供前端并排渲染的不可变快照（引用视角，含原文标记与删除置灰）。"""
    return {
        "id": s.id,
        "version_no": s.version_no,
        "preamble": s.preamble,
        "order": list(s.ref.get("order", [])),
        "clauses": s.ref.get("clauses", []),
        "result": s.ref.get("result", []),
        "deleted_clauses": s.ref.get("deleted_clauses", []),
    }


def audit_versions(old_row: dict, new_row: dict) -> dict[str, Any]:
    if old_row["id"] == new_row["id"]:
        raise ValueError("请选择两个不同的版本")
    if old_row["draft_id"] != new_row["draft_id"]:
        raise ValueError("只能比较同一草稿签发的两个版本")

    old, new = _load_side(old_row), _load_side(new_row)

    clause_diffs = _clause_diffs(old, new)
    ref_diffs = _ref_diffs(old, new)
    term_diffs, term_direct = _term_diffs(old, new)
    impacts = _impact_chains(old, new, ref_diffs, term_direct)

    preamble_changed = old.preamble != new.preamble

    counts = {
        "clause_total": len(clause_diffs),
        "clause_body": sum(1 for d in clause_diffs
                           if d["kind"] == "body_changed"),
        "clause_reorder": sum(1 for d in clause_diffs
                              if d["kind"] == "reorder"),
        "clause_numbering_only": sum(1 for d in clause_diffs
                                     if d["kind"] == "numbering_only"),
        "clause_removed": sum(1 for d in clause_diffs
                              if d["kind"] in ("removed", "lifecycle_deleted")),
        "clause_added": sum(1 for d in clause_diffs
                            if d["kind"] in ("added", "lifecycle_restored")),
        "ref_changed": len(ref_diffs),
        "ref_target_changed": sum(1 for d in ref_diffs if d["target_changed"]),
        "term_changed": len(term_diffs),
        "term_binding_changed": sum(
            1 for d in term_diffs
            if d["kind"].startswith("term_binding")),
        "impacts": len(impacts),
        "impact_cycles": sum(1 for i in impacts if i["cycle"]),
        "preamble_changed": preamble_changed,
    }

    return {
        "old": _side_meta(old),
        "new": _side_meta(new),
        "counts": counts,
        "preamble_diff": {
            "changed": preamble_changed,
            "old_present": bool(old.preamble),
            "new_present": bool(new.preamble),
        } if preamble_changed else None,
        "clause_diffs": clause_diffs,
        "ref_diffs": ref_diffs,
        "term_diffs": term_diffs,
        "impacts": impacts,
        "sides": {"old": _side_snapshot(old), "new": _side_snapshot(new)},
    }
