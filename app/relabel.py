"""批注标签体系迁移核心逻辑。

把批注的 label 从旧体系迁移到目标体系。偏移约定：start/end 为 Unicode
码点偏移，区间左闭右开 [start, end)；规则的 pattern 用 ``re.fullmatch``
匹配批注原文（文本的 [start, end) 切片）。

规则 JSON::

    {
      "target_labels": ["实体", "流程"],
      "rules": [
        {"from": "旧-对象", "to": "实体", "pattern": "数据"},  # 条件规则
        {"from": "旧-对象", "to": "流程"}                       # 回退规则（无 pattern）
      ]
    }

求值语义（每条批注独立求值）：

- 当前 label 在 ``target_labels`` 中 → 迁移结束；
- 否则在当前 label 的规则组（``from`` == 当前 label）内按**声明顺序**取首个
  pattern 命中批注原文的规则（无 pattern 的回退规则必中），把 label 改写为
  ``to`` 后继续求值；
- 无 pattern 的回退规则每个 ``from`` 至多一条，且必须排在该 ``from``
  规则组的最后；
- 迁移链**重复经过同一 label** 视为循环（``cycle``）；当前 label 不在
  ``target_labels`` 且没有可用规则记 ``no_rule`` 错误。

规则集不合法（空 / 重复目标标签、非法正则、回退规则冲突等）抛出
``RelabelError``（接口层转为 422）；逐条批注的求值错误（``no_rule`` /
``cycle``）逐项列入结果，存在错误时禁止导出。导出保持输入顺序与 id、
范围和其余字段不变，只替换 label。
"""

from __future__ import annotations

import re

# 逐项错误类型
NO_RULE = "no_rule"  # 当前 label 不在目标体系且无可用规则
CYCLE = "cycle"      # 迁移链重复经过同一 label


class RelabelError(ValueError):
    """规则集不合法（空 / 重复目标标签、非法正则、回退规则冲突等）。"""


def validate_relabel_rules(rules: dict) -> dict:
    """校验并规范化规则 JSON，返回可直接求值的结构。

    校验顺序：目标标签（空 → 重复）→ 逐条规则（from / to 非空、pattern
    可编译）→ 各 from 规则组的回退规则约束（至多一条、排在最后）。
    任一失败抛出 RelabelError。返回::

        {
          "target_labels": [...],                 # 声明顺序
          "rules": [{"from", "to", "pattern", "regex", "index"}, ...],
          "by_from": {from_label: [规则, …]},      # 组内保持声明顺序
        }

    ``index`` 为规则在声明中的序号（从 1 计），供页面与错误消息引用。
    """
    targets: list[str] = []
    seen: set[str] = set()
    for t in rules.get("target_labels", []):
        if not isinstance(t, str) or not t:
            raise RelabelError(f"目标标签不能为空：{t!r}")
        if t in seen:
            raise RelabelError(f"目标标签重复：{t}")
        seen.add(t)
        targets.append(t)

    norm: list[dict] = []
    by_from: dict[str, list[dict]] = {}
    for i, r in enumerate(rules.get("rules", []), 1):
        src, dst, pattern = r.get("from"), r.get("to"), r.get("pattern")
        if not isinstance(src, str) or not src:
            raise RelabelError(f"第 {i} 条规则的 from 不能为空：{src!r}")
        if not isinstance(dst, str) or not dst:
            raise RelabelError(f"第 {i} 条规则的 to 不能为空：{dst!r}")
        regex = None
        if pattern is not None:
            if not isinstance(pattern, str):
                raise RelabelError(f"第 {i} 条规则的 pattern 必须是字符串：{pattern!r}")
            try:
                regex = re.compile(pattern)
            except re.error as exc:
                raise RelabelError(f"第 {i} 条规则的 pattern 是非法正则：{pattern!r}（{exc}）")
        rule = {"from": src, "to": dst, "pattern": pattern, "regex": regex, "index": i}
        norm.append(rule)
        by_from.setdefault(src, []).append(rule)

    for src, group in by_from.items():
        fallbacks = [r for r in group if r["pattern"] is None]
        if len(fallbacks) > 1:
            raise RelabelError(
                f"label「{src}」下有 {len(fallbacks)} 条无 pattern 的回退规则，至多一条")
        if fallbacks and group[-1] is not fallbacks[0]:
            raise RelabelError(
                f"label「{src}」的无 pattern 回退规则必须排在该 from 规则组的最后")

    return {"target_labels": targets, "rules": norm, "by_from": by_from}


