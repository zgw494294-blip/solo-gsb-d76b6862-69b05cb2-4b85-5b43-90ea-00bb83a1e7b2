"""可领取的审阅工作副本核心逻辑与 SQLite 持久化。

偏移约定：所有 start/end 均为 Unicode 码点偏移，区间为左闭右开 [start, end)。

一个工作副本包含**固定文本**与若干批注（唯一字符串 id、start、end、非空
label），初始**无人持有**。

- **分组**：两条批注正长度重叠（区间交集长度 > 0，即 ``a.start < b.end`` 且
  ``b.start < a.end``）即在二者间连边，传递闭包得到连通组；**相接**
  （``end == start``）不连边。
- **持有不变量**：同一连通组的全部批注要么无人持有，要么由同一非空审阅者
  标识持有。领取 / 释放均按批注 id 定位其所在组并**整组**操作。
- **领取**：组无人持有 → 整组归该审阅者；已由本人持有 → 幂等成功且不产生
  新版本；由他人持有 → `GroupHledError`（409）。
- **释放**：仅持有者本人可释放整组；无人持有 → `NotHeldError`，他人持有 →
  `GroupHeldError`（均 409）。
- **修改**：仅持有者本人可修改单条批注的范围与 label，不增删批注。修改后
  在新几何上重算分组：新并入的未领取批注归该持有者；新组触及其他持有者的
  批注 → `ModifyConflictError`（409，范围与持有权均不变）；拆开的组保留原
  持有者。

`CopyState` 为不依赖数据库的纯内存核心（便于单测）；`CopyStore` 用 SQLite
持久化，每个变更操作在进程级锁内「完整读入 → 纯核心运算 → 整体写回」，
因此并发请求等价于逐次完整执行，不会丢失其他批注的修改。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from pathlib import Path

# ---------- 错误（HTTP 映射见 app.main） ----------


class CopyError(ValueError):
    """工作副本业务错误基类（409 / 422）。"""


class CopyValidationError(CopyError):
    """非法输入（对应 HTTP 422）。"""


class GroupHeldError(CopyError):
    """目标组正由其他审阅者持有（对应 HTTP 409）。"""

    def __init__(self, holder: str, group: list[str] | None = None):
        self.holder = holder
        self.group = group or []
        super().__init__(
            f"该批注所在连通组已由审阅者 {holder!r} 持有："
            + "、".join(self.group))


class NotHeldError(CopyError):
    """目标组无人持有 / 批注未由该审阅者持有（对应 HTTP 409）。"""

    def __init__(self, group: list[str] | None = None,
                 message: str = "该批注所在连通组当前无人持有，不能执行此操作"):
        self.group = group or []
        super().__init__(message)


class ModifyConflictError(CopyError):
    """修改后的新组触及其他审阅者持有的批注（对应 HTTP 409）。"""

    def __init__(self, foreign: dict[str, str]):
        # foreign: {触及到的他人批注 id: 持有者}
        self.foreign = dict(foreign)
        super().__init__(
            "修改后的连通组触及其他审阅者持有的批注："
            + "、".join(f"{i}（{h}）" for i, h in self.foreign.items()))


class CopyNotFound(LookupError):
    """工作副本不存在（对应 HTTP 404）。"""

    def __init__(self, copy_id: str):
        self.copy_id = copy_id
        super().__init__(f"工作副本不存在：{copy_id}")


class AnnotationNotFound(LookupError):
    """批注在该工作副本中不存在（对应 HTTP 404）。"""

    def __init__(self, ann_id: str):
        self.ann_id = ann_id
        super().__init__(f"批注不存在：{ann_id}")


# ---------- 输入校验 ----------


def check_reviewer(reviewer) -> None:
    """审阅者标识必须是非空字符串。"""
    if not isinstance(reviewer, str) or not reviewer:
        raise CopyValidationError("reviewer 必须是非空字符串")


def check_label(label) -> None:
    """label 必须是非空字符串。"""
    if not isinstance(label, str) or not label:
        raise CopyValidationError("label 必须是非空字符串")


def check_interval(start, end, text_len: int) -> None:
    """码点区间 [start, end) 必须满足 0 ≤ start < end ≤ 文本长度。"""
    if isinstance(start, bool) or isinstance(end, bool) or \
            not isinstance(start, int) or not isinstance(end, int) \
            or not 0 <= start < end <= text_len:
        raise CopyValidationError(
            f"区间 [{start}, {end}) 非法或超出文本长度 {text_len}"
            "（须满足 0 ≤ start < end ≤ 文本长度）")


# ---------- 分组（并查集） ----------


def _sort_key(a: dict) -> tuple:
    """批注稳定排序键：start、end、label、id。"""
    return (a["start"], a["end"], a["label"], str(a["id"]))


def connected_groups(annotations: list[dict]) -> list[list[str]]:
    """计算正长度重叠的传递连通组。

    相接（``end == start``）不连边。返回组的列表：组按组内最小排序键
    （即最早成员）排序，组内批注 id 按 (start, end, label, id) 排序。
    """
    ordered = sorted(annotations, key=_sort_key)
    n = len(ordered)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    for i in range(n):
        # start 已升序：ordered[j].start >= ordered[i].end 时，j 及其后
        # 都不可能与 i 正长度重叠（相接不算），可提前结束。
        for j in range(i + 1, n):
            if ordered[j]["start"] >= ordered[i]["end"]:
                break
            union(i, j)

    groups: dict[int, list[str]] = {}
    for i, a in enumerate(ordered):
        groups.setdefault(find(i), []).append(a["id"])
    # find 根取最小下标，即组内最早成员，按首次出现顺序输出天然有序
    return [groups[k] for k in sorted(groups)]


# ---------- 纯内存核心 ----------


class CopyState:
    """单个工作副本的内存状态（持有不变量见模块 docstring）。"""

    def __init__(self, copy_id: str, text: str,
                 annotations: list[dict], revision: int = 1):
        self.copy_id = copy_id
        self.text = text
        self.order: list[str] = [a["id"] for a in annotations]
        self.annotations: dict[str, dict] = {
            a["id"]: {"id": a["id"], "start": a["start"],
                      "end": a["end"], "label": a["label"]}
            for a in annotations
        }
        self.holders: dict[str, str] = {}
        self.revision = revision

    def _all_annotations(self) -> list[dict]:
        return [self.annotations[i] for i in self.order]

    def group_of(self, ann_id: str) -> list[str]:
        """返回批注所在连通组的全部 id（按稳定顺序）；批注不存在则报错。"""
        if ann_id not in self.annotations:
            raise AnnotationNotFound(ann_id)
        for group in connected_groups(self._all_annotations()):
            if ann_id in group:
                return group
        raise AssertionError("连通组计算异常")  # 不可达

    def claim(self, ann_id: str, reviewer: str) -> bool:
        """领取批注所在整组。返回是否发生了状态变更（本人重复领取为 False）。"""
        check_reviewer(reviewer)
        group = self.group_of(ann_id)
        owners = {self.holders[i] for i in group if self.holders.get(i) is not None}
        other = next(iter(owners - {reviewer}), None)
        if other is not None:
            raise GroupHeldError(other, group)
        changed = any(self.holders.get(i) is None for i in group)
        if changed:
            for i in group:
                self.holders[i] = reviewer
            self.revision += 1
        return changed

    def release(self, ann_id: str, reviewer: str) -> None:
        """释放批注所在整组（仅限持有者本人）。"""
        check_reviewer(reviewer)
        group = self.group_of(ann_id)
        owner = self.holders.get(group[0])  # 持有不变量：整组持有者相同
        if owner is None:
            raise NotHeldError(group)
        if owner != reviewer:
            raise GroupHeldError(owner, group)
        for i in group:
            self.holders.pop(i, None)
        self.revision += 1

    def modify(self, ann_id: str, reviewer: str,
               start: int, end: int, label: str) -> None:
        """修改单条批注的范围与 label（仅限持有者本人），随后重算分组。"""
        check_reviewer(reviewer)
        check_label(label)
        if ann_id not in self.annotations:
            raise AnnotationNotFound(ann_id)
        check_interval(start, end, len(self.text))  # 非法输入优先于持有冲突
        if self.holders.get(ann_id) != reviewer:
            raise NotHeldError(
                message=f"批注 {ann_id} 未由审阅者 {reviewer!r} 持有，不能修改")
        # 在候选新几何上重算分组（不改本对象，冲突时整体放弃）
        candidate = [dict(self.annotations[i]) for i in self.order]
        for a in candidate:
            if a["id"] == ann_id:
                a["start"], a["end"], a["label"] = start, end, label
        target_ids = next(g for g in connected_groups(candidate) if ann_id in g)
        foreign = {
            i: self.holders[i]
            for i in target_ids
            if self.holders.get(i) is not None and self.holders[i] != reviewer
        }
        if foreign:
            raise ModifyConflictError(foreign)
        # 应用新范围 / label；新组成员整组归本人（含并入的未领取批注）。
        # 不在新组内的其他批注持有权一律不动——原同组被拆开的碎片因此
        # 保留原持有者（即本人），其他组完全不受影响。
        self.annotations[ann_id].update(start=start, end=end, label=label)
        for i in target_ids:
            self.holders[i] = reviewer
        self.revision += 1

    def snapshot(self) -> dict:
        """返回同一提交状态下的文本、批注（含持有者）与连通组。"""
        anns = self._all_annotations()
        groups = connected_groups(anns)
        return {
            "id": self.copy_id,
            "text": self.text,
            "revision": self.revision,
            "annotations": [
                {"id": a["id"], "start": a["start"], "end": a["end"],
                 "label": a["label"], "holder": self.holders.get(a["id"])}
                for a in sorted(anns, key=_sort_key)
            ],
            "groups": [
                {"annotation_ids": ids,
                 "holder": self.holders.get(ids[0]) if ids else None}
                for ids in groups
            ],
        }


# ---------- SQLite 持久化 ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS copies (
    id         TEXT PRIMARY KEY,
    text       TEXT NOT NULL,
    revision   INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS annotations (
    copy_id TEXT NOT NULL REFERENCES copies(id) ON DELETE CASCADE,
    ann_id  TEXT NOT NULL,
    pos     INTEGER NOT NULL,
    start   INTEGER NOT NULL,
    end     INTEGER NOT NULL,
    label   TEXT NOT NULL,
    holder  TEXT,
    PRIMARY KEY (copy_id, ann_id)
);
CREATE INDEX IF NOT EXISTS idx_annotations_copy_pos
    ON annotations(copy_id, pos);
"""


