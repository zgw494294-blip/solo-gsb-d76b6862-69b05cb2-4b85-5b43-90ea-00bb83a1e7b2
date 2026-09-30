"""序列标注语料导出核心逻辑测试（纯标准库，无需启动服务）。"""

import pytest

from app.sequence import SequenceError, assign_layers, build_sequence, tokenize


def ann(id, start, end, label):
    return {"id": id, "start": start, "end": end, "label": label}


def toks(text):
    return [(t["text"], t["start"], t["end"]) for t in tokenize(text)]


def spans(result):
    return [(t["text"], t["start"], t["end"]) for t in result["tokens"]]


def tags(result):
    return [t["tags"] for t in result["tokens"]]


# ---------- token 切分 ----------

def test_tokenize_han_run_is_one_token():
    assert toks("数据采集") == [("数据采集", 0, 4)]


def test_tokenize_ascii_alnum_run_is_one_token():
    assert toks("DataX2024") == [("DataX2024", 0, 9)]


def test_tokenize_single_char_for_other():
    # 标点、emoji 等其他非空字符各自成 token；emoji 占 1 个码点
    assert toks("，。🎉") == [("，", 0, 1), ("。", 1, 2), ("🎉", 2, 3)]


def test_tokenize_skips_whitespace():
    assert toks("a b\tc\nd") == [("a", 0, 1), ("b", 2, 3), ("c", 4, 5), ("d", 6, 7)]
    assert toks(" \t\n　") == []  # 含全角空格，一律不产出 token


def test_tokenize_fullwidth_alnum_is_not_ascii_run():
    # 全角字母数字不是 ASCII 字母数字，也不是汉字 → 逐字成 token
    assert toks("ＡＢ１２") == [("Ａ", 0, 1), ("Ｂ", 1, 2), ("１", 2, 3), ("２", 3, 4)]


def test_tokenize_mixed_runs():
    assert toks("第一章 总则") == [("第一章", 0, 3), ("总则", 4, 6)]


# ---------- 分层 ----------

def test_touching_annotations_share_layer():
    # end == start 端点相接不算重叠 → 同层
    assert assign_layers([ann("a", 0, 2, "x"), ann("b", 2, 4, "x")]) == [[0, 1]]


def test_overlapping_annotations_need_own_layers():
    assert assign_layers([ann("a", 0, 3, "x"), ann("b", 1, 2, "x")]) == [[0], [1]]


def test_greedy_picks_min_end_then_layer_number():
    # a3 与 a1、a2 均不重叠；候选层末条 end 为 4（层1）与 3（层2）→ 选结束位置更小的层2
    anns = [ann("a1", 0, 4, "流程"), ann("a2", 1, 3, "核心"), ann("a3", 4, 8, "短语")]
    assert assign_layers(anns) == [[0], [1, 2]]


def test_layer_count_is_minimal():
    # 三条两两重叠 → 深度 3，必须 3 层
    anns = [ann("a", 0, 4, "x"), ann("b", 1, 3, "x"), ann("c", 2, 5, "x")]
    assert assign_layers(anns) == [[0], [1], [2]]
    # 链式重叠深度为 2 → 恰好 2 层
    chain = [ann("a", 0, 3, "x"), ann("b", 1, 2, "x"),
             ann("c", 2, 5, "x"), ann("d", 4, 6, "x")]
    assert assign_layers(chain) == [[0, 3], [1, 2]]


def test_sort_is_stable_by_start_end_label_id():
    # 输入乱序不影响结果：按 (start, end, label, id) 稳定排序后分层
    anns = [ann("c", 0, 4, "y"), ann("a", 0, 2, "x"), ann("b", 2, 4, "x")]
    assert assign_layers(anns) == [[1, 2], [0]]
    # start/end/label 都相同 → 按 id 决定先后
    dup = [ann("b", 0, 2, "x"), ann("a", 0, 2, "x")]
    assert assign_layers(dup) == [[1], [0]]


# ---------- 端到端：切分 + 分层 + BILOU + CoNLL ----------

