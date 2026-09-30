"""可分享文本遮盖导出核心逻辑测试（纯标准库，无需启动服务）。"""

import pytest

from app.redaction import (
    MARK,
    RedactionError,
    merge_regions,
    redact_export,
    validate_redact_labels,
)


def ann(id, start, end, label):
    return {"id": id, "start": start, "end": end, "label": label}


# ---------- 遮盖标签校验 ----------

def test_validate_redact_labels_dedups_keeps_order():
    assert validate_redact_labels(["电话", "工号", "电话"]) == ["电话", "工号"]


def test_validate_redact_labels_empty_list_ok():
    assert validate_redact_labels([]) == []


@pytest.mark.parametrize("bad", ["x", 1, None, {"a": 1}, True, False, 3.14])
def test_validate_redact_labels_rejects_non_list(bad):
    with pytest.raises(RedactionError, match="必须是数组"):
        validate_redact_labels(bad)


@pytest.mark.parametrize("bad", [[""], ["电话", ""], ["电话", 1],
                                 [["x"]], [None], [True]])
def test_validate_redact_labels_rejects_bad_elements(bad):
    with pytest.raises(RedactionError, match="遮盖标签必须是非空字符串"):
        validate_redact_labels(bad)


# ---------- 区间合并 ----------

def test_merge_regions_overlap():
    assert merge_regions([(0, 5), (3, 8)]) == [(0, 8)]


def test_merge_regions_touching_are_merged():
    # 相接（端点相等）即合并
    assert merge_regions([(0, 4), (4, 8)]) == [(0, 8)]


def test_merge_regions_separate_kept_apart():
    assert merge_regions([(0, 4), (5, 8)]) == [(0, 4), (5, 8)]


def test_merge_regions_unsorted_and_contained():
    assert merge_regions([(9, 10), (0, 10), (2, 3)]) == [(0, 10)]


# ---------- 导出：无命中 ----------

def test_no_hit_returns_text_and_ranges_unchanged():
    text = "联系方式：13800001234。"
    anns = [ann("a", 0, 2, "普通"), ann("b", 5, 9, "其他")]
    res = redact_export(text, anns, ["电话"])
    assert res["text"] == text
    assert res["redacted_count"] == 0
    assert res["annotations"] == [
        {"source_id": "a", "fragment": 1, "start": 0, "end": 2, "label": "普通"},
        {"source_id": "b", "fragment": 1, "start": 5, "end": 9, "label": "其他"},
    ]


def test_empty_redact_labels_changes_nothing():
    text = "abc"
    anns = [ann("a", 0, 1, "X")]
    res = redact_export(text, anns, [])
    assert res["text"] == "abc"
    assert res["annotations"][0]["start"] == 0
    assert res["annotations"][0]["end"] == 1


# ---------- 导出：合并、替换与拆分 ----------

def test_single_hit_region_replaced_and_hit_not_exported():
    text = "甲AAA乙"
    anns = [ann("h", 1, 4, "密"), ann("v", 4, 5, "普通")]
    res = redact_export(text, anns, ["密"])
    assert res["text"] == f"甲{MARK}乙"
    assert res["redacted_count"] == 1
    # 命中批注不导出
    assert [f["source_id"] for f in res["annotations"]] == ["v"]
    # 可见批注坐标随标记平移：标记长 4、原区间长 3，整体后移 1
    assert (res["annotations"][0]["start"], res["annotations"][0]["end"]) == (5, 6)
    assert res["text"][5:6] == "乙"


def test_adjacent_hit_zones_merge_into_one_mark():
    # [0,2) 与 [2,15) 相接 → 合并为一个遮盖区
    text = "张三电话13800001234；工号E0088已核验。"
    anns = [
        ann("a1", 0, 2, "姓名"),
        ann("a2", 2, 15, "电话"),
        ann("a3", 16, 23, "工号"),
        ann("a4", 0, 27, "记录"),
        ann("a5", 23, 27, "备注"),
    ]
    res = redact_export(text, anns, ["姓名", "电话", "工号"])
    assert res["text"] == f"{MARK}；{MARK}已核验。"
    assert res["redacted_count"] == 2
    # 命中批注 a1/a2/a3 不导出
    assert [f["source_id"] for f in res["annotations"]] == [
        "a4", "a4", "a5",
    ]
    f1, f2, f3 = res["annotations"]
    assert (f1["fragment"], f1["start"], f1["end"], f1["label"]) == (1, 4, 5, "记录")
    assert (f2["fragment"], f2["start"], f2["end"], f2["label"]) == (2, 9, 13, "记录")
    assert (f3["fragment"], f3["start"], f3["end"], f3["label"]) == (1, 9, 13, "备注")
    # 范围内文字是原文可见部分，不含遮盖标记
    assert res["text"][f1["start"]:f1["end"]] == "；"
    assert res["text"][f2["start"]:f2["end"]] == "已核验。"
    assert all(MARK not in res["text"][f["start"]:f["end"]]
               for f in res["annotations"])


