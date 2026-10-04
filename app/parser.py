"""条款层级编号解析。

支持两类编号：
1. 带「第…条」：第3条、第3.2条、第3.2.1条、第五条、第三条之二；
2. 纯编号列举：1. / 1、 / 1.2.3 （首位编号限制 < 100，避免把 2024.5 当成条款）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
CN_UNITS = {"十": 10, "百": 100, "千": 1000}


def cn_to_int(s: str) -> int | None:
    """中文数字转 int，覆盖 0～9999 的常见表达（十、二十三、一百零五、三千…）。"""
    if not s:
        return None
    total = 0
    section = 0          # 当前累计（遇 万/亿 可扩展，合同条款用不到，暂不处理）
    current = 0
    for ch in s:
        if ch in CN_DIGITS:
            current = CN_DIGITS[ch]
        elif ch in CN_UNITS:
            unit = CN_UNITS[ch]
            section += (current if current else 1) * unit
            current = 0
        else:
            return None
    return total + section + current


# 单个编号段：纯阿拉伯数字 或 中文数字（允许「之二」这种增补编号尾缀，单独成段）
_ARABIC_SEG = r"[0-9]+"
_CN_SEG = r"[零〇一二两三四五六七八九十百千]+"
_SUFFIX_SEG = r"[之]?" + _CN_SEG

# 一个完整编号 token，如 3、3.2、3.2.1、五、三之二
NUM_TOKEN = (
    rf"(?:{_ARABIC_SEG}(?:\.(?:{_ARABIC_SEG}))*"
    rf"|{_CN_SEG}(?:之{_CN_SEG})?)"
)
NUM_TOKEN_RE = re.compile(
    rf"^(?:{_ARABIC_SEG}(?:\.(?:{_ARABIC_SEG}))*"
    rf"|{_CN_SEG}(?:之{_CN_SEG})?)$"
)

# 条款起始标记：只在行首（允许前导空白）识别，避免把正文里的
# 「按第1条执行」误切成新条款。末尾用前瞻，不吞正文首字符。
_BLOCK_RE = re.compile(
    rf"(?:^|\n)(?P<lead>[ \t　]*)"
    rf"(?:第(?P<art>{NUM_TOKEN})条"
    rf"|(?P<plain>[0-9]{{1,3}}(?:\.[0-9]{{1,3}})*)"
    rf"(?P<sep>[、．.]?)(?=[^0-9\r\n.、．]|$))"
)


def parse_path(token: str) -> tuple[int, ...] | None:
    """把编号 token 解析为整数路径，如 '3.2' -> (3, 2)，'三之二' -> (3, 2)。"""
    if not token or not NUM_TOKEN_RE.match(token):
        return None
    parts: list[int] = []
    if token[0].isdigit():
        for seg in token.split("."):
            if not seg.isdigit():
                return None
            parts.append(int(seg))
    else:
        m = re.match(rf"({_CN_SEG})(?:之({_CN_SEG}))?$", token)
        if not m:
            return None
        head = cn_to_int(m.group(1))
        if head is None:
            return None
        parts.append(head)
        if m.group(2):
            tail = cn_to_int(m.group(2))
            if tail is None:
                return None
            parts.append(tail)
    return tuple(parts) if parts and all(p >= 0 for p in parts) else None


@dataclass
class Block:
    """一次成功匹配的条款标记（编号 token 级信息，正文尚未切出）。"""
    match: re.Match
    kind: str            # article | plain
    path: tuple[int, ...]
    label: str
    start: int           # 整块起点（跳过换行与前导空白）
    end: int             # 到下一条款起点（含尾随换行/空白）
    content_end: int     # rstrip 换行后的整块末尾（Clause.raw 用）


@dataclass
class Clause:
    id: str            # 稳定 ID：路径 slug，重复时由调用方加后缀
    path: tuple[int, ...]
    label: str         # 原始编号文本（第3.2条 / 1.2）
    kind: str          # article | plain
    text: str          # 编号之后的正文（不含编号标记）
    heading: str       # 正文首行（列表展示用）
    raw: str           # 含编号的整块原文（本条款到下一条款之间）
    start: int = 0     # 整块在原文中的字符起点（跳过前导空白）
    end: int = 0       # 到下一条款起点（含尾随换行；末条为文本长度）
    content_end: int = 0  # rstrip 换行后的整块末尾


def _make_id(path: tuple[int, ...]) -> str:
    return "c-" + "-".join(str(p) for p in path)


def _block_start(text: str, m: re.Match) -> int:
    """条款标记在行内的实际起点（跳过匹配到的换行与前导空白）。"""
    start = m.start() + (1 if text[m.start()] == "\n" else 0)
    return start + len(m.group("lead") or "")


def split_blocks(text: str) -> tuple[str, list[Block]]:
    """切出 (前置说明, 条款标记块列表)，块带在原文中的字符区间。

    与 :func:`parse_contract` 使用同一套匹配 / 过滤规则，区间供三方合并
    在不重排空白的前提下做外科式文本替换。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    matches = list(_BLOCK_RE.finditer(text))
    # 先筛出有效条款标记，伪匹配（如行首 100、年份串）留在正文中
    accepted: list[tuple[re.Match, str, tuple[int, ...], str]] = []
    for m in matches:
        art = m.group("art")
        if art is not None:
            path = parse_path(art)
            if path is None:
                continue
            accepted.append((m, "article", path, f"第{art}条"))
        else:
            token = m.group("plain")
            segs = tuple(int(s) for s in token.split("."))
            if segs[0] >= 100:
                continue
            accepted.append((m, "plain", segs, token + (m.group("sep") or "")))

    blocks: list[Block] = []
    for idx, (m, kind, path, label) in enumerate(accepted):
        block_start = _block_start(text, m)
        block_end = (_block_start(text, accepted[idx + 1][0])
                     if idx + 1 < len(accepted) else len(text))
        blocks.append(Block(
            match=m, kind=kind, path=path, label=label,
            start=block_start, end=block_end,
            content_end=block_end
            - (len(text[block_start:block_end])
               - len(text[block_start:block_end].rstrip("\n"))),
        ))

    first_start = _block_start(text, accepted[0][0]) if accepted else len(text)
    preamble = text[:first_start].strip()
    return preamble, blocks


def parse_contract(text: str) -> tuple[str, list[Clause]]:
    """切分合同文本，返回 (前置说明 preamble, 条款列表)。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    preamble, blocks = split_blocks(text)
    clauses: list[Clause] = []
    used_ids: set[str] = set()

    for b in blocks:
        m = b.match
        body = text[m.end():b.end]
        body = body.lstrip(" \t　")
        heading = body.strip().split("\n", 1)[0].strip()
        if len(heading) > 40:
            heading = heading[:40] + "…"

        cid = _make_id(b.path)
        if cid in used_ids:
            n = 2
            while f"{cid}-{n}" in used_ids:
                n += 1
            cid = f"{cid}-{n}"
        used_ids.add(cid)

        clauses.append(Clause(
            id=cid, path=b.path, label=b.label, kind=b.kind,
            text=body, heading=heading,
            raw=text[b.start:b.content_end].rstrip("\n"),
            start=b.start, end=b.end, content_end=b.content_end,
        ))

    return preamble, clauses
