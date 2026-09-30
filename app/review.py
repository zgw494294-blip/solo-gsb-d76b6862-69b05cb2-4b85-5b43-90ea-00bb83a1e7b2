"""批注审阅切片核心逻辑。

偏移约定：所有 start/end 均为 Unicode 码点偏移，区间为左闭右开 [start, end)。

把文本上的批注按 (start, end, label, id) 排序后**顺序装箱**：依次考察每条
批注，加入当前组后的跨度（组内最左 start 到最右 end）不超过 max_chars 就
并入当前组，否则当前组封口、以该批注另起新组；单条批注长度超过 max_chars
时报错。

每个组导出一个**审阅切片**：窗口先包住组内全部批注 [s, e)，再把 max_chars
剩余的容量均分到两侧——每侧不超过 context_chars，奇数余量多给右侧；一侧
触及文本边界用不完的余量转给另一侧（每侧仍不超过 context_chars）。切片行
含稳定 fragment_id、片段文本、全局范围、片段的 SHA-256 与局部坐标（窗口
相对）批注，逐行写入 JSONL。

导入审阅结果时后端用同一输入**重新切片**，校验每个切片的 fragment_id 与
摘要、每条批注的归属（id 必须属于该切片），并要求每个 id 恰好提交一次；
批注可保留（keep）、舍弃（drop）或提交窗口内的修正范围（correct，局部
坐标），校验全部通过后换回全局坐标，任一错误均拒绝整批。
"""

from __future__ import annotations

import hashlib
import json

# 审阅决策取值
KEEP = "keep"        # 保留原范围
DROP = "drop"        # 舍弃该批注
CORRECT = "correct"  # 提交窗口内的修正范围（局部坐标）
ACTIONS = (KEEP, DROP, CORRECT)

# 导入校验错误类型
UNKNOWN_FRAGMENT = "unknown_fragment"      # fragment_id 不存在
DUPLICATE_FRAGMENT = "duplicate_fragment"  # 同一切片被重复提交
DIGEST_MISMATCH = "digest_mismatch"        # 切片摘要不符（片段内容已被改动）
FOREIGN_ID = "foreign_id"                  # 批注 id 不属于该切片
DUPLICATE_ID = "duplicate_id"              # 同一 id 被重复提交
MISSING_ID = "missing_id"                  # 存在未提交的批注 id
UNKNOWN_ACTION = "unknown_action"          # 未知决策取值
BAD_RANGE = "bad_range"                    # 修正范围非法或超出窗口


class ReviewError(ValueError):
    """切片参数或批注不合法（如单条批注超过 max_chars）。"""


def _check_params(max_chars: int, context_chars: int) -> None:
    if max_chars < 1:
        raise ReviewError(f"max_chars 必须为正整数，收到 {max_chars}")
    if context_chars < 0:
        raise ReviewError(f"context_chars 必须为非负整数，收到 {context_chars}")


def _sort_key(a: dict) -> tuple:
    """装箱排序键：start、end、label、id。"""
    return (a["start"], a["end"], a["label"], str(a["id"]))


def _pack(ordered: list[dict], max_chars: int) -> list[tuple]:
    """顺序装箱：返回 [(组起点, 组终点, [批注…]), …]。

    批注已按 (start, end, label, id) 排序，故组内最左 start 即首条批注的
    start；加入下一条后的跨度为 max(当前组最右 end, 新批注 end) - 组起点，
    不超过 max_chars 就并入，否则当前组封口、另起新组。
    """
    groups = []
    cur: list[dict] = []
    cur_start = cur_end = 0
    for a in ordered:
        if not cur:
            cur, cur_start, cur_end = [a], a["start"], a["end"]
            continue
        new_end = max(cur_end, a["end"])
        if new_end - cur_start <= max_chars:
            cur.append(a)
            cur_end = new_end
        else:
            groups.append((cur_start, cur_end, cur))
            cur, cur_start, cur_end = [a], a["start"], a["end"]
    if cur:
        groups.append((cur_start, cur_end, cur))
    return groups


