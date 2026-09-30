"""批注审阅切片核心逻辑测试（纯标准库，无需启动服务）。"""

import hashlib
import json

import pytest

from app.review import (
    BAD_RANGE,
    CORRECT,
    DIGEST_MISMATCH,
    DROP,
    DUPLICATE_FRAGMENT,
    DUPLICATE_ID,
    FOREIGN_ID,
    KEEP,
    MISSING_ID,
    UNKNOWN_ACTION,
    UNKNOWN_FRAGMENT,
    ReviewError,
    apply_reviews,
    build_fragments,
    to_jsonl,
)


def ann(id, start, end, label="L"):
    return {"id": id, "start": start, "end": end, "label": label}


TEXT100 = "x" * 100  # 窗口测试用文本（内容无关，只看码点偏移）


# ---------- 顺序装箱 ----------

def test_pack_merges_while_span_fits():
    frags = build_fragments(TEXT100, [ann("a", 2, 5), ann("b", 6, 9), ann("c", 30, 33)],
                            max_chars=12, context_chars=0)
    assert [f["fragment_id"] for f in frags] == ["frag-1", "frag-2"]
    # a、b 合装：跨度 9-2=7 ≤ 12；c 加入后 33-2=31 > 12 → 封口另起
    assert (frags[0]["start"], frags[0]["end"]) == (2, 9)
    assert (frags[1]["start"], frags[1]["end"]) == (30, 33)
    assert frags[0]["annotations"] == [
        {"id": "a", "start": 0, "end": 3, "label": "L"},
        {"id": "b", "start": 4, "end": 7, "label": "L"},
    ]


def test_pack_seals_when_next_annotation_exceeds_span():
    frags = build_fragments(TEXT100, [ann("a", 0, 4), ann("b", 10, 14)],
                            max_chars=8, context_chars=0)
    assert [(f["start"], f["end"]) for f in frags] == [(0, 4), (10, 14)]


def test_pack_span_uses_rightmost_end_for_nested_annotation():
    # b 完整落在 a 内：加入 b 后跨度不变，仍可合并
    frags = build_fragments(TEXT100, [ann("a", 0, 10), ann("b", 2, 3)],
                            max_chars=10, context_chars=0)
    assert len(frags) == 1
    assert (frags[0]["start"], frags[0]["end"]) == (0, 10)


def test_pack_sorts_by_start_end_label_id():
    anns = [
        ann("b", 10, 12, "X"),
        ann("a2", 0, 4, "Y"),   # 同 start/end，label 小者先？label Y > X 但 start 优先
        ann("a1", 0, 4, "Y"),   # 与 a2 全同时按 id 排
        ann("a0", 0, 4, "X"),   # 同范围 label X < Y → 排在 a1/a2 前
    ]
    frags = build_fragments(TEXT100, anns, max_chars=100, context_chars=0)
    assert [a["id"] for a in frags[0]["annotations"]] == ["a0", "a1", "a2", "b"]


def test_single_annotation_longer_than_max_chars_raises():
    with pytest.raises(ReviewError):
        build_fragments(TEXT100, [ann("bad", 0, 11)], max_chars=10, context_chars=0)


def test_all_too_long_ids_listed_in_error():
    with pytest.raises(ReviewError) as exc:
        build_fragments(TEXT100, [ann("x", 0, 20), ann("ok", 30, 31), ann("y", 40, 55)],
                        max_chars=10, context_chars=0)
    assert "x" in str(exc.value) and "y" in str(exc.value)
    assert "ok" not in str(exc.value)


def test_invalid_params_raise():
    with pytest.raises(ReviewError):
        build_fragments(TEXT100, [], max_chars=0, context_chars=0)
    with pytest.raises(ReviewError):
        build_fragments(TEXT100, [], max_chars=10, context_chars=-1)


def test_empty_annotations_yield_no_fragments():
    assert build_fragments(TEXT100, [], max_chars=10, context_chars=2) == []
    assert to_jsonl([]) == ""


# ---------- 切片窗口 ----------

def test_window_even_split():
    f = build_fragments(TEXT100, [ann("a", 40, 50)], max_chars=30, context_chars=100)[0]
    assert (f["start"], f["end"]) == (30, 60)  # 余 20 → 左右各 10


def test_window_odd_remainder_goes_right():
    f = build_fragments(TEXT100, [ann("a", 40, 50)], max_chars=31, context_chars=100)[0]
    assert (f["start"], f["end"]) == (30, 61)  # 余 21 → 左 10 右 11


def test_window_each_side_capped_by_context_chars():
    f = build_fragments(TEXT100, [ann("a", 40, 50)], max_chars=30, context_chars=6)[0]
    assert (f["start"], f["end"]) == (34, 56)  # 每侧封顶 6，窗口可短于 max_chars


def test_window_left_boundary_surplus_moves_right():
    f = build_fragments(TEXT100, [ann("a", 4, 10)], max_chars=20, context_chars=8)[0]
    # 余 14 → 左右各 7；左侧只能扩 4，余量 3 转给右侧（封顶 8）
    assert (f["start"], f["end"]) == (0, 18)


