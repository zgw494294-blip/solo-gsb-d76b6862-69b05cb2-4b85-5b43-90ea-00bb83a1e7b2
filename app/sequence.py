"""序列标注语料导出核心逻辑。

偏移约定：所有 start/end 均为 Unicode 码点偏移，区间为左闭右开 [start, end)。

Token 切分：
- 连续汉字（CJK 表意文字）为一个 token；
- 连续 ASCII 字母数字为一个 token；
- 其余单个非空白字符自成一个 token；
- 空白字符不产出 token。

批注边界会切开其落在的 token，使每条批注恰好覆盖若干完整 token；
只覆盖空白（不含任何 token）的批注报错。

重叠批注不能同层：按 (start, end, label, id) 稳定排序后贪心分层——
在末条批注 end ≤ 当前 start 的层中按（结束位置、层号）选最小者，
无可用层才新建；层数即区间最大深度，必为最少。

每层独立生成 BILOU：未覆盖 token 为 O，单 token 批注为 U-label，
多 token 批注首 / 中 / 末分别为 B- / I- / L-label。
"""

from __future__ import annotations


class SequenceError(ValueError):
    """批注无法落到任何 token 上（如区间只覆盖空白）。"""


def _is_han(ch: str) -> bool:
    """是否为 CJK 表意文字（含扩展区与兼容区）。"""
    cp = ord(ch)
    return (
        0x3400 <= cp <= 0x4DBF       # 扩展 A
        or 0x4E00 <= cp <= 0x9FFF    # 基本区
        or 0xF900 <= cp <= 0xFAFF    # 兼容表意文字
        or 0x20000 <= cp <= 0x2A6DF  # 扩展 B
        or 0x2A700 <= cp <= 0x2EBEF  # 扩展 C–F
        or 0x30000 <= cp <= 0x323AF  # 扩展 G、H
    )


def _is_ascii_alnum(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()


def tokenize(text: str) -> list[dict]:
    """基础切分：汉字串 / ASCII 字母数字串 / 单个其他非空字符；空白不产出 token。"""
    tokens = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if _is_han(ch) or _is_ascii_alnum(ch):
            pred = _is_han if _is_han(ch) else _is_ascii_alnum
            j = i + 1
            while j < n and pred(text[j]):
                j += 1
            tokens.append({"text": text[i:j], "start": i, "end": j})
            i = j
        else:
            tokens.append({"text": ch, "start": i, "end": i + 1})
            i += 1
    return tokens


def _split_at_boundaries(text: str, tokens: list[dict], boundaries: set[int]) -> list[dict]:
    """在批注边界处切开 token，使每个批注边界都成为 token 边缘。"""
    out = []
    for tok in tokens:
        cuts = sorted(b for b in boundaries if tok["start"] < b < tok["end"])
        if not cuts:
            out.append(tok)
            continue
        edges = [tok["start"], *cuts, tok["end"]]
        out.extend({"text": text[s:e], "start": s, "end": e}
                   for s, e in zip(edges, edges[1:]))
    return out


def assign_layers(annotations: list[dict]) -> list[list[int]]:
    """贪心分层：返回层列表，每层为批注下标（按输入顺序计）列表。

    按 (start, end, label, id) 稳定排序后依次分配：在末条批注 end ≤ 当前
    start 的层中按（结束位置、层号）选最小者，无可用层才新建。
    新建层仅当当前批注与所有已有层的末条批注重叠，这些批注在排序后
    start 均不超过当前 start，故它们两两含公共点，层数等于区间最大深度，
    必为最少。
    """
    order = sorted(
        range(len(annotations)),
        key=lambda i: (annotations[i]["start"], annotations[i]["end"],
                       annotations[i]["label"], str(annotations[i]["id"])),
    )
    layers: list[list[int]] = []
    for i in order:
        a = annotations[i]
        best = None  # (末条批注 end, 层号)
        for li, layer in enumerate(layers):
            last_end = annotations[layer[-1]]["end"]
            if last_end <= a["start"] and (best is None or (last_end, li) < best):
                best = (last_end, li)
        if best is None:
            layers.append([i])
        else:
            layers[best[1]].append(i)
    return layers


def _bilou(layer_tags: list[str], covered: list[int], label: str) -> None:
    """把一条批注的 BILOU 标签写入某一层（covered 为覆盖的 token 下标）。"""
    if len(covered) == 1:
        layer_tags[covered[0]] = f"U-{label}"
        return
    layer_tags[covered[0]] = f"B-{label}"
    for i in covered[1:-1]:
        layer_tags[i] = f"I-{label}"
    layer_tags[covered[-1]] = f"L-{label}"


def _to_conll(tokens: list[dict], layer_tags: list[list[str]]) -> str:
    """生成 CoNLL 文本：每行 `token start end 各层标签`，空格分隔。"""
    lines = []
    for i, t in enumerate(tokens):
        cols = [t["text"], str(t["start"]), str(t["end"])]
        cols.extend(tags[i] for tags in layer_tags)
        lines.append(" ".join(cols))
    return "\n".join(lines) + ("\n" if lines else "")


def build_sequence(text: str, annotations: list[dict]) -> dict:
    """切分 token、分层并生成各层 BILOU 标签与 CoNLL 文本。

    返回 {"layers": 层数, "tokens": [{text, start, end, tags}], "conll": str}。
    批注只覆盖空白时抛出 SequenceError。
    """
    boundaries = set()
    for a in annotations:
        boundaries.add(a["start"])
        boundaries.add(a["end"])
    tokens = _split_at_boundaries(text, tokenize(text), boundaries)

    # 边界切开后，每个 token 相对每条批注要么完整在内、要么完整在外
    covered_of = []
    for a in annotations:
        covered = [i for i, t in enumerate(tokens)
                   if a["start"] <= t["start"] and t["end"] <= a["end"]]
        if not covered:
            raise SequenceError(
                f"批注 {a['id']} 的区间 [{a['start']}, {a['end']}) 只覆盖空白，"
                "没有可标注的 token")
        covered_of.append(covered)

    layers = assign_layers(annotations)
    layer_tags = []
    for layer in layers:
        tags = ["O"] * len(tokens)
        for i in layer:
            _bilou(tags, covered_of[i], annotations[i]["label"])
        layer_tags.append(tags)

    out_tokens = [
        {"text": t["text"], "start": t["start"], "end": t["end"],
         "tags": [tags[i] for tags in layer_tags]}
        for i, t in enumerate(tokens)
    ]
    return {"layers": len(layers), "tokens": out_tokens,
            "conll": _to_conll(tokens, layer_tags)}