def _evaluate(excerpt: str, source_label: str, rules: dict) -> dict:
    """求值单条批注的迁移链。

    返回 ``{"chain": [label, …], "steps": [...], "error": {...} 或 None}``：
    ``chain`` 含起始 label 与依次改写到的 label（循环时含重复出现的那个）；
    ``steps`` 记录每步命中的规则（from / to / pattern / index）。
    """
    targets = set(rules["target_labels"])
    by_from = rules["by_from"]
    chain = [source_label]
    steps: list[dict] = []
    seen = {source_label}
    while chain[-1] not in targets:
        current = chain[-1]
        rule = next((r for r in by_from.get(current, [])
                     if r["regex"] is None or r["regex"].fullmatch(excerpt)), None)
        if rule is None:
            return {"chain": chain, "steps": steps,
                    "error": {"type": NO_RULE, "label": current}}
        nxt = rule["to"]
        steps.append({"from": current, "to": nxt,
                      "pattern": rule["pattern"], "index": rule["index"]})
        chain.append(nxt)
        if nxt in seen:
            return {"chain": chain, "steps": steps,
                    "error": {"type": CYCLE, "label": nxt}}
        seen.add(nxt)
    return {"chain": chain, "steps": steps, "error": None}


def _error_message(ann_id, err: dict) -> str:
    if err["type"] == NO_RULE:
        return (f"批注 {ann_id} 的 label「{err['label']}」不在目标标签体系中，"
                "且没有可用的迁移规则")
    return f"批注 {ann_id} 的迁移链重复经过 label「{err['label']}」（循环）"


def migrate_labels(text: str, annotations: list[dict], rules: dict) -> dict:
    """逐条求值迁移链，返回 {"items", "errors", "target_labels"}。

    ``items`` 与输入同序：每条含原文片段、原 label、迁移链、命中规则、
    最终 label（出错时为 None）与该项错误；``errors`` 为全部逐项错误
    （no_rule / cycle）的汇总，存在错误时调用方应禁止导出。
    """
    items = []
    errors = []
    for a in annotations:
        excerpt = text[a["start"]:a["end"]]
        res = _evaluate(excerpt, a["label"], rules)
        item_errors = []
        if res["error"] is not None:
            err = {**res["error"], "id": a["id"],
                   "message": _error_message(a["id"], res["error"])}
            item_errors.append(err)
            errors.append(err)
        items.append({
            "id": a["id"], "start": a["start"], "end": a["end"],
            "text": excerpt,
            "source_label": a["label"],
            "final_label": res["chain"][-1] if res["error"] is None else None,
            "chain": res["chain"],
            "steps": res["steps"],
            "status": "error" if res["error"] is not None else "done",
            "errors": item_errors,
        })
    return {"items": items, "errors": errors,
            "target_labels": rules["target_labels"]}


def export_labels(text: str, annotations: list[dict], rules: dict) -> dict:
    """重算迁移链并导出：{"errors": [...], "annotations": [...]}。

    存在任何逐项错误时 ``annotations`` 为空列表；否则按**输入顺序**返回
    批注，保持 id、范围与其余字段不变，只把 label 替换为迁移结果。
    """
    result = migrate_labels(text, annotations, rules)
    if result["errors"]:
        return {"errors": result["errors"], "annotations": []}
    out = []
    for a, item in zip(annotations, result["items"]):
        new = dict(a)
        new["label"] = item["final_label"]
        out.append(new)
    return {"errors": [], "annotations": out}
