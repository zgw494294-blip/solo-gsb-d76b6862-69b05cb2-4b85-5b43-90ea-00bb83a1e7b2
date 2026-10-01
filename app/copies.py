"""可领取的审阅工作副本核心逻辑与 SQLite 持久化。

偏移约定：所有 start/end 均为 Unicode 码点偏移，区间为左闭右开 [start, end)。

工作副本把一份固定文本与一组批注（唯一字符串 id、start、end、非空 label）
存为可并发操作的持久状态，初始无人持有。审阅者凭非空标识按批注 id 领取 /
释放；两条批注区间交集长度大于 0 即连边（端点相接不连边），传递连通组
必须整组操作，因此同一连通组内持有者一致（全无人持有，或全为同一审阅者）：

- 领取：整组归请求者；任一批注被他人持有 → 整体失败（409），副本不变；
- 释放：仅限持有者，整组释放；组的当前持有者不是请求者（含无人持有）
  → 409，副本不变；
- 修改：仅持有者可改单条批注的范围与 label（不增删批注）；修改后重算
  分组——并入该组的未领取批注归该持有者；触及他人持有的批注 → 409
  （范围与持有权均不变）；拆开的组保留原持有者。

并发与持久化：所有读写经同一连接与锁串行化（相当于逐次完整执行），写
操作在单个 SQLite 事务内完成、失败整体回滚（不改变副本），读取在锁内
完成、返回同一提交状态；行级更新保证不丢失其他批注的修改。SQLite 文件
路径由环境变量 COPIES_DB_PATH 指定（默认 ./copies.db），容器内挂卷后
重启保留副本。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager


class CopyError(ValueError):
    """非法输入（HTTP 422）：空审阅者、空 ids、未知批注、非法区间、空 label 等。"""


class CopyNotFound(LookupError):
    """工作副本不存在（HTTP 404）。"""


class CopyConflict(RuntimeError):
    """持有权冲突（HTTP 409）：conflicts 为 [{id, holder}]，holder 为 None 表示无人持有。"""

    def __init__(self, message: str, conflicts: list[dict]):
        super().__init__(message)
        self.detail = {"message": message, "conflicts": conflicts}


def _overlaps(a: dict, b: dict) -> bool:
    """区间 [start, end) 的交集长度是否大于 0（端点相接不算重叠）。"""
    return max(a["start"], b["start"]) < min(a["end"], b["end"])


def _components(annotations: list[dict]) -> list[list[dict]]:
    """按正长度重叠关系把批注划分为传递连通组（并查集，保持输入顺序）。"""
    parent = list(range(len(annotations)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(annotations):
        for j in range(i + 1, len(annotations)):
            if _overlaps(a, annotations[j]):
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj

    buckets: dict[int, list[dict]] = {}
    for i, a in enumerate(annotations):
        buckets.setdefault(find(i), []).append(a)
    return list(buckets.values())


def _require_reviewer(reviewer) -> str:
    """审阅者标识必须是非空字符串。"""
    if not isinstance(reviewer, str) or not reviewer:
        raise CopyError(f"审阅者标识不能为空：{reviewer!r}")
    return reviewer


def _require_ids(ids) -> list[str]:
    """批注 id 列表必须非空、元素均为字符串；去重后保持给定顺序。"""
    if not isinstance(ids, (list, tuple)) or not ids:
        raise CopyError("ids 必须是非空的批注 id 列表")
    out = []
    for i in ids:
        if not isinstance(i, str):
            raise CopyError(f"批注 id 类型非法：{i!r}（须为 str）")
        if i not in out:
            out.append(i)
    return out


def _check_annotations(text: str, annotations: list[dict]) -> list[dict]:
    """创建校验：唯一字符串 id、码点区间落在文本内、label 非空（与 REVIEW 策略一致）。"""
    if not isinstance(text, str):
        raise CopyError(f"文本必须是字符串：{type(text).__name__}")
    n = len(text)
    seen: set = set()
    out = []
    for a in annotations:
        if not isinstance(a.get("id"), str):
            raise CopyError(f"批注 id 类型非法：{a.get('id')!r}（须为 str）")
        if not 0 <= a["start"] < a["end"] <= n:
            raise CopyError(
                f"批注 {a['id']} 的区间 [{a['start']}, {a['end']}) "
                f"非法或超出文本长度 {n}")
        if not a.get("label"):
            raise CopyError(f"批注 {a['id']} 的 label 不能为空")
        if a["id"] in seen:
            raise CopyError(f"批注 id 重复：{a['id']}")
        seen.add(a["id"])
        out.append({"id": a["id"], "start": a["start"], "end": a["end"],
                    "label": a["label"]})
    return out


def _view(copy_id: str, text: str, anns: list[dict]) -> dict:
    """副本视图：固定文本、按输入顺序排列的批注及当前持有者。"""
    return {"copy_id": copy_id, "text": text,
            "annotations": [dict(a) for a in anns], "count": len(anns)}


class CopyStore:
    """工作副本存储：单 SQLite 连接 + 锁串行化，写操作单事务提交。

    表结构：copies(copy_id, text)、annotations(copy_id, ann_id, start, end,
    label, holder, seq)（holder 为 NULL 表示无人持有，seq 为输入顺序）、
    meta(copy_seq)（副本编号计数器，重启后续号不冲突）。
    """

    def __init__(self, db_path: str):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False,
                                     isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS copies ("
                "  copy_id TEXT PRIMARY KEY,"
                "  text TEXT NOT NULL"
                ")")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS annotations ("
                "  copy_id TEXT NOT NULL REFERENCES copies(copy_id),"
                "  ann_id TEXT NOT NULL,"
                "  start INTEGER NOT NULL,"
                "  end INTEGER NOT NULL,"
                "  label TEXT NOT NULL,"
                "  holder TEXT,"
                "  seq INTEGER NOT NULL,"
                "  PRIMARY KEY (copy_id, ann_id)"
                ")")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS meta ("
                "  key TEXT PRIMARY KEY,"
                "  value INTEGER NOT NULL"
                ")")
            self._conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('copy_seq', 0)")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _txn(self):
        """单事务写：进入即取写锁（BEGIN IMMEDIATE），异常整体回滚。"""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    def _load(self, copy_id: str) -> tuple[str, list[dict]]:
        """读出副本当前状态（须在锁内调用）；副本不存在时抛出 CopyNotFound。"""
        row = self._conn.execute(
            "SELECT text FROM copies WHERE copy_id = ?", (copy_id,)).fetchone()
        if row is None:
            raise CopyNotFound(f"未知的工作副本：{copy_id}")
        rows = self._conn.execute(
            "SELECT ann_id, start, end, label, holder FROM annotations "
            "WHERE copy_id = ? ORDER BY seq", (copy_id,)).fetchall()
        anns = [{"id": r["ann_id"], "start": r["start"], "end": r["end"],
                 "label": r["label"], "holder": r["holder"]} for r in rows]
        return row["text"], anns

    def _set_holder(self, copy_id: str, ann_ids: list[str],
                    holder: str | None) -> None:
        """行级更新持有者（holder 为 None 表示释放），不动其他批注。"""
        self._conn.executemany(
            "UPDATE annotations SET holder = ? WHERE copy_id = ? AND ann_id = ?",
            [(holder, copy_id, i) for i in ann_ids])

    @staticmethod
    def _affected_groups(anns: list[dict], wanted: list[str],
                         by_id: dict) -> list[list[dict]]:
        """wanted 中的 id 命中的传递连通组（未知 id 先报 422）。"""
        unknown = [i for i in wanted if i not in by_id]
        if unknown:
            raise CopyError("未知的批注 id：" + "、".join(unknown))
        return [g for g in _components(anns)
                if any(a["id"] in wanted for a in g)]

    # ---------- 创建与读取 ----------

    def create_copy(self, text: str, annotations: list[dict]) -> dict:
        """创建工作副本：初始无人持有，copy_id 依次为 copy-1、copy-2……"""
        anns = _check_annotations(text, annotations)
        with self._lock, self._txn():
            self._conn.execute(
                "UPDATE meta SET value = value + 1 WHERE key = 'copy_seq'")
            seq = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'copy_seq'").fetchone()["value"]
            copy_id = f"copy-{seq}"
            self._conn.execute(
                "INSERT INTO copies (copy_id, text) VALUES (?, ?)",
                (copy_id, text))
            self._conn.executemany(
                "INSERT INTO annotations (copy_id, ann_id, start, end, label,"
                " holder, seq) VALUES (?, ?, ?, ?, ?, NULL, ?)",
                [(copy_id, a["id"], a["start"], a["end"], a["label"], i)
                 for i, a in enumerate(anns)])
            _, loaded = self._load(copy_id)
            return _view(copy_id, text, loaded)

    def get_copy(self, copy_id: str) -> dict:
        """读取同一提交状态的批注及持有者。"""
        with self._lock:
            text, anns = self._load(copy_id)
            return _view(copy_id, text, anns)

    # ---------- 领取与释放（整组操作） ----------

    def claim(self, copy_id: str, reviewer: str, ids: list[str]) -> dict:
        """整组领取：任一批注被他人持有则整体失败（409），副本不变。"""
        reviewer = _require_reviewer(reviewer)
        wanted = _require_ids(ids)
        with self._lock, self._txn():
            text, anns = self._load(copy_id)
            groups = self._affected_groups(anns, wanted,
                                           {a["id"]: a for a in anns})
            conflicts = [{"id": a["id"], "holder": a["holder"]}
                         for g in groups for a in g
                         if a["holder"] is not None and a["holder"] != reviewer]
            if conflicts:
                raise CopyConflict("部分批注已被他人持有，整组领取失败", conflicts)
            self._set_holder(copy_id, [a["id"] for g in groups for a in g],
                             reviewer)
            _, anns = self._load(copy_id)
            return _view(copy_id, text, anns)

    def release(self, copy_id: str, reviewer: str, ids: list[str]) -> dict:
        """整组释放：仅限持有者（组的当前持有者不是请求者，含无人持有 → 409）。"""
        reviewer = _require_reviewer(reviewer)
        wanted = _require_ids(ids)
        with self._lock, self._txn():
            text, anns = self._load(copy_id)
            groups = self._affected_groups(anns, wanted,
                                           {a["id"]: a for a in anns})
            conflicts = [{"id": a["id"], "holder": a["holder"]}
                         for g in groups for a in g
                         if a["holder"] != reviewer]
            if conflicts:
                raise CopyConflict("仅持有者可释放，整组释放失败", conflicts)
            self._set_holder(copy_id, [a["id"] for g in groups for a in g],
                             None)
            _, anns = self._load(copy_id)
            return _view(copy_id, text, anns)

    # ---------- 修改单条批注（仅持有者） ----------

    def modify(self, copy_id: str, reviewer: str, ann_id: str,
               start: int, end: int, label: str) -> dict:
        """修改单条批注的范围与 label（不增删批注），修改后重算分组。

        并入该组的未领取批注归该持有者；触及他人持有的批注 → 409（事务
        回滚，范围与持有权均不变）；拆开的组保留原持有者。
        """
        reviewer = _require_reviewer(reviewer)
        with self._lock, self._txn():
            text, anns = self._load(copy_id)
            ann = next((a for a in anns if a["id"] == ann_id), None)
            if ann is None:
                raise CopyError(f"未知的批注 id：{ann_id}")
            n = len(text)
            if not (isinstance(start, int) and isinstance(end, int)
                    and 0 <= start < end <= n):
                raise CopyError(
                    f"批注 {ann_id} 的区间 [{start}, {end}) "
                    f"非法或超出文本长度 {n}")
            if not isinstance(label, str) or not label:
                raise CopyError(f"批注 {ann_id} 的 label 不能为空")
            if ann["holder"] != reviewer:
                raise CopyConflict(
                    "仅持有者可修改批注",
                    [{"id": ann_id, "holder": ann["holder"]}])
            # 应用新范围后重算分组（内存试算，失败靠事务回滚，副本不变）
            ann["start"], ann["end"], ann["label"] = start, end, label
            group = next(g for g in _components(anns)
                         if any(a["id"] == ann_id for a in g))
            others = [{"id": a["id"], "holder": a["holder"]} for a in group
                      if a["holder"] is not None and a["holder"] != reviewer]
            if others:
                raise CopyConflict(
                    "修改后的范围触及他人持有的批注，范围与持有权均不变", others)
            self._conn.execute(
                "UPDATE annotations SET start = ?, end = ?, label = ? "
                "WHERE copy_id = ? AND ann_id = ?",
                (start, end, label, copy_id, ann_id))
            # 并入的未领取批注归该持有者；拆开的组不在 group 内，保留原持有者
            self._set_holder(copy_id, [a["id"] for a in group], reviewer)
            _, anns = self._load(copy_id)
            return _view(copy_id, text, anns)
