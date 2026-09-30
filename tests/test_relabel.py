"""批注标签体系迁移核心逻辑测试（纯标准库，无需启动服务）。"""

import pytest

from app.relabel import (
    CYCLE,
    NO_RULE,
    RelabelError,
    export_labels,
    migrate_labels,
    validate_relabel_rules,
)


def ann(id, start, end, label, **extra):
    return {"id": id, "start": start, "end": end, "label": label, **extra}


def make_rules(target_labels, rules):
    return validate_relabel_rules({"target_labels": target_labels, "rules": rules})


# 与前端「载入示例」一致的数据：多步链 / 条件分流 / 回退 / 合并 / 循环 / 缺规则
DEMO_TEXT = "采集数据，清洗样本，统一口径；保留杂项。"
DEMO_RULES = {
    "target_labels": ["实体", "流程"],
    "rules": [
        {"from": "上古-流程", "to": "旧-流程"},
        {"from": "旧-流程", "to": "流程"},
        {"from": "旧-对象", "to": "实体", "pattern": "数据"},
        {"from": "旧-对象", "to": "流程"},
        {"from": "旧-甲", "to": "实体"},
        {"from": "旧-乙", "to": "实体"},
        {"from": "坏-甲", "to": "坏-乙"},
        {"from": "坏-乙", "to": "坏-甲"},
    ],
}
DEMO_ANNS = [
    ann("r1", 0, 2, "上古-流程"),
    ann("r2", 2, 4, "旧-对象"),
    ann("r3", 5, 7, "旧-对象"),
    ann("r4", 7, 9, "旧-甲"),
    ann("r5", 10, 12, "旧-乙"),
    ann("r6", 12, 14, "坏-甲"),
    ann("r7", 15, 17, "孤立"),
    ann("r8", 17, 19, "实体"),
]


# ---------- 规则集校验 ----------

def test_target_label_empty_or_non_string_rejected():
    with pytest.raises(RelabelError, match="目标标签不能为空"):
        validate_relabel_rules({"target_labels": ["实体", ""], "rules": []})
    with pytest.raises(RelabelError, match="目标标签不能为空"):
        validate_relabel_rules({"target_labels": [None], "rules": []})
    with pytest.raises(RelabelError, match="目标标签不能为空"):
        validate_relabel_rules({"target_labels": [1], "rules": []})


def test_target_label_duplicate_rejected():
    with pytest.raises(RelabelError, match="目标标签重复：实体"):
        validate_relabel_rules({"target_labels": ["实体", "流程", "实体"], "rules": []})


def test_rule_from_to_must_be_non_empty():
    with pytest.raises(RelabelError, match="第 1 条规则的 from 不能为空"):
        validate_relabel_rules({"target_labels": ["T"], "rules": [{"from": "", "to": "T"}]})
    with pytest.raises(RelabelError, match="第 2 条规则的 to 不能为空"):
        validate_relabel_rules({"target_labels": ["T"], "rules": [
            {"from": "A", "to": "T"},
            {"from": "B", "to": ""},
        ]})


def test_invalid_pattern_rejected():
    with pytest.raises(RelabelError, match="非法正则"):
        validate_relabel_rules({"target_labels": ["T"], "rules": [
            {"from": "A", "to": "T", "pattern": "(未闭合"},
        ]})
    with pytest.raises(RelabelError, match="pattern 必须是字符串"):
        validate_relabel_rules({"target_labels": ["T"], "rules": [
            {"from": "A", "to": "T", "pattern": 3},
        ]})


def test_multiple_fallbacks_same_from_rejected():
    with pytest.raises(RelabelError, match="至多一条"):
        validate_relabel_rules({"target_labels": ["T"], "rules": [
            {"from": "A", "to": "T"},
            {"from": "A", "to": "T"},
        ]})


def test_fallback_must_be_last_in_its_from_group():
    with pytest.raises(RelabelError, match="最后"):
        validate_relabel_rules({"target_labels": ["T", "U"], "rules": [
            {"from": "A", "to": "T"},                      # 回退
            {"from": "A", "to": "U", "pattern": "x"},      # 回退之后还有规则
        ]})


