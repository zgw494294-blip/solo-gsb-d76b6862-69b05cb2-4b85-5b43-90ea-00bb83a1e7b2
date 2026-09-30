"""批注偏移制式转换核心逻辑。

一条批注的区间 [start, end) 可以按三种制式描述：

- ``codepoint``：Unicode 码点偏移（Python ``str`` 索引）；
- ``utf16``：UTF-16 代码单元偏移（BMP 字符占 1，辅助平面字符的代理对占 2）；
- ``utf8_byte``：UTF-8 字节偏移（每码点占 1~4 字节）。

**一次遍历**文本（逐码点）建立三套字符边界映射：每个码点边界处在三种
制式下的偏移，以及各制式下的文本总长度。源偏移必须落在对应制式的字符
边界上：

- ``codepoint``：整数位置天然都是码点边界，只需不越界；
- ``utf16``：禁止落在代理对内部（高、低代理代码单元之间）；
- ``utf8_byte``：禁止落在 UTF-8 多字节序列内部。

校验错误逐项列出批注 ``id``、字段（``start`` / ``end``，区间整体非法时
为 ``interval``）与值。导出仅把 ``start``、``end`` 转为目标制式，保持
输入顺序，其余字段原样保留。
"""

from __future__ import annotations

# 支持的偏移制式
CODEPOINT = "codepoint"
UTF16 = "utf16"
UTF8_BYTE = "utf8_byte"
UNITS = (CODEPOINT, UTF16, UTF8_BYTE)

UNIT_LABELS = {
    CODEPOINT: "码点",
    UTF16: "UTF-16 代码单元",
    UTF8_BYTE: "UTF-8 字节",
}

# 错误类型
OUT_OF_RANGE = "out_of_range"    # 偏移为负或超出文本总长
NOT_BOUNDARY = "not_boundary"    # 落入 UTF-8 多字节序列 / UTF-16 代理对内部
BAD_INTERVAL = "bad_interval"    # start >= end


class UnitsError(ValueError):
    """请求不合法（未知 source_unit / target_unit）。"""


def _utf8_size(cp: int) -> int:
    """码点编码为 UTF-8 所占字节数（1~4）。"""
    if cp < 0x80:
        return 1
    if cp < 0x800:
        return 2
    if cp < 0x10000:
        return 3
    return 4


def build_boundaries(text: str) -> dict:
    """一次遍历文本，建立三套字符边界映射。

    返回::

        {
          "boundary": {unit: {该制式偏移: 码点下标}},  # 仅字符边界处有键
          "to":       {unit: [每码点边界处的该制式偏移, …, 末尾偏移]},
          "length":   {unit: 文本在该制式下的总长度},
        }

    ``boundary`` 用于判定源偏移是否落在字符边界（含两端）；``to`` 以码点
    下标为索引，用于把边界换算到目标制式。每读入一个码点推进一次计数器，
    三套映射在同一遍循环内建成。
    """
    cp_pos = 0
    u8_pos = 0
    u16_pos = 0
    cp_map = {0: 0}
    u8_map = {0: 0}
    u16_map = {0: 0}
    to_cp = [0]
    to_u8 = [0]
    to_u16 = [0]
    for ch in text:
        cp = ord(ch)
        cp_pos += 1
        u8_pos += _utf8_size(cp)
        u16_pos += 1 if cp < 0x10000 else 2  # 辅助平面字符为代理对（2 单元）
        cp_map[cp_pos] = cp_pos
        u8_map[u8_pos] = cp_pos
        u16_map[u16_pos] = cp_pos
        to_cp.append(cp_pos)
        to_u8.append(u8_pos)
        to_u16.append(u16_pos)
    return {
        "boundary": {CODEPOINT: cp_map, UTF16: u16_map, UTF8_BYTE: u8_map},
        "to": {CODEPOINT: to_cp, UTF16: to_u16, UTF8_BYTE: to_u8},
        "length": {CODEPOINT: cp_pos, UTF16: u16_pos, UTF8_BYTE: u8_pos},
    }


def _check_units(source_unit: str, target_unit: str) -> None:
    if source_unit not in UNITS:
        raise UnitsError(f"未知 source_unit：{source_unit!r}，支持 {', '.join(UNITS)}")
    if target_unit not in UNITS:
        raise UnitsError(f"未知 target_unit：{target_unit!r}，支持 {', '.join(UNITS)}")


