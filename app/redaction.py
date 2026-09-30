"""可分享文本的遮盖导出核心逻辑。

偏移约定：所有 start/end 均为 Unicode 码点偏移，区间为左闭右开
[start, end)；Python 字符串按码点索引，因此切片结果天然符合该约定。

给定文本、批注（唯一 id、start、end、label）与需遮盖的标签列表：

- **命中批注**：label 属于遮盖标签集合的批注，其区间自身**不导出**；
- **合并遮盖区**：所有命中区间只要**重叠或相接**（前一个的 end 等于
  后一个的 start 也算）就合并为一个遮盖区；
- **文本替换**：每个合并区在导出文本中整体替换为固定标记 ``MARK``
  （「[遮盖]」），遮盖区之间未被覆盖的可见文字原样保留；
- **其余批注**：非命中批注跨遮盖区时按遮盖区切成多个**可见片段**，
  完全被遮盖的批注舍弃；片段按**原批注顺序、同批注内按原文位置**
  排列，保留原 label，并带 ``source_id``（原批注 id）与从 1 开始的
  片段序号 ``fragment``；
- 片段在新文本中的码点范围 ``[start, end)`` 所覆盖的文字必须是原文
  的可见部分，不含遮盖标记；
- **无命中**（没有任何批注的 label 在遮盖标签列表中）时，文本与
  批注范围完全不变（此时其余批注各导出一个序号为 1 的完整片段）。

遮盖标签列表不合法（元素不是非空字符串）抛出 ``RedactionError``。
"""

from __future__ import annotations

MARK = "[遮盖]"  # 每个合并遮盖区的统一替换标记（4 个码点）


class RedactionError(ValueError):
    """遮盖标签列表不合法（元素不是非空字符串）。"""


def validate_redact_labels(labels) -> list[str]:
    """校验需遮盖的标签列表。

    必须是列表，且每个元素都是非空字符串；标签按首次出现去重并保持
    声明顺序。任一元素非法抛出 RedactionError。
    """
    if not isinstance(labels, list):
        raise RedactionError(f"遮盖标签列表必须是数组：{type(labels).__name__}")
    norm: list[str] = []
    seen: set[str] = set()
    for lab in labels:
        if not isinstance(lab, str) or not lab:
            raise RedactionError(f"遮盖标签必须是非空字符串：{lab!r}")
        if lab not in seen:
            seen.add(lab)
            norm.append(lab)
    return norm


def merge_regions(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """把命中区间合并为遮盖区：重叠或相接（端点相等）即合并。

    输入为 ``[(start, end), …]``，返回按起点升序、互不相交且互不相接
    的合并区间 ``[(start, end), …]``。
    """
    regions: list[tuple[int, int]] = []
    for s, e in sorted(spans):
        if regions and s <= regions[-1][1]:  # 重叠或相接 → 并入当前区
            ps, pe = regions[-1]
            regions[-1] = (ps, max(pe, e))
        else:
            regions.append((s, e))
    return regions


def _to_new(pos: int, regions: list[tuple[int, int]]) -> int:
    """原文码点边界 pos 在导出文本中的码点位置。

    每个遮盖区 [s, e) 原本占 e-s 个码点，导出文本中替换为 len(MARK)
    个码点；边界若落在遮盖区内部会被夹到该区的标记端点。
    """
    shift = 0
    for s, e in regions:
        if pos <= s:
            break
        shift += len(MARK) - (e - s)
        if pos < e:
            return s + len(MARK)  # 区内边界统一夹到标记结束位置
    return pos + shift


def _visible_pieces(start: int, end: int,
                    regions: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """求批注区间 [start, end) 减去遮盖区并集后的可见片段（原码点坐标）。

    纯区间差集：与遮盖区仅端点相接而不重叠时不扣除任何码点。
    """
    pieces: list[tuple[int, int]] = []
    cur = start
    for s, e in regions:
        if e <= cur:
            continue
        if s >= end:
            break
        if s > cur:
            pieces.append((cur, min(s, end)))
        cur = max(cur, e)
        if cur >= end:
            break
    if cur < end:
        pieces.append((cur, end))
    return pieces


def redact_export(text: str, annotations: list[dict],
                  redact_labels: list[str]) -> dict:
    """执行遮盖导出，返回 ``{"text", "annotations", "count", "redacted_count"}``。

    ``annotations`` 为可见片段列表，按原批注顺序、同批注内按原文位置
    排列；每条含 ``source_id``、``fragment``（从 1 开始）、``start``、
    ``end``（新文本码点范围）、``label``。``redacted_count`` 为合并后
    的遮盖区数量（无命中时为 0）。
    """
    labels = set(redact_labels)
    hit_spans = [(a["start"], a["end"]) for a in annotations
                 if a["label"] in labels]
    regions = merge_regions(hit_spans)

    if not regions:
        # 无命中：文本与批注范围完全不变
        fragments = [
            {"source_id": a["id"], "fragment": 1, "start": a["start"],
             "end": a["end"], "label": a["label"]}
            for a in annotations
        ]
        return {"text": text, "annotations": fragments,
                "count": len(fragments), "redacted_count": 0}

    # 拼接导出文本：可见部分原样保留，每个遮盖区替换为一个标记
    parts: list[str] = []
    cursor = 0
    for s, e in regions:
        parts.append(text[cursor:s])
        parts.append(MARK)
        cursor = e
    parts.append(text[cursor:])
    new_text = "".join(parts)

    fragments: list[dict] = []
    for a in annotations:
        if a["label"] in labels:
            continue  # 命中批注不导出
        pieces = _visible_pieces(a["start"], a["end"], regions)
        for idx, (s, e) in enumerate(pieces, 1):
            ns, ne = _to_new(s, regions), _to_new(e, regions)
            # 不变量：片段范围只对应原文可见文字（坐标构造保证不会跨过插入的标记）
            assert new_text[ns:ne] == text[s:e]
            fragments.append({"source_id": a["id"], "fragment": idx,
                              "start": ns, "end": ne, "label": a["label"]})

    return {"text": new_text, "annotations": fragments,
            "count": len(fragments), "redacted_count": len(regions)}
