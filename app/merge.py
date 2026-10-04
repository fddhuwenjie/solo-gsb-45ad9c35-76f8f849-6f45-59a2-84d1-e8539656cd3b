"""版本差异审计 → 应用到当前草稿（旧版基线 / 新版候选的三方合并）。

以审计旧版（``old``）为基线、新版（``new``）为候选，当前草稿（``draft``）
为工作副本，用户在审计页逐条勾选要带入的变更：

- ``body``：条款正文（候选快照里该条款的整块原文）；
- ``structure``：父级层级 / 同父兄弟间顺序（同时也是新增条款的落位与
  删除条款的移出）；
- ``lifecycle``：旧活新删 → 删除（含级联），旧删新活 → 恢复；
- ``presence``：条款在候选中整体新增 / 整体移除（原路径 ID 仅一侧存在）。

合并在**合并后的文本、顺序、删除状态**上重新跑解析、引用改写与术语绑定，
绝不拼接候选版本签发时的引用改写文本或术语绑定结果。

冲突规则（冲突项禁止覆盖，提交时携带任一冲突即整体拒绝）：

1. 当前草稿相对旧版已改动**同一条款**的正文 / 层级顺序 / 删除状态
   （任一维度）→ 该条款的所有勾选项冲突；
2. 该条款的**任一祖先条款**在草稿中相对旧版有改动 → 所有勾选项冲突
   （祖先局部术语定义、级联删除与层级都可能改变子树语义）；
3. 勾选删除 / 移除时，该条款**子树内任一条款**已被草稿相对旧版改动
   → 删除 / 移除项冲突（避免静默抹掉草稿对子条款的修改）。

编号仅随重排变化（``numbering_only``）无需勾选：合并后由引用重排统一
重算，不产生可带入的独立变更。
"""
from __future__ import annotations

from typing import Any

from .audit import (
    _Side,
    _clause_diffs,
    _load_side,
    _parent_slug,
    _side_snapshot,
    _sibling_ranks,
)
from .parser import parse_contract, split_blocks
from .references import analyze, cascade_deleted
from .terms import review_terms

# 可选维度（请求中的白名单与展示顺序）
DIMENSIONS = ("body", "structure", "lifecycle", "presence")
_DIMENSION_LABELS = {
    "body": "正文",
    "structure": "父子顺序 / 层级",
    "lifecycle": "删除 / 恢复",
    "presence": "新增 / 移除",
}
# 审计条款差异类型 -> 候选维度（body_changed / reorder 还需按 flags 细分）
_KIND_DIMENSIONS: dict[str, tuple[str, ...]] = {
    "body_changed": ("body",),
    "reorder": ("structure",),
    "removed": ("presence",),
    "added": ("presence",),
    "lifecycle_deleted": ("lifecycle", "body"),
    "lifecycle_restored": ("lifecycle", "body"),
}


# ---------------------------------------------------------------- 快照视图

def version_view(row: dict) -> _Side:
    return _load_side(row)


def draft_view(content: str, order: list[str], deleted: list[str]) -> _Side:
    """把当前草稿构造成与版本快照同构的工作视图（行 id 用负数占位）。"""
    preamble, clauses = parse_contract(content)
    known = {c.id for c in clauses}
    order = [cid for cid in order if cid in known]
    deleted = [cid for cid in deleted if cid in known]
    ref_result = analyze(clauses, order, deleted)
    deleted_set = cascade_deleted(clauses, deleted)
    term_result = review_terms(preamble, clauses, deleted_set)
    return _Side(
        version_id=0, version_no=0, revision=0, title="", issued_at=0,
        content=content, ref_result=ref_result, term_result=term_result,
    )


# ---------------------------------------------------------------- 差异条目

def _ancestor_paths(path: tuple[int, ...]) -> list[tuple[int, ...]]:
    return [path[:i] for i in range(1, len(path))]