def test_spanning_annotation_split_into_fragments():
    text = "甲AAABBB乙"
    anns = [
        ann("h1", 1, 4, "密"),   # AAA
        ann("h2", 4, 7, "密"),   # BBB，与 h1 相接 → 合并
        ann("c", 0, 8, "公开"),  # 跨遮盖区 → 甲 / 乙 两个片段
    ]
    res = redact_export(text, anns, ["密"])
    assert res["text"] == f"甲{MARK}乙"
    assert res["redacted_count"] == 1
    f1, f2 = res["annotations"]
    assert (f1["source_id"], f1["fragment"], f1["start"], f1["end"]) == (
        "c", 1, 0, 1)
    assert (f2["source_id"], f2["fragment"], f2["start"], f2["end"]) == (
        "c", 2, 5, 6)
    assert res["text"][0:1] == "甲"
    assert res["text"][5:6] == "乙"


def test_annotation_fully_inside_region_is_dropped():
    text = "xxAAAxx"
    anns = [
        ann("h", 2, 5, "密"),
        ann("inside", 3, 4, "普通"),   # 完全被遮盖 → 舍弃
        ann("cover", 1, 6, "普通"),    # [1,2) 与 [5,6) 两个片段
    ]
    res = redact_export(text, anns, ["密"])
    assert [f["source_id"] for f in res["annotations"]] == ["cover", "cover"]
    assert [f["fragment"] for f in res["annotations"]] == [1, 2]
    assert res["text"][res["annotations"][0]["start"]:res["annotations"][0]["end"]] == "x"
    assert res["text"][res["annotations"][1]["start"]:res["annotations"][1]["end"]] == "x"


def test_annotation_touching_region_boundary_is_not_split():
    # 仅相接不重叠：[0,2) 与遮盖区 [2,5) 之间没有损失码点
    text = "甲乙AAA丙"
    anns = [ann("h", 2, 5, "密"), ann("left", 0, 2, "普通"),
            ann("right", 5, 6, "普通")]
    res = redact_export(text, anns, ["密"])
    assert res["text"] == f"甲乙{MARK}丙"
    left, right = res["annotations"]
    assert (left["start"], left["end"]) == (0, 2)
    assert res["text"][0:2] == "甲乙"
    # 右侧起点从 5 平移到 2+4=6
    assert (right["start"], right["end"]) == (6, 7)
    assert res["text"][6:7] == "丙"
    assert all(f["fragment"] == 1 for f in res["annotations"])


def test_fragments_ordered_by_input_then_position():
    text = "aAAbBBc"
    anns = [
        ann("z_last", 0, 7, "普通"),   # 输入在前，覆盖全局
        ann("h1", 1, 3, "密"),
        ann("h2", 4, 6, "密"),
        ann("m_mid", 0, 7, "普通"),    # 输入在后
    ]
    res = redact_export(text, anns, ["密"])
    assert [f["source_id"] for f in res["annotations"]] == [
        "z_last", "z_last", "z_last", "m_mid", "m_mid", "m_mid",
    ]
    # 同批注内片段按位置升序
    z = [f for f in res["annotations"] if f["source_id"] == "z_last"]
    assert [f["fragment"] for f in z] == [1, 2, 3]
    starts = [f["start"] for f in z]
    assert starts == sorted(starts)


def test_whole_text_redacted_leaves_only_mark():
    text = "秘密内容"
    anns = [ann("h", 0, 4, "密"), ann("v", 1, 3, "普通")]
    res = redact_export(text, anns, ["密"])
    assert res["text"] == MARK
    assert res["annotations"] == []
    assert res["count"] == 0
    assert res["redacted_count"] == 1


def test_codepoint_offsets_with_supplementary_chars():
    # 辅助平面字符 🎉 占 1 个码点：区间按码点计，标记替换不破坏周围字符
    text = "🎉秘密🎉"
    anns = [ann("h", 1, 3, "密"), ann("v", 3, 4, "普通")]
    res = redact_export(text, anns, ["密"])
    assert res["text"] == f"🎉{MARK}🎉"
    v = res["annotations"][0]
    assert res["text"][v["start"]:v["end"]] == "🎉"


def test_label_filter_is_exact_match():
    text = "abcdef"
    anns = [ann("h", 0, 2, "密"), ann("v", 2, 4, "秘密")]
    res = redact_export(text, anns, ["密"])
    assert res["text"] == f"{MARK}cdef"
    assert [f["source_id"] for f in res["annotations"]] == ["v"]
    v = res["annotations"][0]
    assert (v["start"], v["end"]) == (4, 6)


def test_input_annotations_are_not_mutated():
    text = "xAAx"
    anns = [ann("h", 1, 3, "密"), ann("v", 0, 4, "普通")]
    redact_export(text, anns, ["密"])
    assert anns[0] == {"id": "h", "start": 1, "end": 3, "label": "密"}
    assert anns[1] == {"id": "v", "start": 0, "end": 4, "label": "普通"}


def test_original_text_literally_containing_mark_still_exported():
    # 原文本身就含有「[遮盖]」字样：它是可见文字，片段仍应原样保留
    text = f"{MARK}AA"
    anns = [ann("h", 4, 6, "密"), ann("v", 0, 4, "普通")]
    res = redact_export(text, anns, ["密"])
    assert res["text"] == f"{MARK}{MARK}"
    v = res["annotations"][0]
    assert res["text"][v["start"]:v["end"]] == MARK
    assert (v["start"], v["end"]) == (0, 4)