def test_boundary_splits_token():
    res = build_sequence("数据采集", [ann("a", 1, 3, "核心")])
    assert spans(res) == [("数", 0, 1), ("据采", 1, 3), ("集", 3, 4)]
    assert tags(res) == [["O"], ["U-核心"], ["O"]]


def test_bilou_single_token_U_and_multi_token_BIL():
    res = build_sequence("甲ab。", [ann("a", 0, 4, "x")])
    assert [t[0] for t in tags(res)] == ["B-x", "I-x", "L-x"]
    res = build_sequence("目标", [ann("a", 0, 2, "x")])
    assert [t[0] for t in tags(res)] == ["U-x"]


def test_readme_example_end_to_end():
    text = "数据采集ABC。"
    anns = [ann("a1", 0, 4, "流程"), ann("a2", 1, 3, "核心"), ann("a3", 4, 8, "短语")]
    res = build_sequence(text, anns)
    assert res["layers"] == 2
    assert spans(res) == [("数", 0, 1), ("据采", 1, 3), ("集", 3, 4),
                          ("ABC", 4, 7), ("。", 7, 8)]
    # 同一 token 可在不同层各有标签（据采：层1 为 I-流程，层2 为 U-核心）
    assert tags(res) == [["B-流程", "O"], ["I-流程", "U-核心"], ["L-流程", "O"],
                         ["O", "B-短语"], ["O", "L-短语"]]
    assert res["conll"] == (
        "数 0 1 B-流程 O\n"
        "据采 1 3 I-流程 U-核心\n"
        "集 3 4 L-流程 O\n"
        "ABC 4 7 O B-短语\n"
        "。 7 8 O L-短语\n"
    )


def test_frontend_demo_example():
    text = "第一章 总则\n本模块用DataX数据采集与清洗，目标是统一口径。🎉"
    anns = [
        ann("s-chapter", 4, 6, "章节"),
        ann("s-flow", 11, 20, "流程"),
        ann("s-core", 18, 23, "核心"),
        ann("s-goal", 24, 31, "目标"),
        ann("s-emoji", 32, 33, "标记"),
    ]
    res = build_sequence(text, anns)
    assert res["layers"] == 2
    assert spans(res) == [
        ("第一章", 0, 3), ("总则", 4, 6), ("本模块用", 7, 11), ("DataX", 11, 16),
        ("数据", 16, 18), ("采集", 18, 20), ("与清洗", 20, 23), ("，", 23, 24),
        ("目标是统一口径", 24, 31), ("。", 31, 32), ("🎉", 32, 33),
    ]
    assert tags(res) == [
        ["O", "O"], ["U-章节", "O"], ["O", "O"], ["B-流程", "O"],
        ["I-流程", "O"], ["L-流程", "B-核心"], ["O", "L-核心"], ["O", "O"],
        ["U-目标", "O"], ["O", "O"], ["O", "U-标记"],
    ]


def test_whitespace_only_annotation_rejected():
    with pytest.raises(SequenceError):
        build_sequence("ab  cd", [ann("a", 2, 4, "x")])   # 区间只含空格
    with pytest.raises(SequenceError):
        build_sequence("甲\n乙", [ann("a", 1, 2, "x")])    # 区间只含换行


def test_empty_annotations_produce_bare_conll():
    res = build_sequence("ab。", [])
    assert res["layers"] == 0
    assert tags(res) == [[], []]
    assert res["conll"] == "ab 0 2\n。 2 3\n"


def test_empty_text():
    assert build_sequence("", []) == {"layers": 0, "tokens": [], "conll": ""}


def test_code_point_offsets_with_astral_chars():
    res = build_sequence("🎉目标🎉", [ann("a", 1, 3, "目标")])
    assert spans(res) == [("🎉", 0, 1), ("目标", 1, 3), ("🎉", 3, 4)]
    assert tags(res) == [["O"], ["U-目标"], ["O"]]
    assert res["conll"] == "🎉 0 1 O\n目标 1 3 U-目标\n🎉 3 4 O\n"