def test_window_right_boundary_surplus_moves_left():
    f = build_fragments("x" * 14, [ann("a", 4, 10)], max_chars=20, context_chars=8)[0]
    # 余 14 → 左 7 右 7；左扩 4 余 3 转右（封顶 8），右只能扩 4 余 4 再转回左
    assert (f["start"], f["end"]) == (0, 14)


def test_window_never_exceeds_text():
    f = build_fragments("x" * 10, [ann("a", 2, 8)], max_chars=30, context_chars=100)[0]
    assert (f["start"], f["end"]) == (0, 10)


def test_window_zero_context_chars_is_exact_span():
    f = build_fragments(TEXT100, [ann("a", 40, 50)], max_chars=30, context_chars=0)[0]
    assert (f["start"], f["end"]) == (40, 50)


# ---------- 切片内容与摘要 ----------

def test_fragment_fields_and_digest():
    text = "目标是统一口径，验收时复核样本。"
    f = build_fragments(text, [ann("a1", 0, 4, "目标")], max_chars=10, context_chars=2)[0]
    assert f["fragment_id"] == "frag-1"
    assert (f["start"], f["end"]) == (0, 6)  # 左界余量转右，右侧封顶 2
    assert f["text"] == text[0:6]
    assert f["sha256"] == hashlib.sha256(text[0:6].encode("utf-8")).hexdigest()
    assert f["annotations"] == [{"id": "a1", "start": 0, "end": 4, "label": "目标"}]


def test_astral_char_offsets_are_codepoints():
    text = "ab🎉cd"  # 🎉 占 1 码点：a0 b1 🎉2 c3 d4
    f = build_fragments(text, [ann("e", 2, 3, "表情")], max_chars=3, context_chars=1)[0]
    assert (f["start"], f["end"]) == (1, 4)
    assert f["text"] == "b🎉c"
    assert f["annotations"] == [{"id": "e", "start": 1, "end": 2, "label": "表情"}]


def test_jsonl_one_object_per_line_with_local_annotations():
    text = "目标是统一口径，验收时复核样本。"
    anns = [ann("a1", 0, 4, "目标"), ann("a2", 12, 14, "对象")]
    frags = build_fragments(text, anns, max_chars=10, context_chars=2)
    jl = to_jsonl(frags)
    assert jl.endswith("\n")
    rows = [json.loads(line) for line in jl.splitlines()]
    assert len(rows) == 2
    assert list(rows[0]) == ["fragment_id", "text", "start", "end", "sha256", "annotations"]
    assert rows[0]["fragment_id"] == "frag-1"
    assert rows[1]["annotations"] == [{"id": "a2", "start": 2, "end": 4, "label": "对象"}]


# ---------- 导入审阅结果 ----------

IMPORT_TEXT = "目标是统一口径，验收时复核样本。"
IMPORT_ANNS = [ann("a1", 0, 4, "目标"), ann("a2", 12, 14, "对象")]
IMPORT_PARAMS = (10, 2)  # max_chars, context_chars


def _frags():
    return build_fragments(IMPORT_TEXT, IMPORT_ANNS, *IMPORT_PARAMS)


def _review(frag, decisions):
    return {"fragment_id": frag["fragment_id"], "sha256": frag["sha256"],
            "decisions": decisions}


def _all_keep():
    return [_review(f, [{"id": a["id"], "action": KEEP} for a in f["annotations"]])
            for f in _frags()]


def test_import_keep_round_trip_restores_global_ranges():
    res = apply_reviews(IMPORT_TEXT, IMPORT_ANNS, *IMPORT_PARAMS, _all_keep())
    assert res["errors"] == [] and res["dropped"] == []
    assert res["annotations"] == [
        {"id": "a1", "start": 0, "end": 4, "label": "目标",
         "action": KEEP, "fragment_id": "frag-1"},
        {"id": "a2", "start": 12, "end": 14, "label": "对象",
         "action": KEEP, "fragment_id": "frag-2"},
    ]


def test_import_correct_converts_local_to_global():
    frags = _frags()
    reviews = _all_keep()
    reviews[1]["decisions"] = [{"id": "a2", "action": CORRECT, "start": 0, "end": 3}]
    res = apply_reviews(IMPORT_TEXT, IMPORT_ANNS, *IMPORT_PARAMS, reviews)
    assert res["errors"] == []
    a2 = res["annotations"][1]
    # frag-2 窗口为 [10, 16)，局部 [0, 3) → 全局 [10, 13)
    assert frags[1]["start"] == 10
    assert (a2["start"], a2["end"], a2["action"]) == (10, 13, CORRECT)


def test_import_drop_excludes_annotation():
    reviews = _all_keep()
    reviews[0]["decisions"] = [{"id": "a1", "action": DROP}]
    res = apply_reviews(IMPORT_TEXT, IMPORT_ANNS, *IMPORT_PARAMS, reviews)
    assert res["errors"] == []
    assert [a["id"] for a in res["annotations"]] == ["a2"]
    assert res["dropped"] == ["a1"]