def _edits(old: _Side, draft: _Side) -> dict[str, dict[str, bool]]:
    """相对旧版有改动的条款 id -> {body, structure, lifecycle}。

    对齐口径与版本差异审计一致：正文逐字比较；层级 / 顺序仅在「旧版与
    草稿共同存活」的兄弟间比较相对名次；生命周期看存活翻转。
    """
    common = set(old.by_id) & set(draft.by_id)
    common_alive = {cid for cid in common
                    if cid in old.alive and cid in draft.alive}
    rank_old = _sibling_ranks(old, common_alive)
    rank_draft = _sibling_ranks(draft, common_alive)
    out: dict[str, dict[str, bool]] = {}
    for cid in common:
        body = old.by_id[cid].text != draft.by_id[cid].text
        if cid in common_alive:
            structure = rank_old.get(cid) != rank_draft.get(cid)
            lifecycle = False
        else:
            # 一侧删除、另一侧存活：删除 / 恢复本身即生命周期改动；
            # 层级名次不再可比，structure 置 False（lifecycle 已拦截）。
            structure = False
            lifecycle = (cid in old.alive) != (cid in draft.alive)
        if body or structure or lifecycle:
            out[cid] = {"body": body, "structure": structure,
                        "lifecycle": lifecycle}
    return out


def _locs_for_item(kind: str, cid: str,
                   old: _Side, new: _Side, draft: _Side) -> dict:
    def loc(side: _Side) -> dict | None:
        if cid not in side.by_id:
            return None
        return side.clause_loc(cid)
    return {"old": loc(old), "new": loc(new), "draft": loc(draft)}


def build_items(old: _Side, new: _Side, draft: _Side) -> list[dict]:
    """构造逐条「可带入变更」清单（含冲突判定与三侧定位）。"""
    clause_diffs = _clause_diffs(old, new)
    edited = _edits(old, draft)

    # 路径 -> 草稿中该路径的条款 id（祖先判定按路径，避免重复编号歧义）
    draft_path_ids: dict[tuple[int, ...], str] = {}
    for c in draft.clauses:
        draft_path_ids.setdefault(c.path, c.id)
    old_path_ids: dict[tuple[int, ...], str] = {}
    for c in old.clauses:
        old_path_ids.setdefault(c.path, c.id)

    items: list[dict] = []
    for d in clause_diffs:
        kind = d["kind"]
        dims = list(_KIND_DIMENSIONS.get(kind, ()))
        # 正文改变行同时有层级 / 顺序变化时，才额外开放 structure 勾选；
        # reorder 行若同时带正文变化（理论上正文优先成类，此处仅防御）开放 body。
        flags = d["flags"]
        if kind == "body_changed" and (
                flags["hierarchy_changed"] or flags["order_changed"]):
            dims.append("structure")
        if kind == "reorder" and flags["body_changed"]:
            dims.insert(0, "body")
        if not dims:
            # numbering_only：编号合并后统一重算，无可勾选项
            continue
        cid = d["clause_id"]
        conflict_reasons: list[str] = []
        dim_conflicts: dict[str, list[str]] = {dim: [] for dim in dims}

        clause = new.by_id.get(cid) or old.by_id.get(cid)
        path = clause.path if clause else None

        # 规则 1：草稿已改同一条款（任一维度）→ 全部勾选维度冲突
        self_edit = edited.get(cid)
        if self_edit:
            which = "、".join(_DIMENSION_LABELS[k]
                              for k, v in self_edit.items() if v)
            reason = f"当前草稿相对旧版已改动本条款（{which}）"
            conflict_reasons.append(reason)
            for dim in dims:
                dim_conflicts[dim].append(reason)

        # 规则 2：任一祖先在草稿中相对旧版有改动 → 全部维度冲突
        if path is not None:
            for ap in _ancestor_paths(path):
                aid = old_path_ids.get(ap)
                if aid and aid in edited:
                    changed = edited[aid]
                    which = "、".join(_DIMENSION_LABELS[k]
                                      for k, v in changed.items() if v)
                    reason = (f"父级条款 {old.issued_label(aid) or aid} "
                              f"已在当前草稿中改动（{which}）")
                    conflict_reasons.append(reason)
                    for dim in dims:
                        dim_conflicts[dim].append(reason)

        # 规则 3（仅删除 / 移除类勾选）：子树内有草稿改动 → 该项冲突
        remove_dims = [x for x in ("lifecycle", "presence") if x in dims]
        # lifecycle 仅在「旧活新删」方向是删除；恢复不带删除语义
        is_delete = kind in ("removed", "lifecycle_deleted")
        if is_delete and path is not None:
            subtree_touched: list[str] = []
            for dc_id, ed in edited.items():
                dc = draft.by_id.get(dc_id) or old.by_id.get(dc_id)
                if dc and len(dc.path) > len(path) \
                        and dc.path[:len(path)] == path:
                    subtree_touched.append(dc_id)
            for dc_id in sorted(subtree_touched,
                                key=lambda x: (draft.doc.get(x, 10 ** 9),
                                               old.doc.get(x, 10 ** 9))):
                dc = draft.by_id.get(dc_id) or old.by_id.get(dc_id)
                reason = (f"子条款 {dc.label} 已在当前草稿中改动，"
                          f"删除 / 移除将覆盖其修改")
                conflict_reasons.append(reason)
                for dim in remove_dims:
                    dim_conflicts[dim].append(reason)

        # 生命周期方向（仅用于文案 / 定位）
        lifecycle_direction = None
        if kind == "lifecycle_deleted":
            lifecycle_direction = "delete"
        elif kind == "lifecycle_restored":
            lifecycle_direction = "restore"
        elif kind == "removed":
            lifecycle_direction = "remove"
        elif kind == "added":
            lifecycle_direction = "add"

        items.append({
            "clause_id": cid,
            "kind": kind,
            "message": d["message"],
            "flags": d["flags"],
            "lifecycle_direction": lifecycle_direction,
            "dimensions": [
                {
                    "dim": dim,
                    "label": _DIMENSION_LABELS[dim],
                    "conflict": bool(dim_conflicts[dim]),
                    "conflict_reasons": list(dict.fromkeys(
                        dim_conflicts[dim])),
                }
                for dim in dims
            ],
            "conflict": bool(conflict_reasons),
            "conflict_reasons": list(dict.fromkeys(conflict_reasons)),
            "locs": _locs_for_item(kind, cid, old, new, draft),
            "sort_pos": (new.position(cid) if cid in new.by_id
                         else 10 ** 6 + old.doc.get(cid, 0)),
        })

    items.sort(key=lambda it: (it["sort_pos"], it["clause_id"]))
    for it in items:
        it.pop("sort_pos", None)
    return items


