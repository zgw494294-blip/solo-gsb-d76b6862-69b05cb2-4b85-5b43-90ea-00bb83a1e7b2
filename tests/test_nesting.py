"""嵌套批注审计核心逻辑测试（纯标准库，无需启动服务）。"""

import pytest

from app.nesting import (
    CHILD_LABEL,
    CROSSING,
    DUPLICATE_SPAN,
    ROOT_LABEL,
    NestingError,
    audit_nesting,
    build_tree,
    check_labels,
    export_nesting,
    find_span_errors,
    validate_rules,
)


def ann(id, start, end, label):
    return {"id": id, "start": start, "end": end, "label": label}


RULES = validate_rules({
    "root_labels": ["文档"],
    "children": {
        "文档": ["章节", "段落"],
        "章节": ["句子"],
        "段落": ["句子", "短语"],
        "句子": ["短语"],
    },
})


def shape(roots):
    """把节点树压缩为 (id, [子树…]) 便于断言。"""
    return [(n["annotation"]["id"], shape(n["children"])) for n in roots]


def view_shape(nodes):
    """把审计返回的视图树压缩为 (id, [子树…]) 便于断言。"""
    return [(n["id"], view_shape(n["children"])) for n in nodes]


# ---------- 区间关系 ----------

def test_touching_intervals_are_not_overlap():
    # [0, 2) 与 [2, 4) 端点相接，不算重叠 → 无错误，二者为并列根
    anns = [ann("a", 0, 2, "x"), ann("b", 2, 4, "x")]
    assert find_span_errors(anns) == []
    assert shape(build_tree(anns)) == [("a", []), ("b", [])]


def test_same_range_is_duplicate_span():
    errors = find_span_errors([ann("a", 1, 3, "x"), ann("b", 1, 3, "y")])
    assert [e["type"] for e in errors] == [DUPLICATE_SPAN]
    assert errors[0]["ids"] == ["a", "b"]


def test_partial_crossing_is_error():
    errors = find_span_errors([ann("a", 0, 3, "x"), ann("b", 2, 5, "x")])
    assert [e["type"] for e in errors] == [CROSSING]
    assert errors[0]["ids"] == ["a", "b"]


def test_strict_containment_is_not_error():
    assert find_span_errors([ann("a", 0, 5, "x"), ann("b", 1, 3, "x")]) == []


def test_shared_endpoint_containment_is_strict():
    # 起点相同、终点不同 / 终点相同、起点不同 → 严格包含，不是 duplicate_span
    assert find_span_errors([ann("a", 0, 4, "x"), ann("b", 0, 2, "x")]) == []
    assert find_span_errors([ann("a", 0, 4, "x"), ann("b", 2, 4, "x")]) == []


def test_three_identical_ranges_give_three_pair_errors():
    anns = [ann("a", 1, 3, "x"), ann("b", 1, 3, "x"), ann("c", 1, 3, "x")]
    errors = find_span_errors(anns)
    assert [e["ids"] for e in errors] == [["a", "b"], ["a", "c"], ["b", "c"]]
    assert all(e["type"] == DUPLICATE_SPAN for e in errors)


def test_span_errors_order_is_deterministic():
    # 多条错误按排序后的批注顺序逐对产出，与输入顺序无关
    anns = [ann("b", 2, 5, "x"), ann("a", 0, 3, "x"), ann("c", 1, 3, "x")]
    e1 = find_span_errors(anns)
    e2 = find_span_errors(list(reversed(anns)))
    assert [e["ids"] for e in e1] == [["a", "b"], ["c", "b"]]
    assert all(e["type"] == CROSSING for e in e1)
    assert e1 == e2


# ---------- 栈建树：最窄直接父级 ----------

def test_narrowest_parent_via_stack():
    # leaf 的最窄直接父级是 mid 而非 root
    anns = [ann("root", 0, 10, "x"), ann("mid", 0, 5, "x"), ann("leaf", 1, 3, "x")]
    assert shape(build_tree(anns)) == [("root", [("mid", [("leaf", [])])])]


