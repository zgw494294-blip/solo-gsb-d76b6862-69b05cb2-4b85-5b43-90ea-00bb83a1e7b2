"""批注迁移核心逻辑。

偏移约定：所有 start/end 均为 Unicode 码点偏移，区间为左闭右开 [start, end)。
Python 字符串按码点索引，因此切片结果天然符合该约定。
"""

from __future__ import annotations

import difflib

CONTEXT_LEN = 20  # 命中位置前后各取的码点数

# 状态取值
SHIFTED = "shifted"              # 完整落在同一匹配块，直接平移
MATCHED = "matched"              # 新版中唯一命中批注原文
DISAMBIGUATED = "disambiguated"  # 多处命中，上下文距离最小者胜出
MANUAL = "manual"                # 人工选择候选
ABANDONED = "abandoned"          # 人工标记废弃
PENDING = "pending"              # 待确认（命中并列或无命中）


class DecisionError(ValueError):
    """人工决策不合法（如所选区间不在候选列表中）。"""


def levenshtein(a: str, b: str) -> int:
    """按码点计算两个字符串的 Levenshtein 编辑距离。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def find_all(haystack: str, needle: str) -> list[int]:
    """返回 needle 在 haystack 中所有（含重叠）出现的码点起点。"""
    starts = []
    pos = haystack.find(needle)
    while pos != -1:
        starts.append(pos)
        pos = haystack.find(needle, pos + 1)
    return starts


def _contexts(text: str, start: int, end: int) -> tuple[str, str]:
    """取区间 [start, end) 前后各 CONTEXT_LEN 个码点作为上下文。"""
    return text[max(0, start - CONTEXT_LEN):start], text[end:end + CONTEXT_LEN]


def migrate_annotations(old_text: str, new_text: str, annotations: list[dict]) -> list[dict]:
    """逐条迁移批注，返回与输入同序的结果列表。"""
    matcher = difflib.SequenceMatcher(None, old_text, new_text, autojunk=False)
    blocks = [b for b in matcher.get_matching_blocks() if b.size > 0]
    return [_migrate_one(old_text, new_text, ann, blocks) for ann in annotations]


def _migrate_one(old_text: str, new_text: str, ann: dict, blocks: list) -> dict:
    start, end = ann["start"], ann["end"]
    length = end - start
    excerpt = old_text[start:end]
    base = {"id": ann["id"], "start": start, "end": end, "text": excerpt}

    # 1) 批注完整落在同一匹配块内 → 直接平移
    for blk in blocks:
        if blk.a <= start and end <= blk.a + blk.size:
            new_start = blk.b + (start - blk.a)
            return {**base, "status": SHIFTED,
                    "new_start": new_start, "new_end": new_start + length,
                    "candidates": [], "detail": "完整落在匹配块内，直接平移"}

    # 2) 在新版中搜索批注原文
    hits = find_all(new_text, excerpt)
    old_before, old_after = _contexts(old_text, start, end)

    def make_candidate(hit: int) -> dict:
        new_before, new_after = _contexts(new_text, hit, hit + length)
        dist = levenshtein(old_before, new_before) + levenshtein(old_after, new_after)
        return {"start": hit, "end": hit + length, "distance": dist}

    if len(hits) == 1:
        c = make_candidate(hits[0])
        return {**base, "status": MATCHED,
                "new_start": c["start"], "new_end": c["end"],
                "candidates": [c], "detail": "新版中唯一命中批注原文"}

    if len(hits) > 1:
        candidates = [make_candidate(h) for h in hits]
        best = min(c["distance"] for c in candidates)
        winners = [c for c in candidates if c["distance"] == best]
        if len(winners) == 1:
            w = winners[0]
            return {**base, "status": DISAMBIGUATED,
                    "new_start": w["start"], "new_end": w["end"],
                    "candidates": candidates,
                    "detail": f"{len(hits)} 处命中，上下文距离 {best} 最小者胜出"}
        return {**base, "status": PENDING, "new_start": None, "new_end": None,
                "candidates": candidates,
                "detail": f"{len(hits)} 处命中且最小上下文距离 {best} 并列，待确认"}

    return {**base, "status": PENDING, "new_start": None, "new_end": None,
            "candidates": [], "detail": "新版中未找到批注原文，待确认"}


def apply_decisions(results: list[dict], decisions: dict[str, dict]) -> tuple[list[dict], list]:
    """把人工决策应用到迁移结果上。

    返回 (exported, unresolved)：exported 为可导出条目；
    unresolved 为仍为待确认且未给出决策的批注 id（非空时调用方应拒绝导出）。
    """
    exported, unresolved = [], []
    for r in results:
        if r["status"] != PENDING:
            exported.append({"id": r["id"], "start": r["new_start"],
                             "end": r["new_end"], "status": r["status"]})
            continue
        d = decisions.get(str(r["id"]))
        if d is None:
            unresolved.append(r["id"])
        elif d.get("action") == "abandon":
            exported.append({"id": r["id"], "start": None, "end": None, "status": ABANDONED})
        elif d.get("action") == "choose":
            hit = (d.get("start"), d.get("end"))
            if not any((c["start"], c["end"]) == hit for c in r["candidates"]):
                raise DecisionError(f"批注 {r['id']} 选择的区间 {hit} 不在候选列表中")
            exported.append({"id": r["id"], "start": d["start"], "end": d["end"], "status": MANUAL})
        else:
            raise DecisionError(f"批注 {r['id']} 的决策动作未知：{d.get('action')}")
    return exported, unresolved
