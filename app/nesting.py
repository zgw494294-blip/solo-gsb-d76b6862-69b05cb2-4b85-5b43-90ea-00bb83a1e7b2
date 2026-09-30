"""嵌套批注审计核心逻辑。

偏移约定：所有 start/end 均为 Unicode 码点偏移，区间为左闭右开 [start, end)。

区间关系（相接不算重叠，只有严格包含才形成父子）：
- 同范围 → duplicate_span 错误；
- 部分交叉 → crossing 错误；
- 严格包含 → 父子嵌套；端点相接（end == start）不算重叠。

区间关系全部合法时，按 (start, -end, label, str(id)) 排序，用栈确定最窄直接
父级；根 label 须在 root_labels 中，非根 label 须在直接父级的 children 中。
存在任何错误时禁止导出；导出为保留原 id、label、范围的稳定嵌套 JSON。
"""

from __future__ import annotations

# 错误类型
DUPLICATE_SPAN = "duplicate_span"  # 同范围
CROSSING = "crossing"              # 部分交叉
ROOT_LABEL = "root_label"          # 根 label 未列入 root_labels
CHILD_LABEL = "child_label"        # 子 label 不在直接父级的 children 中


class NestingError(ValueError):
    """结构规则不合法（如规则列表含重复 label）。"""


def validate_rules(rules: dict) -> dict:
    """校验结构规则：root_labels 与每个 children 列表均不得含重复 label。"""
    root_labels = list(rules.get("root_labels", []))
    children = {k: list(v) for k, v in rules.get("children", {}).items()}
    _reject_dup(root_labels, "root_labels")
    for parent, labels in children.items():
        _reject_dup(labels, f"children[{parent}]")
    return {"root_labels": root_labels, "children": children}


def _reject_dup(labels: list, where: str) -> None:
    seen = set()
    for lb in labels:
        if lb in seen:
            raise NestingError(f"结构规则的 {where} 含重复 label：{lb}")
        seen.add(lb)


def _sort_key(a: dict) -> tuple:
    """批注排序键：start 升序、end 降序、label、id 字符串。"""
    return (a["start"], -a["end"], a["label"], str(a["id"]))


def _relation(a: dict, b: dict) -> str | None:
    """两条批注的区间关系：duplicate_span / crossing / contains / None（不相交）。"""
    if max(a["start"], b["start"]) >= min(a["end"], b["end"]):
        return None  # 相接或相离，不算重叠
    if a["start"] == b["start"] and a["end"] == b["end"]:
        return DUPLICATE_SPAN
    if (a["start"] <= b["start"] and b["end"] <= a["end"]) or (
            b["start"] <= a["start"] and a["end"] <= b["end"]):
        return "contains"
    return CROSSING


def find_span_errors(annotations: list[dict]) -> list[dict]:
    """检查所有批注对，返回 duplicate_span / crossing 错误（确定性顺序）。

    按 (start, -end, label, str(id)) 排序后逐对检查；排序后 b 的 start 单调
    不减，b.start ≥ a.end 时二者必不相交，且之后的批注也不可能再与 a 相交。
    """
    ordered = sorted(annotations, key=_sort_key)
    errors = []
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            if b["start"] >= a["end"]:
                break
            rel = _relation(a, b)
            if rel == DUPLICATE_SPAN:
                errors.append({
                    "type": DUPLICATE_SPAN,
                    "ids": [a["id"], b["id"]],
                    "message": f"批注 {a['id']} 与 {b['id']} 范围相同 "
                               f"[{a['start']}, {a['end']})（duplicate_span）",
                })
            elif rel == CROSSING:
                errors.append({
                    "type": CROSSING,
                    "ids": [a["id"], b["id"]],
                    "message": f"批注 {a['id']} [{a['start']}, {a['end']}) 与 "
                               f"{b['id']} [{b['start']}, {b['end']}) "
                               "部分交叉（crossing）",
                })
    return errors


def build_tree(annotations: list[dict]) -> list[dict]:
    """用栈构建嵌套树（假定无 duplicate_span / crossing）。

    按 (start, -end, label, str(id)) 排序后依次入栈：先弹出 end ≤ 当前 start
    的节点，此后栈顶即最窄直接父级（栈自底向顶为一条严格嵌套链）。
    返回根节点列表；节点为 {"annotation": 批注, "children": [...]}，
    各层 children 均按同一排序键稳定有序。
    """
    roots = []
    stack = []
    for a in sorted(annotations, key=_sort_key):
        node = {"annotation": a, "children": []}
        while stack and stack[-1]["annotation"]["end"] <= a["start"]:
            stack.pop()
        if stack:
            stack[-1]["children"].append(node)
        else:
            roots.append(node)
        stack.append(node)
    return roots


def check_labels(roots: list[dict], rules: dict) -> list[dict]:
    """检查 label 规则：根 label 须在 root_labels，子 label 须在直接父级的 children 中。"""
    errors = []
    root_labels = set(rules["root_labels"])
    children = rules["children"]

    def visit(node: dict, parent: dict | None) -> None:
        a = node["annotation"]
        if parent is None:
            if a["label"] not in root_labels:
                errors.append({
                    "type": ROOT_LABEL,
                    "ids": [a["id"]],
                    "message": f"根批注 {a['id']} 的 label「{a['label']}」"
                               "不在 root_labels 中",
                })
        elif a["label"] not in children.get(parent["label"], []):
            errors.append({
                "type": CHILD_LABEL,
                "ids": [parent["id"], a["id"]],
                "message": f"批注 {a['id']} 的 label「{a['label']}」不在直接父级 "
                           f"{parent['id']}（label「{parent['label']}」）的 children 中",
            })
        for c in node["children"]:
            visit(c, a)

    for r in roots:
        visit(r, None)
    return errors


def _view(text: str, node: dict) -> dict:
    """附带原文片段的节点视图（供页面展示）。"""
    a = node["annotation"]
    return {"id": a["id"], "label": a["label"],
            "start": a["start"], "end": a["end"],
            "text": text[a["start"]:a["end"]],
            "children": [_view(text, c) for c in node["children"]]}


def _export(node: dict) -> dict:
    """导出节点：保留原 id、label、范围。"""
    a = node["annotation"]
    return {"id": a["id"], "label": a["label"],
            "start": a["start"], "end": a["end"],
            "children": [_export(c) for c in node["children"]]}


def audit_nesting(text: str, annotations: list[dict], rules: dict) -> dict:
    """审计嵌套批注：返回 {"errors": [...], "tree": [...] 或 None}。

    存在 duplicate_span / crossing 时无法建树，tree 为 None；
    否则返回嵌套树（label 规则错误不影响建树，仅记入 errors）。
    """
    span_errors = find_span_errors(annotations)
    if span_errors:
        return {"errors": span_errors, "tree": None}
    roots = build_tree(annotations)
    return {"errors": check_labels(roots, rules),
            "tree": [_view(text, r) for r in roots]}


def export_nesting(annotations: list[dict]) -> list[dict]:
    """导出稳定嵌套 JSON（假定审计无错误）：保留原 id、label、范围。"""
    return [_export(n) for n in build_tree(annotations)]