def build_apply_preview(old_row: dict, new_row: dict,
                        draft: dict) -> dict[str, Any]:
    """只读预览：可勾选条目 + 冲突 + 三侧快照（不写任何状态）。"""
    old, new = version_view(old_row), version_view(new_row)
    dview = draft_view(draft["content"], draft["order"], draft["deleted"])
    items = build_items(old, new, dview)
    return {
        "draft_id": draft["id"],
        "draft_revision": draft["revision"],
        "old": {"id": old.id, "version_no": old.version_no,
                "revision": old.revision,
                "fingerprint": old_row["fingerprint"]},
        "new": {"id": new.id, "version_no": new.version_no,
                "revision": new.revision,
                "fingerprint": new_row["fingerprint"]},
        "counts": {
            "items": len(items),
            "conflict_items": sum(1 for it in items if it["conflict"]),
            "selectable_dimensions": sum(
                1 for it in items for dim in it["dimensions"]
                if not dim["conflict"]),
        },
        "items": items,
        "sides": {
            "old": _side_snapshot(old),
            "new": _side_snapshot(new),
            "draft": _side_snapshot(dview),
        },
    }


# ---------------------------------------------------------------- 文本合并

def _merge_content(content: str, draft: _Side, new: _Side,
                   accepted: dict[str, set[str]]) -> str:
    """在当前草稿文本上做外科式替换：保留所有未勾选块的原文与空白。

    每个条款至多一块：正文替换取候选整块（沿用本块原有的尾随换行）；
    移除删除整块（含尾随换行）；新增按候选解析顺序锚定到「同版前一条款」
    之后（锚点缺失或锚点本身将被移除则放到文末）。前置说明原样保留。
    """
    _, dblocks = split_blocks(content)
    # id -> (起点, 块末（含尾随空白）, 正文末（rstrip 换行后）)
    dspan: dict[str, tuple[int, int, int]] = {}
    for c in draft.clauses:
        b = next((bb for bb in dblocks if bb.start == c.start), None)
        if b is not None:
            dspan[c.id] = (b.start, b.end, b.content_end)

    _, nblocks = split_blocks(new.content)
    nraw: dict[str, str] = {}
    for c in new.clauses:
        b = next((bb for bb in nblocks if bb.start == c.start), None)
        if b is not None:
            nraw[c.id] = new.content[b.start:b.content_end]

    new_doc_ids = [c.id for c in new.clauses]

    def has(cid: str, dim: str) -> bool:
        return dim in accepted.get(cid, ())

    ops: list[tuple[int, int, str]] = []  # (start, end, replacement)

    # 1) 草稿中已存在的块：整体移除或正文整块替换
    for cid, (s, e, _ce) in dspan.items():
        dims = accepted.get(cid)
        if not dims:
            continue
        if "presence" in dims and cid not in new.by_id:
            ops.append((s, e, ""))                    # 候选整体移除
        elif "body" in dims and cid in new.by_id:
            ops.append((s, e, nraw[cid] + _trailing(content, s, e)))

    # 2) 候选存在、草稿文本不存在的块：新增 / 恢复落位
    def insert_after_anchor(cid: str) -> None:
        idx = new_doc_ids.index(cid)
        anchor_cid = next(
            (a for a in reversed(new_doc_ids[:idx]) if a in dspan), None)
        if anchor_cid is not None and not has(anchor_cid, "presence"):
            # 插到锚点正文末尾之后、其尾随换行之前：既保持换行归属，
            # 又不会把锚点的行首缩进带进新条款的正文。
            _, _ae, ace = dspan[anchor_cid]
            ops.append((ace, ace, "\n" + nraw[cid]))
        else:
            prefix = "" if (not content or content.endswith("\n")) else "\n"
            ops.append((len(content), len(content), prefix + nraw[cid] + "\n"))

    for cid in new_doc_ids:
        dims = accepted.get(cid)
        if not dims or cid in dspan:
            # 恢复的条款文本本就保留在草稿里（删除只是标记），无需插入
            continue
        if "presence" in dims or "lifecycle" in dims:
            insert_after_anchor(cid)

    # 3) 按起点倒序应用，字符偏移保持有效；同起点时先应用纯插入
    ops.sort(key=lambda op: (op[0], 1 if op[0] == op[1] else 0), reverse=True)
    out = content
    for s, e, rep in ops:
        out = out[:s] + rep + out[e:]
    return out


