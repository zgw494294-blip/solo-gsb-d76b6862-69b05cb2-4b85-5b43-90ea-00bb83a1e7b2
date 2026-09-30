"""批注输入校验策略的表驱动契约测试（无需启动服务）。

覆盖 app.validation 中每个策略的：
- 合法值：输出字段、顺序、额外字段保留方式与唯一性作用域符合契约；
- 首个失败项：多处违规时按（列表顺序 → 输入顺序 → 单条内 id 类型、区间、
  label、重复 id）短路，报出首个失败项的精确 422 detail；
以及各路由函数与策略的接线（直接调用，断言状态码与 detail 不变）。
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import main
from app.validation import (
    ARBITRATE,
    LABELED,
    MIGRATE,
    RELABEL,
    REVIEW,
    UNITS,
    validate,
)


class Ann(SimpleNamespace):
    """最小批注桩：validate 只访问 id / start / end / label 与 model_dump()。"""

    def model_dump(self):
        return dict(self.__dict__)


def req_of(**kwargs):
    return SimpleNamespace(**kwargs)


# ---------- 策略声明：文本长度来源、区间开关、label 要求、id 类型与
# ---------- 唯一性作用域、额外字段保留方式、错误前缀 ----------

def test_strategy_declarations():
    # 迁移：区间落在旧版文本，固定输出 id/start/end，不要求 label
    assert MIGRATE.text_attr == "old_text"
    assert MIGRATE.length_label == "旧版文本长度"
    assert MIGRATE.check_interval is True
    assert MIGRATE.require_label is False
    assert MIGRATE.fields == ("id", "start", "end")

    # 序列 / 嵌套：区间 + 非空 label + 固定输出字段
    assert LABELED.text_attr == "text"
    assert LABELED.length_label == "文本长度"
    assert LABELED.check_interval is True
    assert LABELED.require_label is True
    assert LABELED.id_type == (str, int)
    assert LABELED.fields == ("id", "start", "end", "label")

    # 审阅：只收字符串 id
    assert REVIEW.id_type == (str,)

    # 制式转换：不查区间、不要求 label、保留全部字段（含额外字段）
    assert UNITS.check_interval is False
    assert UNITS.require_label is False
    assert UNITS.fields is None

    # 标签体系迁移：查区间、要求 label，且保留全部字段（导出只替换 label）
    assert RELABEL.check_interval is True
    assert RELABEL.require_label is True
    assert RELABEL.fields is None

    # 仲裁：左右两个列表分别校验、各自判重，错误带侧别前缀
    assert ARBITRATE.id_scope == "list"
    assert [(s.attr, s.prefix) for s in ARBITRATE.lists] == [
        ("left", "左侧"), ("right", "右侧")]

    # 其余策略：单一 annotations 列表、整请求判重、无前缀
    for strategy in (MIGRATE, LABELED, REVIEW, UNITS, RELABEL):
        assert strategy.id_scope == "request"
        assert [(s.attr, s.prefix) for s in strategy.lists] == [("annotations", "")]


# ---------- 合法值 ----------

@pytest.mark.parametrize(
    "strategy, req, expected",
    [
        pytest.param(
            MIGRATE,
            req_of(old_text="旧版文本", new_text="新",
                   annotations=[Ann(id="a1", start=0, end=4),
                                Ann(id=2, start=1, end=3)]),
            [[{"id": "a1", "start": 0, "end": 4},
              {"id": 2, "start": 1, "end": 3}]],
            id="migrate：end 可达旧版末尾、整数 id、输出仅 id/start/end",
        ),
        pytest.param(
            MIGRATE,
            req_of(old_text="abcd", new_text="abcd",
                   annotations=[Ann(id="1", start=0, end=1),
                                Ann(id=1, start=1, end=2)]),
            [[{"id": "1", "start": 0, "end": 1},
              {"id": 1, "start": 1, "end": 2}]],
            id="migrate：字符串 '1' 与整数 1 是不同 id",
        ),
        pytest.param(
            LABELED,
            req_of(text="abcdef",
                   annotations=[Ann(id="x", start=0, end=1, label="L"),
                                Ann(id=7, start=2, end=6, label="词")]),
            [[{"id": "x", "start": 0, "end": 1, "label": "L"},
              {"id": 7, "start": 2, "end": 6, "label": "词"}]],
            id="labeled：输出含 label，整数 id 合法",
        ),
        pytest.param(
            REVIEW,
            req_of(text="abcdef",
                   annotations=[Ann(id="r1", start=0, end=2, label="L"),
                                Ann(id="r2", start=2, end=6, label="M")]),
            [[{"id": "r1", "start": 0, "end": 2, "label": "L"},
              {"id": "r2", "start": 2, "end": 6, "label": "M"}]],
            id="review：字符串 id 合法",
        ),
        pytest.param(
            UNITS,
            req_of(text="abc",
                   annotations=[Ann(id="u1", start=0, end=99, label="越界放行", note="保留"),
                                Ann(id=5, start=-3, end=-1)]),
            [[{"id": "u1", "start": 0, "end": 99, "label": "越界放行", "note": "保留"},
              {"id": 5, "start": -3, "end": -1}]],
            id="units：不查区间与 label，额外字段原样保留",
        ),
        pytest.param(
            RELABEL,
            req_of(text="abcd",
                   annotations=[Ann(id="r1", start=0, end=2, label="旧", note="保留"),
                                Ann(id=8, start=2, end=4, label="新")]),
            [[{"id": "r1", "start": 0, "end": 2, "label": "旧", "note": "保留"},
              {"id": 8, "start": 2, "end": 4, "label": "新"}]],
            id="relabel：查区间与 label，额外字段原样保留",
        ),
        pytest.param(
            ARBITRATE,
            req_of(text="abcd",
                   left=[Ann(id="same", start=0, end=2, label="L")],
                   right=[Ann(id="same", start=1, end=3, label="R")]),
            [[{"id": "same", "start": 0, "end": 2, "label": "L"}],
             [{"id": "same", "start": 1, "end": 3, "label": "R"}]],
            id="arbitrate：左右侧分别判重，同一 id 可分居两侧",
        ),
    ],
)
def test_valid(strategy, req, expected):
    assert validate(req, strategy) == expected


# ---------- 首个失败项（精确 422 detail，含短路顺序） ----------

@pytest.mark.parametrize(
    "strategy, req, detail",
    [
        # 迁移：区间错误以旧版文本长度为准
        pytest.param(
            MIGRATE,
            req_of(old_text="abcd", new_text="abcdef",
                   annotations=[Ann(id="bad", start=2, end=5)]),
            "批注 bad 的区间 [2, 5) 非法或超出旧版文本长度 4",
            id="migrate：end 超出旧版文本（新版更长也不算）",
        ),
        pytest.param(
            MIGRATE,
            req_of(old_text="abcd", new_text="",
                   annotations=[Ann(id="bad", start=2, end=2)]),
            "批注 bad 的区间 [2, 2) 非法或超出旧版文本长度 4",
            id="migrate：start == end",
        ),
        pytest.param(
            MIGRATE,
            req_of(old_text="abcd", new_text="",
                   annotations=[Ann(id="bad", start=-1, end=2)]),
            "批注 bad 的区间 [-1, 2) 非法或超出旧版文本长度 4",
            id="migrate：start 为负",
        ),
        pytest.param(
            MIGRATE,
            req_of(old_text="abcd", new_text="",
                   annotations=[Ann(id="x", start=0, end=1),
                                Ann(id="x", start=1, end=2)]),
            "批注 id 重复：x",
            id="migrate：id 重复",
        ),
        pytest.param(
            MIGRATE,
            req_of(old_text="abcd", new_text="",
                   annotations=[Ann(id="first", start=0, end=9),
                                Ann(id="first", start=0, end=1)]),
            "批注 first 的区间 [0, 9) 非法或超出旧版文本长度 4",
            id="migrate：首条即失败，后续重复 id 不再报",
        ),
        # 序列 / 嵌套：单条内依次为区间 → label → 重复 id
        pytest.param(
            LABELED,
            req_of(text="abcd",
                   annotations=[Ann(id="a", start=0, end=9, label="")]),
            "批注 a 的区间 [0, 9) 非法或超出文本长度 4",
            id="labeled：区间先于空 label",
        ),
        pytest.param(
            LABELED,
            req_of(text="abcd",
                   annotations=[Ann(id="a", start=0, end=1, label="")]),
            "批注 a 的 label 不能为空",
            id="labeled：空 label",
        ),
        pytest.param(
            LABELED,
            req_of(text="abcd",
                   annotations=[Ann(id="x", start=0, end=1, label="L"),
                                Ann(id="x", start=1, end=2, label="")]),
            "批注 x 的 label 不能为空",
            id="labeled：label 先于重复 id",
        ),
        pytest.param(
            LABELED,
            req_of(text="abcd",
                   annotations=[Ann(id="x", start=0, end=1, label="L"),
                                Ann(id="x", start=1, end=2, label="M")]),
            "批注 id 重复：x",
            id="labeled：id 重复",
        ),
        # 审阅：只收字符串 id，且 id 类型先于区间
        pytest.param(
            REVIEW,
            req_of(text="abcd",
                   annotations=[Ann(id=123, start=0, end=9, label="L")]),
            "批注 id 类型非法：123（须为 str）",
            id="review：非字符串 id，类型先于区间",
        ),
        pytest.param(
            REVIEW,
            req_of(text="abcd",
                   annotations=[Ann(id="a", start=0, end=9, label="L")]),
            "批注 a 的区间 [0, 9) 非法或超出文本长度 4",
            id="review：区间非法",
        ),
        pytest.param(
            REVIEW,
            req_of(text="abcd",
                   annotations=[Ann(id="a", start=0, end=1, label="L"),
                                Ann(id="a", start=1, end=2, label="M")]),
            "批注 id 重复：a",
            id="review：id 重复",
        ),
        # 制式转换：只判重（边界错误留给 app.units）
        pytest.param(
            UNITS,
            req_of(text="abc",
                   annotations=[Ann(id="u", start=0, end=1),
                                Ann(id="u", start=1, end=2)]),
            "批注 id 重复：u",
            id="units：id 重复",
        ),
        # 标签体系迁移：查区间、要求 label、判重
        pytest.param(
            RELABEL,
            req_of(text="abcd",
                   annotations=[Ann(id="r", start=0, end=9, label="旧")]),
            "批注 r 的区间 [0, 9) 非法或超出文本长度 4",
            id="relabel：区间非法",
        ),
        pytest.param(
            RELABEL,
            req_of(text="abcd",
                   annotations=[Ann(id="r", start=0, end=1, label="")]),
            "批注 r 的 label 不能为空",
            id="relabel：空 label",
        ),
        pytest.param(
            RELABEL,
            req_of(text="abcd",
                   annotations=[Ann(id="r", start=0, end=1, label="旧"),
                                Ann(id="r", start=1, end=2, label="新")]),
            "批注 id 重复：r",
            id="relabel：id 重复",
        ),
        # 仲裁：侧别前缀、各自判重、左侧先于右侧
        pytest.param(
            ARBITRATE,
            req_of(text="abcd",
                   left=[Ann(id="L1", start=0, end=9, label="x")],
                   right=[Ann(id="R1", start=0, end=9, label="y")]),
            "左侧批注 L1 的区间 [0, 9) 非法或超出文本长度 4",
            id="arbitrate：左侧先于右侧",
        ),
        pytest.param(
            ARBITRATE,
            req_of(text="abcd",
                   left=[Ann(id="L1", start=0, end=1, label="x")],
                   right=[Ann(id="R1", start=0, end=9, label="y")]),
            "右侧批注 R1 的区间 [0, 9) 非法或超出文本长度 4",
            id="arbitrate：右侧区间错误带右侧前缀",
        ),
        pytest.param(
            ARBITRATE,
            req_of(text="abcd",
                   left=[Ann(id="L", start=0, end=1, label="a"),
                         Ann(id="L", start=1, end=2, label="b")],
                   right=[]),
            "左侧批注 id 重复：L",
            id="arbitrate：左侧内部判重",
        ),
        pytest.param(
            ARBITRATE,
            req_of(text="abcd",
                   left=[],
                   right=[Ann(id="R", start=0, end=1, label=""),
                          Ann(id="R", start=1, end=2, label="b")]),
            "右侧批注 R 的 label 不能为空",
            id="arbitrate：右侧 label 先于重复 id",
        ),
    ],
)
def test_first_failure(strategy, req, detail):
    with pytest.raises(HTTPException) as exc_info:
        validate(req, strategy)
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == detail


# ---------- 接口接线：每个路由函数接入对应策略，状态码与 detail 不变 ----------

@pytest.mark.parametrize(
    "endpoint, req, detail",
    [
        pytest.param(
            main.migrate,
            main.MigrateRequest(old_text="ab", new_text="ab",
                                annotations=[{"id": "x", "start": 0, "end": 9}]),
            "批注 x 的区间 [0, 9) 非法或超出旧版文本长度 2",
            id="/api/migrate",
        ),
        pytest.param(
            main.export,
            main.ExportRequest(old_text="ab", new_text="ab",
                               annotations=[{"id": "x", "start": 0, "end": 9}]),
            "批注 x 的区间 [0, 9) 非法或超出旧版文本长度 2",
            id="/api/export",
        ),
        pytest.param(
            main.arbitrate,
            main.ArbitrateRequest(
                text="ab",
                left=[{"id": "L", "start": 0, "end": 9, "label": "x"}]),
            "左侧批注 L 的区间 [0, 9) 非法或超出文本长度 2",
            id="/api/arbitrate",
        ),
        pytest.param(
            main.arbitrate_export,
            main.ArbitrateExportRequest(
                text="ab",
                right=[{"id": "R", "start": 0, "end": 1, "label": ""}]),
            "右侧批注 R 的 label 不能为空",
            id="/api/arbitrate/export",
        ),
        pytest.param(
            main.sequence_preview,
            main.SequenceRequest(
                text="ab",
                annotations=[{"id": "x", "start": 0, "end": 9, "label": "L"}]),
            "批注 x 的区间 [0, 9) 非法或超出文本长度 2",
            id="/api/sequence/preview",
        ),
        pytest.param(
            main.sequence_export,
            main.SequenceRequest(
                text="ab",
                annotations=[{"id": "x", "start": 0, "end": 1, "label": "L"},
                             {"id": "x", "start": 0, "end": 1, "label": "M"}]),
            "批注 id 重复：x",
            id="/api/sequence/export",
        ),
        pytest.param(
            main.nesting_preview,
            main.NestingRequest(
                text="ab",
                annotations=[{"id": "x", "start": 0, "end": 1, "label": ""}]),
            "批注 x 的 label 不能为空",
            id="/api/nesting/preview",
        ),
        pytest.param(
            main.nesting_export,
            main.NestingRequest(
                text="ab",
                annotations=[{"id": "x", "start": 1, "end": 1, "label": "L"}]),
            "批注 x 的区间 [1, 1) 非法或超出文本长度 2",
            id="/api/nesting/export",
        ),
        pytest.param(
            main.units_preview,
            main.UnitsRequest(
                text="ab", source_unit="codepoint", target_unit="utf16",
                annotations=[{"id": "u", "start": 0, "end": 1},
                             {"id": "u", "start": 0, "end": 1}]),
            "批注 id 重复：u",
            id="/api/units/preview",
        ),
        pytest.param(
            main.units_export,
            main.UnitsRequest(
                text="ab", source_unit="codepoint", target_unit="utf16",
                annotations=[{"id": "u", "start": 0, "end": 1},
                             {"id": "u", "start": 0, "end": 1}]),
            "批注 id 重复：u",
            id="/api/units/export",
        ),
        pytest.param(
            main.review_preview,
            main.ReviewRequest(
                text="ab", max_chars=10, context_chars=2,
                annotations=[{"id": "r", "start": 0, "end": 9, "label": "L"}]),
            "批注 r 的区间 [0, 9) 非法或超出文本长度 2",
            id="/api/review/preview",
        ),
        pytest.param(
            main.review_export,
            main.ReviewRequest(
                text="ab", max_chars=10, context_chars=2,
                annotations=[{"id": "r", "start": 0, "end": 1, "label": ""}]),
            "批注 r 的 label 不能为空",
            id="/api/review/export",
        ),
        pytest.param(
            main.review_import,
            main.ReviewImportRequest(
                text="ab", max_chars=10, context_chars=2,
                annotations=[{"id": "r", "start": 0, "end": 1, "label": "L"},
                             {"id": "r", "start": 0, "end": 1, "label": "M"}]),
            "批注 id 重复：r",
            id="/api/review/import",
        ),
        pytest.param(
            main.relabel_preview,
            main.RelabelRequest(
                text="ab",
                annotations=[{"id": "x", "start": 0, "end": 9, "label": "旧"}]),
            "批注 x 的区间 [0, 9) 非法或超出文本长度 2",
            id="/api/relabel/preview",
        ),
        pytest.param(
            main.relabel_export,
            main.RelabelRequest(
                text="ab",
                annotations=[{"id": "x", "start": 0, "end": 1, "label": ""}]),
            "批注 x 的 label 不能为空",
            id="/api/relabel/export",
        ),
    ],
)
def test_endpoint_wiring(endpoint, req, detail):
    with pytest.raises(HTTPException) as exc_info:
        endpoint(req)
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == detail


# ---------- 接口接线：合法输入的输出字段与排序不变 ----------

def test_migrate_endpoint_output_unchanged():
    res = main.migrate(main.MigrateRequest(
        old_text="第一段：旧版说明。第二段：保持不变。",
        new_text="第一段：全新的新版说明。第二段：保持不变。",
        annotations=[{"id": "a1", "start": 13, "end": 17}],
    ))
    assert res["results"][0]["status"] == "shifted"
    assert (res["results"][0]["new_start"],
            res["results"][0]["new_end"]) == (16, 20)


def test_arbitrate_endpoint_allows_same_id_on_both_sides():
    res = main.arbitrate(main.ArbitrateRequest(
        text="目标是统一口径。",
        left=[{"id": "a", "start": 0, "end": 4, "label": "目标"}],
        right=[{"id": "a", "start": 0, "end": 4, "label": "目标"}],
    ))
    assert res["groups"][0]["status"] == "auto"


def test_units_export_preserves_extra_fields_and_input_order():
    res = main.units_export(main.UnitsRequest(
        text="汉字", source_unit="codepoint", target_unit="utf8_byte",
        annotations=[{"id": "u2", "start": 0, "end": 1, "label": "汉", "note": "x"},
                     {"id": "u1", "start": 1, "end": 2}],
    ))
    assert set(res) == {"annotations", "source_unit", "target_unit", "count"}
    assert res["annotations"] == [
        {"id": "u2", "start": 0, "end": 3, "label": "汉", "note": "x"},
        {"id": "u1", "start": 3, "end": 6},
    ]
    assert res["count"] == 2


def test_relabel_export_preserves_extra_fields_and_input_order():
    res = main.relabel_export(main.RelabelRequest(
        text="数据清洗",
        annotations=[{"id": "z", "start": 2, "end": 4, "label": "旧-对象", "note": "x"},
                     {"id": "a", "start": 0, "end": 2, "label": "旧-对象"}],
        rules={"target_labels": ["实体", "流程"],
               "rules": [{"from": "旧-对象", "to": "实体", "pattern": "数据"},
                         {"from": "旧-对象", "to": "流程"}]},
    ))
    assert set(res) == {"annotations", "count"}
    assert res["annotations"] == [
        {"id": "z", "start": 2, "end": 4, "label": "流程", "note": "x"},
        {"id": "a", "start": 0, "end": 2, "label": "实体"},
    ]
    assert res["count"] == 2


def test_relabel_export_conflict_on_item_errors():
    with pytest.raises(HTTPException) as exc_info:
        main.relabel_export(main.RelabelRequest(
            text="口径",
            annotations=[{"id": "a", "start": 0, "end": 2, "label": "坏"}],
            rules={"target_labels": ["实体"],
                   "rules": [{"from": "坏", "to": "坏"}]},
        ))
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["message"] == "存在标签迁移错误，禁止导出"
    assert exc_info.value.detail["errors"][0]["type"] == "cycle"


def test_relabel_preview_rejects_invalid_rules():
    with pytest.raises(HTTPException) as exc_info:
        main.relabel_preview(main.RelabelRequest(
            text="数据",
            annotations=[{"id": "a", "start": 0, "end": 2, "label": "旧"}],
            rules={"target_labels": ["实体", "实体"], "rules": []},
        ))
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "目标标签重复：实体"