def test_stack_pops_touching_nodes():
    # [0, 4) 与 [4, 8) 相接 → 并列根；[5, 7) 是 [4, 8) 的子级
    anns = [ann("a", 0, 4, "x"), ann("b", 4, 8, "x"), ann("c", 5, 7, "x")]
    assert shape(build_tree(anns)) == [("a", []), ("b", [("c", [])])]


def test_same_start_wider_end_is_parent():
    # 排序键 (start, -end)：同 start 时范围更宽者先入栈成为父级
    anns = [ann("narrow", 2, 3, "x"), ann("wide", 2, 5, "x")]
    assert shape(build_tree(anns)) == [("wide", [("narrow", [])])]


def test_tree_is_stable_regardless_of_input_order():
    anns = [ann("p", 0, 8, "段落"), ann("s", 3, 7, "短语"), ann("t", 0, 2, "短语")]
    assert shape(build_tree(anns)) == shape(build_tree(list(reversed(anns))))
    assert shape(build_tree(anns)) == [("p", [("t", []), ("s", [])])]


# ---------- label 规则 ----------

def test_root_label_must_be_listed():
    roots = build_tree([ann("a", 0, 4, "段落")])
    rules = validate_rules({"root_labels": ["文档"], "children": {}})
    errors = check_labels(roots, rules)
    assert [e["type"] for e in errors] == [ROOT_LABEL]
    assert errors[0]["ids"] == ["a"]


def test_child_label_must_be_in_direct_parents_children():
    anns = [ann("p", 0, 8, "段落"), ann("s", 1, 3, "章节")]
    roots = build_tree(anns)
    rules = validate_rules({"root_labels": ["段落"], "children": {"段落": ["短语"]}})
    errors = check_labels(roots, rules)
    assert [e["type"] for e in errors] == [CHILD_LABEL]
    assert errors[0]["ids"] == ["p", "s"]


def test_parent_without_children_entry_allows_nothing():
    # 父 label 在 children 中没有条目 → 任何子 label 都不合法
    anns = [ann("p", 0, 8, "段落"), ann("s", 1, 3, "短语")]
    roots = build_tree(anns)
    rules = validate_rules({"root_labels": ["段落"], "children": {}})
    errors = check_labels(roots, rules)
    assert [e["type"] for e in errors] == [CHILD_LABEL]


def test_rules_apply_to_direct_parent_only():
    # 短语允许在句子下；这里直接父级是章节（非句子）→ 仍报错
    anns = [ann("d", 0, 8, "文档"), ann("c", 0, 6, "章节"), ann("s", 1, 3, "短语")]
    roots = build_tree(anns)
    errors = check_labels(roots, RULES)
    assert [e["type"] for e in errors] == [CHILD_LABEL]
    assert errors[0]["ids"] == ["c", "s"]


# ---------- 审计（端到端） ----------

def test_structural_errors_suppress_tree():
    res = audit_nesting("abcdef", [ann("a", 0, 3, "x"), ann("b", 2, 5, "x")], RULES)
    assert res["tree"] is None
    assert [e["type"] for e in res["errors"]] == [CROSSING]


def test_label_errors_keep_tree():
    res = audit_nesting("abcdef", [ann("a", 0, 6, "段落")], RULES)
    assert [e["type"] for e in res["errors"]] == [ROOT_LABEL]
    assert view_shape(res["tree"]) == [("a", [])]
    assert res["tree"][0]["text"] == "abcdef"


def test_clean_audit_has_no_errors():
    text = "目标：统一口径。"
    anns = [ann("p", 0, 8, "段落"), ann("s", 3, 7, "短语")]
    rules = validate_rules({"root_labels": ["段落"], "children": {"段落": ["短语"]}})
    res = audit_nesting(text, anns, rules)
    assert res["errors"] == []
    assert res["tree"] == [
        {"id": "p", "label": "段落", "start": 0, "end": 8, "text": text,
         "children": [
             {"id": "s", "label": "短语", "start": 3, "end": 7,
              "text": "统一口径", "children": []},
         ]},
    ]