def _trailing(text: str, s: int, e: int) -> str:
    """原块区间内的纯尾随换行（整块替换正文时保留原有空白形态）。"""
    seg = text[s:e]
    return seg[len(seg.rstrip("\n")):]


# ---------------------------------------------------------------- 删除集 / 顺序

def _merge_deleted(draft: _Side, new: _Side,
                   accepted: dict[str, set[str]]) -> list[str]:
    """合并删除状态（返回直接删除列表；级联由 analyze 展开）。"""
    # 以草稿当前「直接删除」为基：取删除集里那些「父级未被删除」的根
    cascaded = cascade_deleted(draft.clauses,
                               [c.id for c in draft.clauses
                                if c.id in draft.deleted])
    roots: set[str] = set()
    for cid in cascaded:
        c = draft.by_id.get(cid)
        if c is None:
            continue
        parent = _parent_slug(cid, draft)
        if parent is None or parent not in cascaded:
            roots.add(cid)

    for cid, dims in accepted.items():
        in_new = cid in new.by_id and cid in new.alive
        if "lifecycle" in dims or "presence" in dims:
            if in_new:
                roots.discard(cid)      # 候选存活：恢复
            elif cid in new.by_id or "presence" in dims:
                roots.add(cid)          # 候选删除 / 移除：删除
    return sorted(roots)


