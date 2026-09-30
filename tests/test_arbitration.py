"""批注集仲裁核心逻辑测试（纯标准库，无需启动服务）。"""

import pytest

from app.arbitration import (
    AUTO,
    PENDING,
    ArbitrationError,
    apply_arbitration,
    build_groups,
)


def ann(id, start, end, label):
    return {"id": id, "start": start, "end": end, "label": label}


def test_touching_intervals_do_not_connect():
    # [0, 2) 与 [2, 4) 端点相接，交集长度为 0 → 不连边，各自成组
    text = "abcdef"
    groups = build_groups(text, [ann("L1", 0, 2, "x")], [ann("R1", 2, 4, "x")])
    assert len(groups) == 2
    assert all(g["status"] == PENDING for g in groups)  # 均为单侧分量


def test_overlap_length_must_be_positive():
    text = "abcdef"
    groups = build_groups(text, [ann("L1", 0, 3, "x")], [ann("R1", 2, 4, "x")])
    assert len(groups) == 1  # 交集 [2, 3) 长度 1 > 0 → 同组


def test_auto_only_when_identical_one_to_one():
    text = "目标是统一口径。"
    groups = build_groups(text, [ann("L1", 0, 4, "目标")], [ann("R1", 0, 4, "目标")])
    assert len(groups) == 1
    assert groups[0]["status"] == AUTO


def test_same_range_different_label_is_pending():
    text = "目标是统一口径。"
    groups = build_groups(text, [ann("L1", 0, 4, "目标")], [ann("R1", 0, 4, "重点")])
    assert len(groups) == 1
    assert groups[0]["status"] == PENDING


def test_same_label_different_range_is_pending():
    text = "目标是统一口径。"
    groups = build_groups(text, [ann("L1", 0, 4, "目标")], [ann("R1", 0, 5, "目标")])
    assert len(groups) == 1
    assert groups[0]["status"] == PENDING


def test_one_to_many_stays_single_group():
    # 一条左侧与两条右侧重叠 → 一个 1v2 分量，不允许逐条贪心拆开
    text = "第二段 保持不变的内容。"
    left = [ann("L1", 0, 11, "段落")]
    right = [ann("R1", 0, 3, "段首"), ann("R2", 4, 10, "共识")]
    groups = build_groups(text, left, right)
    assert len(groups) == 1
    g = groups[0]
    assert g["status"] == PENDING
    assert [a["id"] for a in g["left"]] == ["L1"]
    assert [a["id"] for a in g["right"]] == ["R1", "R2"]


def test_many_to_many_via_transitive_chain():
    # L1∩R1、R1∩L2、L2∩R2 → 传递连通为一个 2v2 分量
    text = "aaaabbbbccccddddeeee"
    left = [ann("L1", 0, 6, "x"), ann("L2", 5, 14, "y")]
    right = [ann("R1", 4, 8, "p"), ann("R2", 12, 16, "q")]
    groups = build_groups(text, left, right)
    assert len(groups) == 1
    g = groups[0]
    assert len(g["left"]) == 2 and len(g["right"]) == 2
    assert g["status"] == PENDING


def test_single_side_components_are_pending():
    text = "左侧独有。右侧独有。"
    groups = build_groups(text, [ann("L1", 0, 4, "遗留")], [ann("R1", 5, 9, "新增")])
    assert len(groups) == 2
    by_side = {("l" if g["left"] else "r"): g for g in groups}
    assert by_side["l"]["status"] == PENDING
    assert by_side["r"]["status"] == PENDING


def test_group_ids_are_stable_and_ordered():
    text = "甲乙丙丁戊己"
    left = [ann("L2", 4, 6, "b"), ann("L1", 0, 2, "a")]
    groups = build_groups(text, left, [])
    assert [g["group_id"] for g in groups] == ["g1", "g2"]
    assert groups[0]["left"][0]["id"] == "L1"  # 按 start 排序


def test_view_includes_excerpt():
    text = "目标是统一口径。"
    groups = build_groups(text, [ann("L1", 0, 4, "目标")], [])
    assert groups[0]["left"][0]["text"] == "目标是统"


def _demo():
    """覆盖全部分支的输入：auto / 1v1 冲突 / 1v2 / 2v2 / 左单侧 / 右单侧。"""
    text = "数据采集与清洗，目标是统一口径。第二段内容。甲乙丙丁。左侧独有。右侧独有。"
    left = [
        ann("L-clean", 0, 7, "流程"),      # 与 R-clean 同范围、label 冲突
        ann("L-goal", 8, 15, "目标"),      # 与 R-goal 完全一致 → auto
        ann("L-para", 16, 21, "段落"),     # 1v2
        ann("L-a", 22, 24, "甲"),          # 2v2
        ann("L-b", 23, 26, "乙"),
        ann("L-old", 27, 31, "遗留"),      # 左单侧
    ]
    right = [
        ann("R-clean", 0, 7, "核心"),
        ann("R-goal", 8, 15, "目标"),
        ann("R-p1", 16, 19, "段首"),
        ann("R-p2", 19, 21, "段尾"),
        ann("R-a", 22, 25, "丙"),
        ann("R-b", 24, 26, "丁"),
        ann("R-new", 32, 36, "新增"),      # 右单侧
    ]
    return text, left, right