def test_empty_annotations():
    assert audit_nesting("abc", [], RULES) == {"errors": [], "tree": []}
    assert export_nesting([]) == []


def test_code_point_offsets_with_astral_chars():
    text = "🎉目标🎉"
    anns = [ann("p", 0, 4, "段落"), ann("s", 1, 3, "短语")]
    rules = validate_rules({"root_labels": ["段落"], "children": {"段落": ["短语"]}})
    res = audit_nesting(text, anns, rules)
    assert res["errors"] == []
    assert res["tree"][0]["children"][0]["text"] == "目标"
    assert export_nesting(anns)[0]["children"][0]["start"] == 1


def test_frontend_demo_example():
    text = "第一章 总则\n本模块负责数据采集与清洗，目标是统一口径。"
    anns = [
        ann("n-doc", 0, 28, "文档"),
        ann("n-ch1", 0, 6, "章节"),
        ann("n-title", 4, 6, "短语"),
        ann("n-body", 6, 28, "段落"),   # 与 n-ch1 端点相接，不算重叠
        ann("n-goal", 20, 27, "句子"),
        ann("n-core", 23, 27, "短语"),
    ]
    rules = validate_rules({
        "root_labels": ["文档"],
        "children": {
            "文档": ["章节"],
            "章节": ["句子"],
            "段落": ["句子", "短语"],
            "句子": ["短语"],
        },
    })
    res = audit_nesting(text, anns, rules)
    assert [e["type"] for e in res["errors"]] == [CHILD_LABEL, CHILD_LABEL]
    assert [e["ids"] for e in res["errors"]] == [["n-ch1", "n-title"], ["n-doc", "n-body"]]
    assert view_shape(res["tree"]) == [
        ("n-doc", [("n-ch1", [("n-title", [])]),
                   ("n-body", [("n-goal", [("n-core", [])])])]),
    ]


# ---------- 导出 ----------

def test_export_keeps_original_id_label_range():
    anns = [ann("p", 0, 8, "段落"), ann("s", 3, 7, "短语")]
    assert export_nesting(anns) == [
        {"id": "p", "label": "段落", "start": 0, "end": 8,
         "children": [
             {"id": "s", "label": "短语", "start": 3, "end": 7, "children": []},
         ]},
    ]


def test_export_children_ordered_by_sort_key():
    # 同父各子按 (start, -end, label, str(id)) 稳定有序，与输入顺序无关
    anns = [
        ann("p", 0, 10, "段落"),
        ann("b", 5, 8, "短语"),
        ann("a", 1, 4, "短语"),
    ]
    (root,) = export_nesting(list(reversed(anns)))
    assert [c["id"] for c in root["children"]] == ["a", "b"]


def test_export_is_stable_regardless_of_input_order():
    anns = [ann("p", 0, 8, "段落"), ann("s", 3, 7, "短语"), ann("t", 0, 2, "短语")]
    assert export_nesting(anns) == export_nesting(list(reversed(anns)))


def test_int_ids_sort_by_str_and_keep_original():
    # id 可为整数：排序键使用 str(id)，导出保留原 id
    anns = [ann(2, 0, 4, "段落"), ann(10, 1, 3, "短语")]
    out = export_nesting(anns)
    assert out[0]["id"] == 2
    assert out[0]["children"][0]["id"] == 10


# ---------- 结构规则校验 ----------

def test_validate_rules_rejects_duplicate_root_labels():
    with pytest.raises(NestingError):
        validate_rules({"root_labels": ["文档", "文档"], "children": {}})


def test_validate_rules_rejects_duplicate_child_labels():
    with pytest.raises(NestingError):
        validate_rules({"root_labels": [], "children": {"段落": ["短语", "短语"]}})


def test_validate_rules_defaults_and_normalizes():
    assert validate_rules({}) == {"root_labels": [], "children": {}}
    rules = validate_rules({"root_labels": ("文档",), "children": {"文档": ("章节",)}})
    assert rules == {"root_labels": ["文档"], "children": {"文档": ["章节"]}}