def test_fallback_last_is_ok_and_other_from_unaffected():
    rules = make_rules(["T", "U"], [
        {"from": "A", "to": "U", "pattern": "x"},
        {"from": "A", "to": "T"},          # A 的回退，排最后 ✓
        {"from": "B", "to": "T"},          # B 的回退（各自 from 独立）✓
    ])
    assert [r["to"] for r in rules["by_from"]["A"]] == ["U", "T"]
    assert [r["index"] for r in rules["rules"]] == [1, 2, 3]


def test_empty_target_list_allowed_but_annotations_fail_per_item():
    # 空 target_labels 列表本身合法；批注因永远到不了目标体系而逐项报 no_rule
    rules = make_rules([], [{"from": "A", "to": "B"}])
    res = migrate_labels("xy", [ann("a", 0, 1, "A")], rules)
    assert [e["type"] for e in res["errors"]] == [NO_RULE]


# ---------- 求值：命中与分流 ----------

def test_already_in_targets_is_zero_step():
    rules = make_rules(["实体"], [])
    res = migrate_labels("数据采集", [ann("a", 0, 2, "实体")], rules)
    item = res["items"][0]
    assert res["errors"] == []
    assert item["status"] == "done"
    assert item["chain"] == ["实体"]
    assert item["steps"] == []
    assert item["final_label"] == "实体"


def test_single_step_rename():
    rules = make_rules(["流程"], [{"from": "旧-流程", "to": "流程"}])
    res = migrate_labels("采集", [ann("a", 0, 2, "旧-流程")], rules)
    assert res["items"][0]["chain"] == ["旧-流程", "流程"]
    assert res["items"][0]["final_label"] == "流程"


def test_multi_step_chain():
    rules = make_rules(["流程"], [
        {"from": "上古-流程", "to": "旧-流程"},
        {"from": "旧-流程", "to": "流程"},
    ])
    res = migrate_labels("采集", [ann("a", 0, 2, "上古-流程")], rules)
    item = res["items"][0]
    assert item["chain"] == ["上古-流程", "旧-流程", "流程"]
    assert [(s["from"], s["to"], s["index"]) for s in item["steps"]] == [
        ("上古-流程", "旧-流程", 1), ("旧-流程", "流程", 2),
    ]


def test_pattern_fullmatch_requires_entire_excerpt():
    # fullmatch：pattern「数据」只命中原文恰为「数据」的批注
    rules = make_rules(["实体", "流程"], [
        {"from": "旧", "to": "实体", "pattern": "数据"},
        {"from": "旧", "to": "流程"},
    ])
    res = migrate_labels("数据数据集", [
        ann("exact", 0, 2, "旧"),     # 原文「数据」→ 命中
        ann("longer", 0, 4, "旧"),    # 原文「数据数据」→ 未中，走回退
    ], rules)
    assert res["items"][0]["final_label"] == "实体"
    assert res["items"][1]["final_label"] == "流程"


def test_pattern_matches_annotation_text_not_label():
    # pattern 匹配批注原文切片，与 label 内容无关
    rules = make_rules(["实体"], [{"from": "旧", "to": "实体", "pattern": r"\d{4}年"}])
    res = migrate_labels("公元2024年", [ann("a", 2, 7, "旧")], rules)
    assert res["errors"] == []
    assert res["items"][0]["final_label"] == "实体"


def test_conditional_branching_by_excerpt():
    # 条件分流：同 label 不同原文走向不同目标
    rules = make_rules(["实体", "流程"], [
        {"from": "旧-对象", "to": "实体", "pattern": "数据"},
        {"from": "旧-对象", "to": "流程"},
    ])
    res = migrate_labels("数据清洗", [
        ann("a", 0, 2, "旧-对象"),
        ann("b", 2, 4, "旧-对象"),
    ], rules)
    assert [it["final_label"] for it in res["items"]] == ["实体", "流程"]
    assert res["items"][0]["steps"][0]["pattern"] == "数据"
    assert res["items"][1]["steps"][0]["pattern"] is None  # 回退


def test_first_matching_rule_in_declaration_order_wins():
    rules = make_rules(["甲", "乙"], [
        {"from": "旧", "to": "甲", "pattern": "数.*"},
        {"from": "旧", "to": "乙", "pattern": ".*据"},   # 也命中，但声明在后
    ])
    res = migrate_labels("数据", [ann("a", 0, 2, "旧")], rules)
    assert res["items"][0]["final_label"] == "甲"
    assert res["items"][0]["steps"][0]["index"] == 1