def find_annotation_errors(annotations: list[dict], unit: str, bd: dict) -> list[dict]:
    """逐条检查源制式偏移，按输入顺序、start 先于 end 列出全部错误。

    每个端点先查越界（负数或超过该制式总长），再查是否字符边界
    （落入 UTF-8 多字节序列 / UTF-16 代理对内部）；两端点都合法时
    再查 ``start < end``。错误项含 ``id``、``field``、``value``、
    ``type`` 与 ``message``。
    """
    total = bd["length"][unit]
    boundary = bd["boundary"][unit]
    errors: list[dict] = []
    for a in annotations:
        s, e = a["start"], a["end"]
        endpoint_errors = 0
        for field, value in (("start", s), ("end", e)):
            if value < 0 or value > total:
                errors.append({
                    "id": a["id"], "field": field, "value": value,
                    "type": OUT_OF_RANGE,
                    "message": f"批注 {a['id']} 的 {field}={value} 越界"
                               f"（{UNIT_LABELS[unit]}文本长度为 {total}）",
                })
                endpoint_errors += 1
            elif value not in boundary:
                if unit == UTF16:
                    where = "UTF-16 代理对内部"
                else:
                    where = "UTF-8 多字节序列内部"
                errors.append({
                    "id": a["id"], "field": field, "value": value,
                    "type": NOT_BOUNDARY,
                    "message": f"批注 {a['id']} 的 {field}={value} 不是"
                               f"{UNIT_LABELS[unit]}字符边界（落入{where}）",
                })
                endpoint_errors += 1
        if endpoint_errors == 0 and s >= e:
            errors.append({
                "id": a["id"], "field": "interval", "value": [s, e],
                "type": BAD_INTERVAL,
                "message": f"批注 {a['id']} 的区间 [{s}, {e}) 非法"
                           "（要求 start < end）",
            })
    return errors


def convert_offsets(text: str, annotations: list[dict],
                    source_unit: str, target_unit: str,
                    bd: dict | None = None) -> dict:
    """把全部批注的 start、end 从源制式换算到目标制式。

    返回 ``{"errors": [...], "annotations": [...]}``：存在任何校验错误时
    ``annotations`` 为空列表；否则按**输入顺序**返回批注，仅替换
    ``start``、``end``，其余字段保持不变。
    """
    _check_units(source_unit, target_unit)
    bd = bd or build_boundaries(text)
    errors = find_annotation_errors(annotations, source_unit, bd)
    if errors:
        return {"errors": errors, "annotations": []}
    src_boundary = bd["boundary"][source_unit]
    tgt_to = bd["to"][target_unit]
    out = []
    for a in annotations:
        cp_s = src_boundary[a["start"]]
        cp_e = src_boundary[a["end"]]
        item = dict(a)
        item["start"] = tgt_to[cp_s]
        item["end"] = tgt_to[cp_e]
        out.append(item)
    return {"errors": [], "annotations": out}


def preview_offsets(text: str, annotations: list[dict],
                    source_unit: str, target_unit: str) -> dict:
    """页面预览：每条批注的原文片段与三种制式坐标。

    返回 ``{"items", "errors", "lengths", "source_unit", "target_unit"}``。
    合法批注的 ``coordinates`` 同时给出三种制式的 ``{start, end}``，
    ``text`` 为按码点切出的原文；非法批注二者为 ``null``，错误列入
    ``errors``（同时挂在该条目的 ``errors`` 上）。
    """
    _check_units(source_unit, target_unit)
    bd = build_boundaries(text)
    errors = find_annotation_errors(annotations, source_unit, bd)
    errors_by_id: dict = {}
    for err in errors:
        errors_by_id.setdefault(err["id"], []).append(err)

    src_boundary = bd["boundary"][source_unit]
    items = []
    for a in annotations:
        row_errors = errors_by_id.get(a["id"], [])
        if row_errors:
            items.append({"id": a["id"], "text": None,
                          "coordinates": None, "errors": row_errors})
            continue
        cp_s = src_boundary[a["start"]]
        cp_e = src_boundary[a["end"]]
        coordinates = {
            unit: {"start": bd["to"][unit][cp_s], "end": bd["to"][unit][cp_e]}
            for unit in UNITS
        }
        items.append({"id": a["id"], "text": text[cp_s:cp_e],
                      "coordinates": coordinates, "errors": []})
    return {"items": items, "errors": errors, "lengths": bd["length"],
            "source_unit": source_unit, "target_unit": target_unit}