def _merge_order(merged_content: str, merged_direct_deleted: list[str],
                 draft: _Side, new: _Side,
                 accepted: dict[str, set[str]]) -> list[str]:
    """在父子树上合并兄弟顺序，输出存活先序（再交后端规整 / 重编号）。"""
    _, mclauses = parse_contract(merged_content)
    m_dead = cascade_deleted(mclauses, merged_direct_deleted)
    malive = [c for c in mclauses if c.id not in m_dead]

    def node_paths(side_clauses) -> set[tuple[int, ...]]:
        """该侧出现过的全部树节点路径（含原文未单列的虚父节点）。"""
        out: set[tuple[int, ...]] = set()
        for c in side_clauses:
            for d in range(1, len(c.path) + 1):
                out.add(c.path[:d])
        return out

    def side_seq(side: _Side, parent: tuple[int, ...]
                 ) -> list[tuple[int, ...]]:
        """该侧在 parent 下按其存活先序排出的子节点路径序列。"""
        seen: list[tuple[int, ...]] = []
        for cid in side.ref.get("order", []):
            c = side.by_id.get(cid)
            if c and c.path[:-1] == parent and c.path not in seen:
                seen.append(c.path)
        return seen

    d_paths, n_paths = node_paths(draft.clauses), node_paths(new.clauses)

    def children_of(paths: set[tuple[int, ...]],
                    parent: tuple[int, ...]) -> list[tuple[int, ...]]:
        return sorted(q for q in paths if q[:-1] == parent)

    all_parents: set[tuple[int, ...]] = set()
    for q in d_paths | n_paths:
        all_parents.add(q[:-1])

    # 路径 -> 合并文本中的条款 id（重复路径取首条，与绑定语义一致）
    path_to_mid: dict[tuple[int, ...], str] = {}
    for c in mclauses:
        path_to_mid.setdefault(c.path, c.id)
    d_path_id: dict[tuple[int, ...], str] = {}
    for c in draft.clauses:
        d_path_id.setdefault(c.path, c.id)

    merged_seq: dict[tuple[int, ...], list[tuple[int, ...]]] = {}
    for parent in sorted(all_parents, key=lambda p: (len(p), p)):
        dseq, nseq_full = side_seq(draft, parent), side_seq(new, parent)
        kids = list(dict.fromkeys(
            children_of(d_paths, parent)
            + [q for q in children_of(n_paths, parent)
               if q not in d_paths]))

        def alive_node(q: tuple[int, ...]) -> bool:
            cid = path_to_mid.get(q)
            return bool(cid and cid not in m_dead)

        base = [q for q in dseq if q in kids and alive_node(q)]
        # 草稿侧没有、仅候选存在的新节点（新增条款 / 恢复子树）补入
        for q in nseq_full:
            if q in kids and alive_node(q) and q not in base:
                base.append(q)

        # 需要按候选重定位的节点：勾选 structure（或新增 / 恢复落位）
        moved: list[tuple[int, ...]] = []
        for q in nseq_full:
            cid = path_to_mid.get(q) or d_path_id.get(q)
            if not cid:
                continue
            dims = accepted.get(cid)
            if not dims:
                continue
            if "structure" in dims or "presence" in dims \
                    or ("lifecycle" in dims and cid in new.alive
                        and cid not in draft.alive):
                if q in kids and alive_node(q) and q not in moved:
                    moved.append(q)

        seq = list(base)
        for q in moved:
            if q in seq:
                seq.remove(q)
            idx = nseq_full.index(q)
            anchor = None
            for a in reversed(nseq_full[:idx]):
                if a in seq:
                    anchor = a
                    break
            if anchor is None:
                seq.insert(0, q)
            else:
                seq.insert(seq.index(anchor) + 1, q)
        merged_seq[parent] = seq

    # 按合并后的父子树先序输出存活条款 id（虚节点不产生 id）
    flat: list[str] = []

    def emit(path: tuple[int, ...]) -> None:
        own = path_to_mid.get(path)
        if own and own not in m_dead and own not in flat:
            flat.append(own)
        for q in merged_seq.get(path, []):
            if q != path:
                emit(q)

    for q in merged_seq.get((), []):
        emit(q)
    # 兜底：任何未被树覆盖的存活条款按解析序追加（防御性，正常不触发）
    for c in malive:
        if c.id not in flat:
            flat.append(c.id)
    return flat