def _window(start: int, end: int, text_len: int,
            max_chars: int, context_chars: int) -> tuple[int, int]:
    """计算切片窗口 [win_start, win_end)。

    窗口先包住 [start, end)，再把 max_chars 剩余的容量均分到两侧：每侧
    不超过 context_chars，奇数余量多给右侧；一侧触及文本边界用不完的
    余量转给另一侧（每侧仍不超过 context_chars）。
    """
    remaining = max_chars - (end - start)
    left = min(remaining // 2, context_chars)
    right = min(remaining - remaining // 2, context_chars)  # 奇数多给右侧
    # 左侧触及文本起点的余量转给右侧
    left_ext = min(left, start)
    right = min(right + (left - left_ext), context_chars)
    # 右侧触及文本终点的余量转回左侧
    right_ext = min(right, text_len - end)
    left = min(left_ext + (right - right_ext), context_chars)
    left_ext = min(left, start)
    return start - left_ext, end + right_ext


def build_fragments(text: str, annotations: list[dict],
                    max_chars: int, context_chars: int) -> list[dict]:
    """装箱并生成审阅切片。

    返回 [{"fragment_id", "text", "start", "end", "sha256",
           "annotations": [{"id", "start", "end", "label"}（局部坐标）]}, …]；
    切片按组顺序编号 frag-1、frag-2……，sha256 为片段 UTF-8 编码的摘要。
    单条批注长度超过 max_chars 时抛出 ReviewError。
    """
    _check_params(max_chars, context_chars)
    ordered = sorted(annotations, key=_sort_key)
    too_long = [a["id"] for a in ordered if a["end"] - a["start"] > max_chars]
    if too_long:
        raise ReviewError(
            f"单条批注长度超过 max_chars={max_chars}："
            + "、".join(str(i) for i in too_long))
    fragments = []
    for idx, (gs, ge, members) in enumerate(_pack(ordered, max_chars), 1):
        win_s, win_e = _window(gs, ge, len(text), max_chars, context_chars)
        frag_text = text[win_s:win_e]
        fragments.append({
            "fragment_id": f"frag-{idx}",
            "text": frag_text,
            "start": win_s,
            "end": win_e,
            "sha256": hashlib.sha256(frag_text.encode("utf-8")).hexdigest(),
            "annotations": [
                {"id": a["id"], "start": a["start"] - win_s,
                 "end": a["end"] - win_s, "label": a["label"]}
                for a in members
            ],
        })
    return fragments


def to_jsonl(fragments: list[dict]) -> str:
    """把切片序列化为 JSONL（UTF-8，每行一个切片对象）。"""
    lines = [json.dumps(f, ensure_ascii=False) for f in fragments]
    return "\n".join(lines) + ("\n" if lines else "")


def apply_reviews(text: str, annotations: list[dict],
                  max_chars: int, context_chars: int,
                  reviews: list[dict]) -> dict:
    """校验并应用审阅结果，换回全局坐标。

    后端用同一输入重新切片，逐条校验：fragment_id 存在且只提交一次、切片
    摘要一致、批注 id 属于该切片（归属）、每个 id 恰好提交一次、修正范围
    落在窗口内（局部坐标 0 ≤ start < end ≤ 窗口长度）。返回
    {"annotations", "dropped", "errors"}：存在任何错误时 annotations 为
    空列表（整批拒绝）；否则 annotations 为全局坐标结果（含 action 与所属
    fragment_id，按 start、end、label、id 稳定排序），dropped 为被舍弃的
    批注 id。
    """
    fragments = build_fragments(text, annotations, max_chars, context_chars)
    by_id = {f["fragment_id"]: f for f in fragments}
    ann_by_id = {a["id"]: a for a in annotations}
    owner = {}  # 批注 id -> 所属 fragment_id（装箱是划分，每个 id 恰属一个切片）
    for f in fragments:
        for a in f["annotations"]:
            owner[a["id"]] = f["fragment_id"]

    errors: list[dict] = []
    results = []  # (action, 批注 id, 全局 start, 全局 end, fragment_id)
    seen_fragments: set = set()
    seen_ids: set = set()
    for rev in reviews:
        fid = rev.get("fragment_id")
        frag = by_id.get(fid)
        if frag is None:
            errors.append({"type": UNKNOWN_FRAGMENT, "fragment_id": fid,
                           "message": f"未知的切片：{fid}"})
            continue
        if fid in seen_fragments:
            errors.append({"type": DUPLICATE_FRAGMENT, "fragment_id": fid,
                           "message": f"切片 {fid} 被重复提交"})
            continue
        seen_fragments.add(fid)
        if rev.get("sha256") != frag["sha256"]:
            errors.append({"type": DIGEST_MISMATCH, "fragment_id": fid,
                           "message": f"切片 {fid} 的摘要不符（片段内容已被改动）"})
            continue
        win_len = frag["end"] - frag["start"]
        for d in rev.get("decisions", []):
            aid = d.get("id")
            action = d.get("action")
            if aid in seen_ids:
                errors.append({"type": DUPLICATE_ID, "fragment_id": fid, "id": aid,
                               "message": f"批注 {aid} 被重复提交"})
                continue
            seen_ids.add(aid)
            if owner.get(aid) != fid:
                errors.append({"type": FOREIGN_ID, "fragment_id": fid, "id": aid,
                               "message": f"批注 {aid} 不属于切片 {fid}"})
                continue
            if action == KEEP:
                a = ann_by_id[aid]
                results.append((KEEP, aid, a["start"], a["end"], fid))
            elif action == DROP:
                results.append((DROP, aid, None, None, fid))
            elif action == CORRECT:
                s, e = d.get("start"), d.get("end")
                if s is None or e is None or not 0 <= s < e <= win_len:
                    errors.append({
                        "type": BAD_RANGE, "fragment_id": fid, "id": aid,
                        "message": f"批注 {aid} 的修正范围 [{s}, {e}) 非法"
                                   f"（须为窗口内局部坐标：0 ≤ start < end ≤ {win_len}）",
                    })
                    continue
                results.append((CORRECT, aid, frag["start"] + s,
                                frag["start"] + e, fid))
            else:
                errors.append({
                    "type": UNKNOWN_ACTION, "fragment_id": fid, "id": aid,
                    "message": f"批注 {aid} 的决策非法：{action}"
                               "（应为 keep / drop / correct）",
                })
    missing = [aid for aid in owner if aid not in seen_ids]
    if missing:
        errors.append({
            "type": MISSING_ID, "ids": missing,
            "message": "存在未提交的批注 id（每个 id 必须恰好出现一次）："
                       + "、".join(str(i) for i in missing),
        })
    if errors:
        return {"annotations": [], "dropped": [], "errors": errors}

    out = []
    dropped = []
    for action, aid, gs, ge, fid in results:
        if action == DROP:
            dropped.append(aid)
            continue
        out.append({"id": aid, "start": gs, "end": ge,
                    "label": ann_by_id[aid]["label"],
                    "action": action, "fragment_id": fid})
    out.sort(key=lambda r: (r["start"], r["end"], r["label"], str(r["id"])))
    dropped.sort(key=str)
    return {"annotations": out, "dropped": dropped, "errors": []}