def default_db_path() -> Path:
    """副本数据库路径：环境变量 COPY_DB_PATH 优先。

    容器内默认 ``/data/copies.db``（由 Compose 挂卷）；本地开发无 /data
    写权限时回退到仓库根目录下的 ``data/copies.db``。
    """
    env = os.environ.get("COPY_DB_PATH")
    if env:
        return Path(env)
    candidates = (
        Path("/data/copies.db"),
        Path(__file__).resolve().parent.parent / "data" / "copies.db",
    )
    for cand in candidates:
        try:
            cand.parent.mkdir(parents=True, exist_ok=True)
            probe = cand.parent / ".copies-db-write-test"
            probe.write_bytes(b"ok")
            probe.unlink()
            return cand
        except OSError:
            continue
    raise RuntimeError("无法确定可写的 SQLite 数据库路径（可设置 COPY_DB_PATH）")


class CopyStore:
    """SQLite 工作副本仓库；进程级锁保证变更操作串行完整执行。"""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else default_db_path()
        self._lock = threading.RLock()
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init_schema(self) -> None:
        """建表（幂等）：随容器启动初始化，重启后数据保留。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _load(self, conn: sqlite3.Connection, copy_id: str) -> CopyState:
        row = conn.execute(
            "SELECT text, revision FROM copies WHERE id=?", (copy_id,)).fetchone()
        if row is None:
            raise CopyNotFound(copy_id)
        text, revision = row
        rows = conn.execute(
            "SELECT ann_id, pos, start, end, label, holder FROM annotations "
            "WHERE copy_id=? ORDER BY pos", (copy_id,)).fetchall()
        state = CopyState(
            copy_id, text,
            [{"id": r[0], "start": r[2], "end": r[3], "label": r[4]} for r in rows],
            revision=revision)
        for r in rows:
            if r[5] is not None:
                state.holders[r[0]] = r[5]
        return state

    def create(self, text: str, annotations: list[dict]) -> dict:
        """创建工作副本，返回其初始快照（初始无人持有，revision=1）。"""
        copy_id = uuid.uuid4().hex
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO copies(id, text, revision) VALUES(?, ?, 1)",
                    (copy_id, text))
                conn.executemany(
                    "INSERT INTO annotations(copy_id, ann_id, pos, start, end, "
                    "label, holder) VALUES(?, ?, ?, ?, ?, ?, NULL)",
                    [(copy_id, a["id"], i, a["start"], a["end"], a["label"])
                     for i, a in enumerate(annotations)])
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
        return self.get(copy_id)

    def get(self, copy_id: str) -> dict:
        """读取同一提交状态的快照。"""
        with self._lock:
            conn = self._connect()
            try:
                return self._load(conn, copy_id).snapshot()
            finally:
                conn.close()

    def _apply(self, copy_id: str, operation) -> dict:
        """在锁内完整执行「读入 → 运算 → 整体写回」，失败不写任何内容。"""
        with self._lock:
            conn = self._connect()
            try:
                state = self._load(conn, copy_id)
                changed = operation(state)  # 抛错则直接跳出，不写库
                if changed is not False:     # 幂等领取（False）不产生新版本
                    conn.execute(
                        "UPDATE copies SET revision=? WHERE id=?",
                        (state.revision, copy_id))
                    # 整体写回该副本全部批注：持锁读入的最新状态上做的修改
                    # 不会覆盖其他批注（其他请求的修改已在本操作之前提交）。
                    conn.executemany(
                        "UPDATE annotations SET start=?, end=?, label=?, holder=? "
                        "WHERE copy_id=? AND ann_id=?",
                        [(state.annotations[i]["start"],
                          state.annotations[i]["end"],
                          state.annotations[i]["label"],
                          state.holders.get(i), copy_id, i)
                         for i in state.order])
                    conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
        return self.get(copy_id)

    def claim(self, copy_id: str, ann_id: str, reviewer: str) -> dict:
        return self._apply(copy_id, lambda st: st.claim(ann_id, reviewer))

    def release(self, copy_id: str, ann_id: str, reviewer: str) -> dict:
        return self._apply(copy_id, lambda st: st.release(ann_id, reviewer))

    def modify(self, copy_id: str, ann_id: str, reviewer: str,
               start: int, end: int, label: str) -> dict:
        return self._apply(
            copy_id,
            lambda st: st.modify(ann_id, reviewer, start, end, label))