# ---------------------------------------------------------------- 主入口

def _accepted_map(selections: list[dict]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for sel in selections:
        cid = sel.get("clause_id")
        dims = sel.get("dimensions") or []
        if not cid or not isinstance(dims, list):
            continue
        bad = [d for d in dims if d not in DIMENSIONS]
        if bad:
            raise ValueError(f"未知的合并维度：{'、'.join(bad)}")
        out.setdefault(cid, set()).update(dims)
    return out


def validate_selections(items: list[dict],
                        selections: list[dict]) -> dict[str, set[str]]:
    """校验请求选择：条目 / 维度必须存在，且不得勾选任何冲突维度。"""
    accepted = _accepted_map(selections)
    item_by_id = {it["clause_id"]: it for it in items}
    conflicts: list[dict] = []
    for cid, dims in accepted.items():
        it = item_by_id.get(cid)
        if it is None:
            raise ValueError(f"该差异条目不存在或不可应用：{cid}")
        allowed = {d["dim"]: d for d in it["dimensions"]}
        for dim in sorted(dims):
            if dim not in allowed:
                raise ValueError(
                    f"条款 {cid} 不支持带入「{_DIMENSION_LABELS.get(dim, dim)}」变更")
            if allowed[dim]["conflict"]:
                conflicts.append({
                    "clause_id": cid,
                    "dimension": dim,
                    "message": "、".join(allowed[dim]["conflict_reasons"]),
                })
    if conflicts:
        raise MergeConflict(conflicts)
    return accepted


class MergeConflict(Exception):
    """勾选了冲突维度：整体拒绝，不写任何部分结果。"""

    def __init__(self, conflicts: list[dict]):
        super().__init__("存在冲突的合并项")
        self.conflicts = conflicts


def apply_merge(old_row: dict, new_row: dict, draft_row: dict,
                selections: list[dict]) -> dict[str, Any]:
    """计算合并后的草稿四要素（不碰数据库；调用方负责事务与修订号校验）。"""
    old, new = version_view(old_row), version_view(new_row)
    draft = draft_view(draft_row["content"], draft_row["order"],
                       draft_row["deleted"])
    items = build_items(old, new, draft)
    accepted = validate_selections(items, selections)
    if not any(dims for dims in accepted.values()):
        raise ValueError("未勾选任何要带入的变更")

    content = _merge_content(draft_row["content"], draft, new, accepted)
    deleted = _merge_deleted(draft, new, accepted)
    order = _merge_order(content, deleted, draft, new, accepted)

    # —— 在合并后的文本、顺序、删除状态上重新解析 / 引用改写 / 术语绑定 ——
    preamble, clauses = parse_contract(content)
    ref_result = analyze(clauses, order, deleted)
    deleted_set = cascade_deleted(clauses, deleted)
    term_result = review_terms(preamble, clauses, deleted_set)

    # 规整后的存活顺序 / 完整删除集（与 /api/analyze 同口径）
    final_order = list(ref_result["order"])
    final_deleted = sorted(c.id for c in clauses if c.id in deleted_set)

    return {
        "title": draft_row["title"],
        "content": content,
        "order": final_order,
        "deleted": final_deleted,
        "term_ignores": list(draft_row.get("term_ignores") or []),
        "preamble": preamble,
        "ref_result": ref_result,
        "term_result": term_result,
        "applied": [
            {"clause_id": cid, "dimensions": sorted(dims)}
            for cid, dims in sorted(accepted.items())
        ],
        "items": items,
    }