def test_demo_groups_cover_all_branches():
    text, left, right = _demo()
    groups = build_groups(text, left, right)
    assert len(groups) == 6
    autos = [g for g in groups if g["status"] == AUTO]
    assert len(autos) == 1
    assert autos[0]["left"][0]["id"] == "L-goal"
    sizes = sorted((len(g["left"]), len(g["right"])) for g in groups)
    assert sizes == [(0, 1), (1, 0), (1, 1), (1, 1), (1, 2), (2, 2)]


def test_export_sorts_and_renumbers():
    text, left, right = _demo()
    groups = build_groups(text, left, right)
    decisions = {g["group_id"]: "right" for g in groups if g["status"] == PENDING}
    annotations, records, unresolved = apply_arbitration(groups, decisions)
    assert unresolved == []
    # 新 id 依次生成，且按 start、end、label、来源 id 稳定排序
    assert [a["id"] for a in annotations] == [f"ann-{i}" for i in range(1, len(annotations) + 1)]
    keys = [(a["start"], a["end"], a["label"], str(a["source_id"])) for a in annotations]
    assert keys == sorted(keys)
    # auto 组取一条、source 为 both；right 决策组取右侧
    auto_ann = next(a for a in annotations if a["label"] == "目标")
    assert auto_ann["source"] == "both"
    assert auto_ann["source_id"] == "L-goal"
    assert all(a["source"] == "right" for a in annotations if a["label"] in {"核心", "段首"})


def test_export_decision_records_per_group():
    text, left, right = _demo()
    groups = build_groups(text, left, right)
    decisions = {}
    for g in groups:
        if g["status"] == PENDING:
            decisions[g["group_id"]] = "discard"
    _, records, unresolved = apply_arbitration(groups, decisions)
    assert unresolved == []
    assert len(records) == len(groups)
    for rec, g in zip(records, groups):
        assert rec["left_ids"] == [a["id"] for a in g["left"]]
        assert rec["right_ids"] == [a["id"] for a in g["right"]]
        assert rec["decision"] == (AUTO if g["status"] == AUTO else "discard")


def test_discard_exports_nothing_for_group():
    text, left, right = _demo()
    groups = build_groups(text, left, right)
    decisions = {g["group_id"]: "discard" for g in groups if g["status"] == PENDING}
    annotations, _, _ = apply_arbitration(groups, decisions)
    assert [a["label"] for a in annotations] == ["目标"]  # 只剩 auto 组


def test_unresolved_pending_groups_listed():
    text, left, right = _demo()
    groups = build_groups(text, left, right)
    pending = [g["group_id"] for g in groups if g["status"] == PENDING]
    _, _, unresolved = apply_arbitration(groups, {pending[0]: "left"})
    assert unresolved == pending[1:]


def test_decision_on_auto_group_rejected():
    text, left, right = _demo()
    groups = build_groups(text, left, right)
    auto_gid = next(g["group_id"] for g in groups if g["status"] == AUTO)
    with pytest.raises(ArbitrationError):
        apply_arbitration(groups, {auto_gid: "left"})


def test_unknown_group_rejected():
    text, left, right = _demo()
    groups = build_groups(text, left, right)
    with pytest.raises(ArbitrationError):
        apply_arbitration(groups, {"g999": "left"})


def test_unknown_decision_value_rejected():
    text, left, right = _demo()
    groups = build_groups(text, left, right)
    gid = next(g["group_id"] for g in groups if g["status"] == PENDING)
    with pytest.raises(ArbitrationError):
        apply_arbitration(groups, {gid: "merge"})


def test_left_decision_picks_left_side():
    text, left, right = _demo()
    groups = build_groups(text, left, right)
    decisions = {}
    for g in groups:
        if g["status"] == PENDING:
            # 1v2 组用左侧，其余舍弃
            decisions[g["group_id"]] = "left" if len(g["right"]) == 2 and len(g["left"]) == 1 else "discard"
    annotations, _, _ = apply_arbitration(groups, decisions)
    para = next(a for a in annotations if a["label"] == "段落")
    assert para["source"] == "left" and para["source_id"] == "L-para"
    assert not any(a["label"] in {"段首", "段尾"} for a in annotations)


def test_code_point_offsets_with_astral_chars():
    text = "🎉🎉目标🎉🎉"
    groups = build_groups(text, [ann("L1", 2, 4, "目标")], [ann("R1", 2, 4, "目标")])
    assert groups[0]["status"] == AUTO
    assert groups[0]["left"][0]["text"] == "目标"
    annotations, _, _ = apply_arbitration(groups, {})
    assert annotations[0]["start"] == 2 and annotations[0]["end"] == 4
