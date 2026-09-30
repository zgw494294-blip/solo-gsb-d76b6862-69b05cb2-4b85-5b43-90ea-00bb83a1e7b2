"""批注偏移制式转换核心逻辑测试（纯标准库，无需启动服务）。"""

import pytest

from app.units import (
    BAD_INTERVAL,
    CODEPOINT,
    NOT_BOUNDARY,
    OUT_OF_RANGE,
    UTF16,
    UTF8_BYTE,
    UnitsError,
    build_boundaries,
    convert_offsets,
    find_annotation_errors,
    preview_offsets,
)

# 🎉 U+1F389：1 码点 / UTF-16 代理对占 2 / UTF-8 占 4 字节
# é 由 e（U+0065，1 字节）+ U+0301 组合尖音符（2 字节）组成（分解序列）
TEXT = "汉字é🎉A"


def ann(id, start, end, **extra):
    return {"id": id, "start": start, "end": end, **extra}


# ---------- 一次遍历建立三套边界 ----------

def test_boundary_lengths():
    bd = build_boundaries(TEXT)
    assert bd["length"] == {CODEPOINT: 6, UTF16: 7, UTF8_BYTE: 14}


def test_boundary_maps_align_on_char_edges_only():
    bd = build_boundaries(TEXT)
    # UTF-16：位置 5 在 🎉 代理对内部，不是边界；4、6 是
    assert set(bd["boundary"][UTF16]) == {0, 1, 2, 3, 4, 6, 7}
    # UTF-8：码点起点 0/3/6/7/9/13 与末尾 14；1、2、8、10、11、12 为多字节内部
    assert set(bd["boundary"][UTF8_BYTE]) == {0, 3, 6, 7, 9, 13, 14}
    # 码点：每个整数都是边界
    assert set(bd["boundary"][CODEPOINT]) == set(range(7))


def test_to_arrays_give_three_coordinates_per_cp_edge():
    bd = build_boundaries(TEXT)
    # 码点边界 i 对应：[码点, UTF-16, UTF-8]
    table = list(zip(bd["to"][CODEPOINT], bd["to"][UTF16], bd["to"][UTF8_BYTE]))
    assert table == [
        (0, 0, 0),   # 文本起点
        (1, 1, 3),   # 汉（UTF-8 3 字节）
        (2, 2, 6),   # 字
        (3, 3, 7),   # e（ASCII，1 字节）
        (4, 4, 9),   # ́（组合尖音符，UTF-16 占 1、UTF-8 占 2）
        (5, 6, 13),  # 🎉（码点 1、UTF-16 2、UTF-8 4）
        (6, 7, 14),  # A
    ]


def test_empty_text_boundaries():
    bd = build_boundaries("")
    assert bd["length"] == {CODEPOINT: 0, UTF16: 0, UTF8_BYTE: 0}
    assert bd["to"] == {CODEPOINT: [0], UTF16: [0], UTF8_BYTE: [0]}
    assert set(bd["boundary"][UTF8_BYTE]) == {0}


# ---------- 各制式互转 ----------

def test_utf16_to_utf8_han_combining_astral():
    res = convert_offsets(TEXT, [
        ann("u1", 0, 2, label="汉字"),
        ann("u2", 2, 4, label="组合"),
        ann("u3", 4, 6, label="表情"),
    ], UTF16, UTF8_BYTE)
    assert res["errors"] == []
    assert [(a["start"], a["end"]) for a in res["annotations"]] == [
        (0, 6),    # 汉字：2 码点 → 6 字节
        (6, 9),    # e（1 字节）+ 组合音符（2 字节）
        (9, 13),   # 🎉：2 个 UTF-16 单元 → 4 字节（不含 A）
    ]


def test_utf8_to_utf16():
    res = convert_offsets(TEXT, [
        ann("u1", 0, 6),     # 汉字
        ann("u2", 9, 13),    # 🎉
    ], UTF8_BYTE, UTF16)
    assert [(a["start"], a["end"]) for a in res["annotations"]] == [
        (0, 2), (4, 6),
    ]


def test_codepoint_is_native_index():
    res = convert_offsets(TEXT, [ann("u", 4, 5)], CODEPOINT, UTF8_BYTE)
    assert res["annotations"][0] == {"id": "u", "start": 9, "end": 13}
    res2 = convert_offsets(TEXT, [ann("u", 4, 5)], CODEPOINT, UTF16)
    assert (res2["annotations"][0]["start"], res2["annotations"][0]["end"]) == (4, 6)


def test_same_unit_is_identity():
    anns = [ann("u1", 4, 6, label="x", score=0.8)]
    res = convert_offsets(TEXT, anns, UTF16, UTF16)
    assert res["annotations"] == anns


def test_round_trip_through_all_units():
    # codepoint → utf16 → utf8 → codepoint 必须还原
    orig = [ann("a", 0, 2), ann("b", 2, 4), ann("c", 4, 6)]
    step1 = convert_offsets(TEXT, orig, CODEPOINT, UTF16)["annotations"]
    step2 = convert_offsets(TEXT, step1, UTF16, UTF8_BYTE)["annotations"]
    step3 = convert_offsets(TEXT, step2, UTF8_BYTE, CODEPOINT)["annotations"]
    assert [(a["start"], a["end"]) for a in step3] == [(0, 2), (2, 4), (4, 6)]


def test_extra_fields_and_order_preserved():
    anns = [
        ann("z", 0, 1, label="汉", note="z"),
        ann("a", 4, 6, label="🎉A", meta={"k": [1, 2]}),
        ann("m", 2, 4),
    ]
    out = convert_offsets(TEXT, anns, CODEPOINT, UTF8_BYTE)["annotations"]
    assert [a["id"] for a in out] == ["z", "a", "m"]          # 输入顺序不变
    assert out[0]["label"] == "汉" and out[0]["note"] == "z"  # 其余字段不变
    assert out[1]["meta"] == {"k": [1, 2]}


