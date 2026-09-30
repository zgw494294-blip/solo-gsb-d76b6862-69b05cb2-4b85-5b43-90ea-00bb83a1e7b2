"""批注集仲裁核心逻辑。

输入同一份文本与左右两组批注（id / start / end / label），偏移按 Unicode 码点，
区间为左闭右开 [start, end)。左右批注区间交集长度大于 0 即在二者间连边，
构成二分图；每个连通分量即一个仲裁组：

- 组内恰为一左一右，且范围、label 完全相同 → 自动通过（auto）；
- 其余分量（一对多、多对多、范围/标签不一致的一对一、单侧分量）全部进入
  仲裁（pending），只能整组采用左侧、整组采用右侧或全部舍弃，
  不做逐条贪心拆分。
"""

from __future__ import annotations

# 组状态
AUTO = "auto"        # 一左一右且范围、label 完全相同，自动通过
PENDING = "pending"  # 待仲裁

# 仲裁决策取值
DECISION_LEFT = "left"        # 整组采用左侧
DECISION_RIGHT = "right"      # 整组采用右侧
DECISION_DISCARD = "discard"  # 全部舍弃
DECISIONS = {DECISION_LEFT, DECISION_RIGHT, DECISION_DISCARD}


class ArbitrationError(ValueError):
    """仲裁决策不合法（如决策值未知、组号不存在、对已自动通过的组做决策）。"""


def _overlaps(a: dict, b: dict) -> bool:
    """区间 [start, end) 的交集长度是否大于 0（端点相接不算重叠）。"""
    return max(a["start"], b["start"]) < min(a["end"], b["end"])


def _ann_key(a: dict) -> tuple:
    """组内批注的展示排序键：start、end、label、来源 id。"""
    return (a["start"], a["end"], a["label"], str(a["id"]))


def _view(text: str, a: dict) -> dict:
    """附带原文片段的批注视图。"""
    return {"id": a["id"], "start": a["start"], "end": a["end"],
            "label": a["label"], "text": text[a["start"]:a["end"]]}


def build_groups(text: str, left: list[dict], right: list[dict]) -> list[dict]:
    """按重叠关系把左右批注划分为二分图连通分量（仲裁组）。

    返回按组内最小 (start, end) 稳定排序的组列表，组号依次为 g1、g2……
    """
    # 并查集：0..len(left)-1 为左节点，len(left).. 为右节点
    parent = list(range(len(left) + len(right)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i, l in enumerate(left):
        for j, r in enumerate(right):
            if _overlaps(l, r):
                union(i, len(left) + j)

    buckets: dict[int, dict] = {}
    for i, l in enumerate(left):
        buckets.setdefault(find(i), {"left": [], "right": []})["left"].append(l)
    for j, r in enumerate(right):
        buckets.setdefault(find(len(left) + j), {"left": [], "right": []})["right"].append(r)

    def group_key(g: dict) -> tuple:
        members = g["left"] + g["right"]
        return (min(a["start"] for a in members),
                min(a["end"] for a in members),
                tuple(sorted(str(a["id"]) for a in members)))

    ordered = sorted(buckets.values(), key=group_key)

    groups = []
    for idx, g in enumerate(ordered, 1):
        ls = sorted(g["left"], key=_ann_key)
        rs = sorted(g["right"], key=_ann_key)
        auto = (len(ls) == 1 and len(rs) == 1
                and ls[0]["start"] == rs[0]["start"]
                and ls[0]["end"] == rs[0]["end"]
                and ls[0]["label"] == rs[0]["label"])
        groups.append({
            "group_id": f"g{idx}",
            "status": AUTO if auto else PENDING,
            "left": [_view(text, a) for a in ls],
            "right": [_view(text, a) for a in rs],
        })
    return groups


def apply_arbitration(groups: list[dict], decisions: dict[str, str]) -> tuple[list[dict], list[dict], list]:
    """校验逐组决策并生成导出结果。

    decisions: {group_id: "left" | "right" | "discard"}。
    返回 (annotations, decision_records, unresolved)：
    - annotations：按 start、end、label、来源 id 稳定排序并重新编号的导出批注；
    - decision_records：逐组保留 left_ids、right_ids、decision；
    - unresolved：未给出决策的待仲裁组号（非空时调用方应拒绝导出）。
    """
    by_id = {g["group_id"]: g for g in groups}
    for gid, d in decisions.items():
        if gid not in by_id:
            raise ArbitrationError(f"未知的仲裁组：{gid}")
        if d not in DECISIONS:
            raise ArbitrationError(
                f"仲裁组 {gid} 的决策非法：{d}（应为 left / right / discard）")
        if by_id[gid]["status"] == AUTO:
            raise ArbitrationError(f"仲裁组 {gid} 已自动通过，无需决策")

    unresolved = [g["group_id"] for g in groups
                  if g["status"] == PENDING and g["group_id"] not in decisions]

    records = []
    for g in groups:
        records.append({
            "group_id": g["group_id"],
            "left_ids": [a["id"] for a in g["left"]],
            "right_ids": [a["id"] for a in g["right"]],
            "decision": AUTO if g["status"] == AUTO else decisions.get(g["group_id"]),
        })

    picked = []  # (批注, 来源侧)
    for g in groups:
        if g["status"] == AUTO:
            picked.append((g["left"][0], "both"))  # 两侧完全一致，取一条即可
            continue
        d = decisions.get(g["group_id"])
        if d == DECISION_LEFT:
            picked.extend((a, "left") for a in g["left"])
        elif d == DECISION_RIGHT:
            picked.extend((a, "right") for a in g["right"])
        # discard 或未决：不导出

    picked.sort(key=lambda p: _ann_key(p[0]))
    annotations = [
        {"id": f"ann-{i}", "start": a["start"], "end": a["end"], "label": a["label"],
         "source": src, "source_id": a["id"]}
        for i, (a, src) in enumerate(picked, 1)
    ]
    return annotations, records, unresolved
