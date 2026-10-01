"""批注输入校验：策略驱动的入口验证器。

各接口对批注（id / start / end / label …）的入口校验规则不同，这里把差异
参数化为 Strategy，validate() 按策略逐条校验，并把通过的批注输出为普通
dict。任一校验失败立即抛出 422（短路）。校验顺序：按 Strategy.lists 中的
列表顺序、列表内按输入顺序；单条批注内依次为 id 类型、码点区间、label
非空、id 重复）。

策略维度：

- lists：请求中的批注列表（属性名 + 错误前缀），顺序即校验（短路）顺序；
- text_attr / length_label：码点区间校验的文本长度来源（迁移取旧版文本，
  其余取当前文本）与错误消息中的长度描述；
- check_interval：是否校验码点区间 [start, end)（制式转换不校验——边界与
  越界错误由 app.units 按源制式逐项列入响应，而非整体 422）；
- require_label：是否要求 label 非空；
- id_type：id 的合法类型（审阅只收字符串；pydantic 已在模型层拒绝非字符串
  id，此处为第二道防线）；
- id_scope：id 唯一性作用域——"request" 为整个请求内唯一，"list" 为每个
  列表各自唯一（仲裁左右侧分别判重，同一 id 可分别出现在两侧）；
- fields：输出字段（None 表示经 model_dump() 保留全部字段，含额外字段）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException


@dataclass(frozen=True)
class ListSpec:
    """请求中的一个批注列表：属性名与错误前缀。"""

    attr: str
    prefix: str = ""


@dataclass(frozen=True)
class Strategy:
    """一组批注列表的入口校验策略（各维度含义见模块 docstring）。"""

    lists: tuple[ListSpec, ...]
    text_attr: str = "text"
    length_label: str = "文本长度"
    check_interval: bool = True
    require_label: bool = True
    id_type: tuple[type, ...] = (str, int)
    id_scope: Literal["request", "list"] = "request"
    fields: tuple[str, ...] | None = ("id", "start", "end", "label")


# 批注迁移 / 迁移导出：区间落在旧版文本内、id 唯一，不要求 label
MIGRATE = Strategy(
    lists=(ListSpec("annotations"),),
    text_attr="old_text",
    length_label="旧版文本长度",
    require_label=False,
    fields=("id", "start", "end"),
)

# 序列标注 / 嵌套审计：区间合法、label 非空、id 唯一
LABELED = Strategy(lists=(ListSpec("annotations"),))

# 批注集仲裁：左右两侧依次校验、各自判重，错误消息带侧别前缀
ARBITRATE = Strategy(
    lists=(ListSpec("left", "左侧"), ListSpec("right", "右侧")),
    id_scope="list",
)

# 偏移制式转换：只在入口判重，额外字段原样保留，边界错误留给 app.units
UNITS = Strategy(
    lists=(ListSpec("annotations"),),
    check_interval=False,
    require_label=False,
    fields=None,
)

# 审阅切片：区间合法、label 非空、id 唯一且只收字符串
REVIEW = Strategy(lists=(ListSpec("annotations"),), id_type=(str,))

# 可领取审阅工作副本：创建时同样要求区间合法、label 非空、字符串 id 唯一
COPY = Strategy(lists=(ListSpec("annotations"),), id_type=(str,))

# 标签体系迁移：区间合法、label 非空、id 唯一，额外字段原样保留（导出只替换 label）
RELABEL = Strategy(lists=(ListSpec("annotations"),), fields=None)


def validate(req, strategy: Strategy) -> list[list[dict]]:
    """按策略校验 req 中的批注列表，返回与各列表同序的普通 dict 列表。

    任一校验失败立即抛出 422（短路顺序见模块 docstring）。
    """
    n = len(getattr(req, strategy.text_attr)) if strategy.check_interval else 0
    seen: set = set()
    out: list[list[dict]] = []
    for spec in strategy.lists:
        if strategy.id_scope == "list":
            seen = set()  # 每个列表各自判重
        checked = []
        for a in getattr(req, spec.attr):
            _check_one(a, spec, strategy, n, seen)
            checked.append(_project(a, strategy))
        out.append(checked)
    return out


def _check_one(a, spec: ListSpec, strategy: Strategy, n: int, seen: set) -> None:
    """校验单条批注：id 类型 → 码点区间 → label 非空 → id 重复。"""
    if not isinstance(a.id, strategy.id_type):
        names = " / ".join(t.__name__ for t in strategy.id_type)
        raise HTTPException(
            status_code=422,
            detail=f"{spec.prefix}批注 id 类型非法：{a.id!r}（须为 {names}）",
        )
    if strategy.check_interval and not 0 <= a.start < a.end <= n:
        raise HTTPException(
            status_code=422,
            detail=f"{spec.prefix}批注 {a.id} 的区间 [{a.start}, {a.end}) "
                   f"非法或超出{strategy.length_label} {n}",
        )
    if strategy.require_label and not a.label:
        raise HTTPException(
            status_code=422,
            detail=f"{spec.prefix}批注 {a.id} 的 label 不能为空",
        )
    if a.id in seen:
        raise HTTPException(
            status_code=422,
            detail=f"{spec.prefix}批注 id 重复：{a.id}",
        )
    seen.add(a.id)


def _project(a, strategy: Strategy) -> dict:
    """把通过的批注输出为普通 dict；fields 为 None 时保留全部字段（含额外字段）。"""
    if strategy.fields is None:
        return a.model_dump()
    return {k: getattr(a, k) for k in strategy.fields}
