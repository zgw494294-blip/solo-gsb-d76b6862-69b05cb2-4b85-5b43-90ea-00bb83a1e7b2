"""迁移核心逻辑测试（纯标准库，无需启动服务）。"""

import pytest

from app.migration import (
    ABANDONED,
    DISAMBIGUATED,
    MANUAL,
    MATCHED,
    PENDING,
    SHIFTED,
    DecisionError,
    apply_decisions,
    find_all,
    levenshtein,
    migrate_annotations,
)


def test_levenshtein_basic():
    assert levenshtein("", "") == 0
    assert levenshtein("abc", "abc") == 0
    assert levenshtein("abc", "") == 3
    assert levenshtein("kitten", "sitting") == 3
    assert levenshtein("第一章", "第二章") == 1


def test_find_all_overlapping():
    assert find_all("aaaa", "aa") == [0, 1, 2]
    assert find_all("abcabc", "abc") == [0, 3]
    assert find_all("abc", "x") == []


def test_shifted_inside_matching_block():
    old = "第一段：旧版说明。第二段：保持不变。"
    new = "第一段：全新的新版说明。第二段：保持不变。"
    # “保持不变”在旧版 [13, 17)，新版整体右移 3 个码点
    res = migrate_annotations(old, new, [{"id": "a1", "start": 13, "end": 17}])
    assert res[0]["status"] == SHIFTED
    assert (res[0]["new_start"], res[0]["new_end"]) == (16, 20)
    assert new[16:20] == "保持不变"


def test_matched_unique_hit():
    old = "口号：安全第一。标语：安全第一。"
    new = "口号：安全至上。标语：安全第一。"
    res = migrate_annotations(old, new, [{"id": "m", "start": 3, "end": 7}])  # 第一个“安全第一”
    assert res[0]["status"] == MATCHED
    assert new[res[0]["new_start"]:res[0]["new_end"]] == "安全第一"
    assert res[0]["new_start"] == new.find("安全第一")


def test_disambiguated_by_context():
    old = "甲章\n备注：暂无。\n乙章\n备注：暂无。\n丙章\n备注：暂无。\n"
    new = "甲章\n备注：已填。\n乙章\n备注：暂无。\n丙章\n备注：暂无。\n"
    res = migrate_annotations(old, new, [{"id": "d", "start": 3, "end": 9}])  # 甲章的“备注：暂无。”
    assert res[0]["status"] == DISAMBIGUATED
    assert new[res[0]["new_start"]:res[0]["new_end"]] == "备注：暂无。"
    # 两处命中（乙章、丙章），乙章上下文与旧版最接近
    assert res[0]["new_start"] == new.find("备注：暂无。")
    assert len(res[0]["candidates"]) == 2


def test_pending_on_tie():
    # 新版中两处命中，前后 20 码点上下文完全一致 → 并列待确认
    block = "起" * 20 + "目标文本" + "止" * 20
    old = block + "间隔" + block + "旧位：目标文本。"
    new = block + "间隔" + block + "旧位：已改写。"
    start = old.rfind("目标文本")
    res = migrate_annotations(old, new, [{"id": "t", "start": start, "end": start + 4}])
    assert res[0]["status"] == PENDING
    assert len(res[0]["candidates"]) == 2
    assert res[0]["candidates"][0]["distance"] == res[0]["candidates"][1]["distance"]


def test_pending_on_no_hit():
    old = "这段文字在新版中被整体删除。"
    new = "全新的内容。"
    res = migrate_annotations(old, new, [{"id": "n", "start": 0, "end": 6}])
    assert res[0]["status"] == PENDING
    assert res[0]["candidates"] == []


def test_code_point_offsets_with_astral_chars():
    # emoji 等为单码点；Python 切片天然按码点
    old = "🎉🎉目标🎉🎉"
    new = "🎉🎉🎉目标🎉🎉"
    res = migrate_annotations(old, new, [{"id": "e", "start": 2, "end": 4}])
    assert res[0]["status"] == SHIFTED
    assert (res[0]["new_start"], res[0]["new_end"]) == (3, 5)
    assert new[3:5] == "目标"


def _results_with_pending():
    old = "这段文字在新版中被整体删除。"
    new = "全新的内容。"
    return migrate_annotations(old, new, [{"id": "n", "start": 0, "end": 6}])


def test_apply_decisions_unresolved():
    exported, unresolved = apply_decisions(_results_with_pending(), {})
    assert exported == []
    assert unresolved == ["n"]


def test_apply_decisions_abandon():
    exported, unresolved = apply_decisions(_results_with_pending(), {"n": {"action": "abandon"}})
    assert unresolved == []
    assert exported == [{"id": "n", "start": None, "end": None, "status": ABANDONED}]


def test_apply_decisions_choose_and_invalid():
    block = "起" * 20 + "目标文本" + "止" * 20
    old = block + "间隔" + block + "旧位：目标文本。"
    new = block + "间隔" + block + "旧位：已改写。"
    start = old.rfind("目标文本")
    res = migrate_annotations(old, new, [{"id": "t", "start": start, "end": start + 4}])
    cand = res[0]["candidates"][0]

    exported, unresolved = apply_decisions(
        res, {"t": {"action": "choose", "start": cand["start"], "end": cand["end"]}}
    )
    assert unresolved == []
    assert exported == [{"id": "t", "start": cand["start"], "end": cand["end"], "status": MANUAL}]

    with pytest.raises(DecisionError):
        apply_decisions(res, {"t": {"action": "choose", "start": 0, "end": 4}})