def test_zero_width_only_interval_is_invalid_in_every_unit():
    # 位置 6 在三种制式下都是字符边界（cp=2「字」之后、utf16 cp=🎉起点、utf8 同）
    for unit in (CODEPOINT, UTF16, UTF8_BYTE):
        res = convert_offsets(TEXT, [ann("u", 6, 6)], unit, CODEPOINT)
        assert res["annotations"] == []
        assert res["errors"][0]["type"] == BAD_INTERVAL


# ---------- 边界 / 越界错误逐项列出 ----------

def test_utf16_surrogate_pair_interior_is_not_boundary():
    errors = find_annotation_errors([ann("u", 5, 6)], UTF16, build_boundaries(TEXT))
    assert [(e["field"], e["type"]) for e in errors] == [("start", NOT_BOUNDARY)]
    assert errors[0]["id"] == "u" and errors[0]["value"] == 5


def test_utf8_multibyte_interior_is_not_boundary():
    # 「汉」占 [0,3)，位置 1、2 落入其 UTF-8 三字节序列内部；
    # U+0301 占 [7,9)，位置 8 同样落入二字节序列内部
    errors = find_annotation_errors([ann("u", 1, 2)], UTF8_BYTE, build_boundaries(TEXT))
    assert [(e["field"], e["type"]) for e in errors] == [
        ("start", NOT_BOUNDARY), ("end", NOT_BOUNDARY),
    ]
    errors = find_annotation_errors([ann("u", 8, 9)], UTF8_BYTE, build_boundaries(TEXT))
    assert [(e["field"], e["type"]) for e in errors] == [("start", NOT_BOUNDARY)]


def test_endpoint_equal_total_is_a_valid_boundary():
    # end 恰好等于文本总长合法；超过总长越界
    assert find_annotation_errors([ann("u", 0, 7)], UTF16, build_boundaries(TEXT)) == []
    errors = find_annotation_errors([ann("u", 6, 9)], UTF16, build_boundaries(TEXT))
    assert [(e["field"], e["type"]) for e in errors] == [("end", OUT_OF_RANGE)]


def test_negative_offset_is_out_of_range():
    errors = find_annotation_errors([ann("u", -1, 2)], CODEPOINT, build_boundaries(TEXT))
    assert errors[0] == {
        "id": "u", "field": "start", "value": -1, "type": OUT_OF_RANGE,
        "message": errors[0]["message"],
    }


def test_errors_listed_per_item_in_input_order():
    anns = [
        ann("good", 0, 2),
        ann("bad-boundary", 5, 6),       # start 落入代理对内部
        ann("bad-range", 6, 9),          # end 越界
        ann("bad-interval", 0, 0),       # start == end
        ann("both-bad", 10, 8),          # start 越界；end 也越界（逐字段列出）
    ]
    errors = find_annotation_errors(anns, UTF16, build_boundaries(TEXT))
    assert [(e["id"], e["field"], e["type"]) for e in errors] == [
        ("bad-boundary", "start", NOT_BOUNDARY),
        ("bad-range", "end", OUT_OF_RANGE),
        ("bad-interval", "interval", BAD_INTERVAL),
        ("both-bad", "start", OUT_OF_RANGE),
        ("both-bad", "end", OUT_OF_RANGE),
    ]


def test_any_error_blocks_conversion_output():
    res = convert_offsets(TEXT, [ann("ok", 0, 1), ann("bad", 5, 6)], UTF16, UTF8_BYTE)
    assert res["annotations"] == []
    assert [e["id"] for e in res["errors"]] == ["bad"]


def test_codepoint_unit_has_no_interior_positions():
    # 码点制式下每个整数都是边界，只有越界会报错
    errors = find_annotation_errors([ann("u", 3, 4)], CODEPOINT, build_boundaries(TEXT))
    assert errors == []


# ---------- 预览 ----------

def test_preview_shows_three_coordinates_and_excerpt():
    res = preview_offsets(TEXT, [ann("u1", 4, 6, label="表情")], UTF16, UTF8_BYTE)
    assert res["errors"] == []
    item = res["items"][0]
    assert item["text"] == "🎉"
    assert item["coordinates"] == {
        CODEPOINT: {"start": 4, "end": 5},
        UTF16: {"start": 4, "end": 6},
        UTF8_BYTE: {"start": 9, "end": 13},
    }
    assert res["lengths"] == {CODEPOINT: 6, UTF16: 7, UTF8_BYTE: 14}


def test_preview_bad_item_has_null_coordinates():
    res = preview_offsets(TEXT, [ann("bad", 5, 6)], UTF16, UTF8_BYTE)
    assert res["items"][0]["text"] is None
    assert res["items"][0]["coordinates"] is None
    assert res["items"][0]["errors"][0]["type"] == NOT_BOUNDARY
    assert res["errors"][0]["id"] == "bad"


def test_preview_combining_sequence_excerpt():
    # [2,4) 码点为 e + U+0301 → 原文是分解组合序列（两个码点）
    res = preview_offsets(TEXT, [ann("u", 2, 4)], CODEPOINT, UTF8_BYTE)
    assert res["items"][0]["text"] == "é"
    assert res["items"][0]["coordinates"][UTF8_BYTE] == {"start": 6, "end": 9}


# ---------- 非法制式 ----------

def test_unknown_unit_raises():
    with pytest.raises(UnitsError):
        convert_offsets(TEXT, [], "byte", CODEPOINT)
    with pytest.raises(UnitsError):
        preview_offsets(TEXT, [], CODEPOINT, "char")