def test_label_merge():
    # 标签合并：多个旧 label 汇入同一目标 label
    rules = make_rules(["实体"], [
        {"from": "旧-甲", "to": "实体"},
        {"from": "旧-乙", "to": "实体"},
    ])
    res = migrate_labels("样本统一", [
        ann("a", 0, 2, "旧-甲"),
        ann("b", 2, 4, "旧-乙"),
    ], rules)
    assert [it["final_label"] for it in res["items"]] == ["实体", "实体"]
    assert res["errors"] == []


# ---------- 求值：逐项错误 ----------

def test_no_rule_when_label_unknown():
    rules = make_rules(["实体"], [])
    res = migrate_labels("保留", [ann("a", 0, 2, "孤立")], rules)
    assert [e["type"] for e in res["errors"]] == [NO_RULE]
    item = res["items"][0]
    assert item["status"] == "error"
    assert item["final_label"] is None
    assert item["chain"] == ["孤立"]
    assert "孤立" in res["errors"][0]["message"]


def test_no_rule_when_pattern_misses_and_no_fallback():
    rules = make_rules(["实体"], [{"from": "旧", "to": "实体", "pattern": "数据"}])
    res = migrate_labels("清洗", [ann("a", 0, 2, "旧")], rules)
    assert [e["type"] for e in res["errors"]] == [NO_RULE]
    assert res["errors"][0]["id"] == "a"


def test_no_rule_reports_intermediate_label():
    # 链中间走到没有规则的 label，报错指向该中间 label
    rules = make_rules(["实体"], [{"from": "旧", "to": "中转"}])
    res = migrate_labels("采集", [ann("a", 0, 2, "旧")], rules)
    assert res["errors"][0]["type"] == NO_RULE
    assert res["errors"][0]["label"] == "中转"
    assert res["items"][0]["chain"] == ["旧", "中转"]


def test_self_loop_is_cycle():
    rules = make_rules(["实体"], [{"from": "旧", "to": "旧"}])
    res = migrate_labels("采集", [ann("a", 0, 2, "旧")], rules)
    assert [e["type"] for e in res["errors"]] == [CYCLE]
    assert res["items"][0]["chain"] == ["旧", "旧"]


def test_two_label_cycle():
    rules = make_rules(["实体"], [
        {"from": "坏-甲", "to": "坏-乙"},
        {"from": "坏-乙", "to": "坏-甲"},
    ])
    res = migrate_labels("口径", [ann("a", 0, 2, "坏-甲")], rules)
    assert [e["type"] for e in res["errors"]] == [CYCLE]
    assert res["errors"][0]["label"] == "坏-甲"
    assert res["items"][0]["chain"] == ["坏-甲", "坏-乙", "坏-甲"]


def test_longer_cycle_back_to_middle():
    rules = make_rules(["实体"], [
        {"from": "A", "to": "B"},
        {"from": "B", "to": "C"},
        {"from": "C", "to": "B"},   # B → C → B 循环（不经过起点）
    ])
    res = migrate_labels("采集", [ann("a", 0, 2, "A")], rules)
    assert [e["type"] for e in res["errors"]] == [CYCLE]
    assert res["errors"][0]["label"] == "B"
    assert res["items"][0]["chain"] == ["A", "B", "C", "B"]


def test_chain_ending_in_target_never_cycles():
    # 目标 label 即使被规则引用也不会被判循环（到达即结束）
    rules = make_rules(["实体"], [
        {"from": "旧", "to": "实体"},
        {"from": "实体", "to": "旧"},   # 死规则：到达实体即结束，不会触发
    ])
    res = migrate_labels("数据", [ann("a", 0, 2, "旧")], rules)
    assert res["errors"] == []
    assert res["items"][0]["chain"] == ["旧", "实体"]