def test_import_result_sorted_by_global_range():
    # a2 修正到 [10, 11)，a1 保持 [0, 4)：输出按全局范围稳定排序
    reviews = _all_keep()
    reviews[1]["decisions"] = [{"id": "a2", "action": CORRECT, "start": 0, "end": 1}]
    res = apply_reviews(IMPORT_TEXT, IMPORT_ANNS, *IMPORT_PARAMS, reviews)
    assert [(a["id"], a["start"]) for a in res["annotations"]] == [("a1", 0), ("a2", 10)]


def test_import_unknown_fragment_rejected():
    reviews = _all_keep()
    reviews[0] = dict(reviews[0], fragment_id="frag-9")
    res = apply_reviews(IMPORT_TEXT, IMPORT_ANNS, *IMPORT_PARAMS, reviews)
    assert res["annotations"] == []
    assert UNKNOWN_FRAGMENT in {e["type"] for e in res["errors"]}


def test_import_duplicate_fragment_rejected():
    reviews = _all_keep()
    reviews.append(dict(reviews[0]))  # 同一切片再提交一次
    res = apply_reviews(IMPORT_TEXT, IMPORT_ANNS, *IMPORT_PARAMS, reviews)
    assert res["annotations"] == []
    assert DUPLICATE_FRAGMENT in {e["type"] for e in res["errors"]}


def test_import_digest_mismatch_rejected():
    reviews = _all_keep()
    reviews[0] = dict(reviews[0], sha256="0" * 64)
    res = apply_reviews(IMPORT_TEXT, IMPORT_ANNS, *IMPORT_PARAMS, reviews)
    assert res["annotations"] == []
    types = {e["type"] for e in res["errors"]}
    assert DIGEST_MISMATCH in types
    assert MISSING_ID in types  # 摘要失败的切片决策不被采纳，其 id 视为未提交


def test_import_foreign_id_rejected():
    frags = _frags()
    reviews = _all_keep()
    # a2 属于 frag-2，却提交到 frag-1
    reviews[0]["decisions"].append({"id": "a2", "action": KEEP})
    reviews[1]["decisions"] = []
    res = apply_reviews(IMPORT_TEXT, IMPORT_ANNS, *IMPORT_PARAMS, reviews)
    assert res["annotations"] == []
    assert FOREIGN_ID in {e["type"] for e in res["errors"]}
    assert frags[1]["fragment_id"] == "frag-2"


def test_import_duplicate_id_rejected():
    reviews = _all_keep()
    reviews[0]["decisions"].append({"id": "a1", "action": DROP})
    res = apply_reviews(IMPORT_TEXT, IMPORT_ANNS, *IMPORT_PARAMS, reviews)
    assert res["annotations"] == []
    assert DUPLICATE_ID in {e["type"] for e in res["errors"]}


def test_import_missing_id_rejected():
    reviews = _all_keep()
    reviews[1]["decisions"] = []  # a2 未提交
    res = apply_reviews(IMPORT_TEXT, IMPORT_ANNS, *IMPORT_PARAMS, reviews)
    assert res["annotations"] == []
    err = [e for e in res["errors"] if e["type"] == MISSING_ID]
    assert err and err[0]["ids"] == ["a2"]


def test_import_correct_range_must_stay_in_window():
    frags = _frags()
    win_len = frags[1]["end"] - frags[1]["start"]
    for bad in ({"start": 0, "end": win_len + 1}, {"start": 2, "end": 2},
                {"start": -1, "end": 2}, {"start": None, "end": 2}):
        reviews = _all_keep()
        reviews[1]["decisions"] = [dict({"id": "a2", "action": CORRECT}, **bad)]
        res = apply_reviews(IMPORT_TEXT, IMPORT_ANNS, *IMPORT_PARAMS, reviews)
        assert res["annotations"] == []
        assert BAD_RANGE in {e["type"] for e in res["errors"]}, bad


def test_import_unknown_action_rejected():
    reviews = _all_keep()
    reviews[0]["decisions"] = [{"id": "a1", "action": "modify"}]
    res = apply_reviews(IMPORT_TEXT, IMPORT_ANNS, *IMPORT_PARAMS, reviews)
    assert res["annotations"] == []
    assert UNKNOWN_ACTION in {e["type"] for e in res["errors"]}


def test_import_any_error_rejects_whole_batch():
    # 第一条决策完全合法，第二条非法 → 整批拒绝，不留部分结果
    reviews = _all_keep()
    reviews[1]["decisions"] = [{"id": "a2", "action": CORRECT, "start": 0, "end": 99}]
    res = apply_reviews(IMPORT_TEXT, IMPORT_ANNS, *IMPORT_PARAMS, reviews)
    assert res["annotations"] == [] and res["dropped"] == []
    assert res["errors"]