def test_errors_are_per_item_and_do_not_stop_others():
    rules = make_rules(["实体", "流程"], [
        {"from": "旧", "to": "实体"},
        {"from": "坏", "to": "坏"},
    ])
    res = migrate_labels("数据采集保留", [
        ann("ok", 0, 2, "旧"),
        ann("bad-cycle", 2, 4, "坏"),
        ann("bad-norule", 4, 6, "孤立"),
        ann("ok2", 0, 2, "旧"),
    ], rules)
    assert [(e["id"], e["type"]) for e in res["errors"]] == [
        ("bad-cycle", CYCLE), ("bad-norule", NO_RULE),
    ]
    assert [it["status"] for it in res["items"]] == ["done", "error", "error", "done"]
    assert res["items"][0]["final_label"] == "实体"


# ---------- 导出 ----------

def test_export_replaces_label_only_and_keeps_order_and_fields():
    rules = make_rules(["实体", "流程"], [
        {"from": "旧-对象", "to": "实体", "pattern": "数据"},
        {"from": "旧-对象", "to": "流程"},
    ])
    anns = [
        ann("z", 2, 4, "旧-对象", note="保留我", meta={"k": [1]}),
        ann("a", 0, 2, "旧-对象"),
        ann(7, 0, 2, "流程"),   # 整数 id、已是目标标签
    ]
    res = export_labels("数据清洗", anns, rules)
    assert res["errors"] == []
    assert [a["id"] for a in res["annotations"]] == ["z", "a", 7]  # 输入顺序不变
    assert res["annotations"][0] == {
        "id": "z", "start": 2, "end": 4, "label": "流程",
        "note": "保留我", "meta": {"k": [1]},
    }
    assert res["annotations"][1]["label"] == "实体"
    assert res["annotations"][2] == anns[2]  # 目标标签原样保留
    # 不改动入参
    assert anns[0]["label"] == "旧-对象"


def test_export_blocked_by_any_item_error():
    rules = make_rules(["实体"], [{"from": "坏", "to": "坏"}])
    res = export_labels("数据采集", [
        ann("ok", 0, 2, "实体"),
        ann("bad", 2, 4, "坏"),
    ], rules)
    assert res["annotations"] == []
    assert [e["id"] for e in res["errors"]] == ["bad"]


# ---------- 前端示例（与「载入示例」一致） ----------

def test_frontend_demo_example():
    rules = validate_relabel_rules(DEMO_RULES)
    res = migrate_labels(DEMO_TEXT, DEMO_ANNS, rules)
    chains = {it["id"]: it["chain"] for it in res["items"]}
    assert chains == {
        "r1": ["上古-流程", "旧-流程", "流程"],   # 多步链
        "r2": ["旧-对象", "实体"],                # 条件分流：pattern 命中
        "r3": ["旧-对象", "流程"],                # 回退
        "r4": ["旧-甲", "实体"],                  # 合并
        "r5": ["旧-乙", "实体"],                  # 合并
        "r6": ["坏-甲", "坏-乙", "坏-甲"],        # 循环
        "r7": ["孤立"],                           # 缺少可用规则
        "r8": ["实体"],                           # 已是目标标签
    }
    assert [(e["id"], e["type"]) for e in res["errors"]] == [
        ("r6", CYCLE), ("r7", NO_RULE),
    ]
    finals = {it["id"]: it["final_label"] for it in res["items"]}
    assert finals == {
        "r1": "流程", "r2": "实体", "r3": "流程", "r4": "实体",
        "r5": "实体", "r6": None, "r7": None, "r8": "实体",
    }
    # 存在错误 → 导出为空
    out = export_labels(DEMO_TEXT, DEMO_ANNS, rules)
    assert out["annotations"] == []
    assert len(out["errors"]) == 2


def test_frontend_demo_export_after_fixing_errors():
    # 去掉 r6 / r7 后示例可整体导出，label 被替换、范围与顺序不变
    anns = [a for a in DEMO_ANNS if a["id"] not in {"r6", "r7"}]
    rules = validate_relabel_rules(DEMO_RULES)
    out = export_labels(DEMO_TEXT, anns, rules)
    assert out["errors"] == []
    assert [(a["id"], a["label"]) for a in out["annotations"]] == [
        ("r1", "流程"), ("r2", "实体"), ("r3", "流程"),
        ("r4", "实体"), ("r5", "实体"), ("r8", "实体"),
    ]
    assert [(a["start"], a["end"]) for a in out["annotations"]] == [
        (0, 2), (2, 4), (5, 7), (7, 9), (10, 12), (17, 19),
    ]
