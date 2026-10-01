"""可领取审阅工作副本：SQLite 仓库测试（持久化、重启保留、并发串行化）。"""

import threading

import pytest

from app.copies import (
    AnnotationNotFound,
    CopyNotFound,
    CopyStore,
    GroupHeldError,
    ModifyConflictError,
)

TEXT = "x" * 30


def ann(id, start, end, label="L"):
    return {"id": id, "start": start, "end": end, "label": label}


@pytest.fixture()
def store(tmp_path):
    return CopyStore(tmp_path / "copies.db")


def _ids_holders(snap):
    return {a["id"]: a["holder"] for a in snap["annotations"]}


# ---------- 基本持久化 ----------

def test_create_returns_initial_snapshot(store):
    snap = store.create(TEXT, [ann("a", 0, 3), ann("b", 2, 5)])
    assert len(snap["id"]) == 32
    assert snap["revision"] == 1
    assert _ids_holders(snap) == {"a": None, "b": None}
    assert snap["groups"] == [{"annotation_ids": ["a", "b"], "holder": None}]


def test_reopen_store_retains_copy(tmp_path):
    db = tmp_path / "copies.db"
    s1 = CopyStore(db)
    snap = s1.create(TEXT, [ann("a", 0, 4), ann("b", 3, 7), ann("c", 15, 18)])
    cid = snap["id"]
    s1.claim(cid, "a", "alice")
    s1.modify(cid, "a", "alice", 0, 2, "新")

    # 模拟容器重启：用同一数据库文件重新打开仓库
    s2 = CopyStore(db)
    again = s2.get(cid)
    assert again["text"] == TEXT
    assert again["revision"] == 3  # 领取 + 修改
    assert _ids_holders(again) == {"a": "alice", "b": "alice", "c": None}
    changed = [a for a in again["annotations"] if a["id"] == "a"][0]
    assert (changed["start"], changed["end"], changed["label"]) == (0, 2, "新")
    assert again["groups"] == [{"annotation_ids": ["a"], "holder": "alice"},
                               {"annotation_ids": ["b"], "holder": "alice"},
                               {"annotation_ids": ["c"], "holder": None}]


def test_schema_init_is_idempotent(tmp_path):
    db = tmp_path / "copies.db"
    CopyStore(db)
    CopyStore(db)  # 再次初始化不报错、不破坏数据


def test_get_unknown_copy_404(store):
    with pytest.raises(CopyNotFound):
        store.get("deadbeef")


def test_claim_unknown_copy_404(store):
    with pytest.raises(CopyNotFound):
        store.claim("deadbeef", "a", "alice")


def test_claim_unknown_annotation_404(store):
    cid = store.create(TEXT, [])["id"]
    with pytest.raises(AnnotationNotFound):
        store.claim(cid, "nope", "alice")


def test_failed_claim_persists_nothing(store):
    cid = store.create(TEXT, [ann("a", 0, 4), ann("b", 3, 7)])["id"]
    store.claim(cid, "a", "alice")
    with pytest.raises(GroupHeldError):
        store.claim(cid, "b", "bob")
    snap = store.get(cid)
    assert _ids_holders(snap) == {"a": "alice", "b": "alice"}
    assert snap["revision"] == 2


def test_failed_modify_is_fully_rolled_back(store):
    cid = store.create(TEXT, [ann("a", 0, 2), ann("b", 4, 7)])["id"]
    store.claim(cid, "a", "alice")
    store.claim(cid, "b", "bob")
    with pytest.raises(ModifyConflictError):
        store.modify(cid, "a", "alice", 0, 5, "X")
    snap = store.get(cid)
    a = [a for a in snap["annotations"] if a["id"] == "a"][0]
    assert (a["start"], a["end"], a["label"]) == (0, 2, "L")
    assert _ids_holders(snap) == {"a": "alice", "b": "bob"}
    assert snap["revision"] == 3  # 两次领取，失败修改不增版本


def test_release_persisted(store):
    cid = store.create(TEXT, [ann("a", 0, 4), ann("b", 3, 7)])["id"]
    store.claim(cid, "a", "alice")
    store.release(cid, "b", "alice")
    snap = store.get(cid)
    assert all(h is None for h in _ids_holders(snap).values())
    assert snap["revision"] == 3


# ---------- 并发：等价于逐次完整执行 ----------

def test_concurrent_claims_serialized_exactly_one_winner(store):
    cid = store.create(TEXT, [ann("a", 0, 4), ann("b", 3, 7)])["id"]
    errors = []

    def claim(reviewer, barrier):
        barrier.wait()
        try:
            store.claim(cid, "a", reviewer)
        except GroupHeldError as exc:
            errors.append((reviewer, exc.holder))

    n = 16
    barrier = threading.Barrier(n)
    threads = [threading.Thread(target=claim, args=(f"r{i}", barrier))
               for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == n - 1  # 恰有一个领取成功
    snap = store.get(cid)
    holders = set(_ids_holders(snap).values())
    assert len(holders) == 1
    assert next(iter(holders)) in {f"r{i}" for i in range(n)}
    assert all(holder == errors[0][1] for holder in holders)
    assert snap["revision"] == 2  # 其余失败请求均不写库，版本只增一次


def test_concurrent_independent_annotation_edits_all_survive(store):
    # 互不重叠的批注由不同审阅者并行修改：任何修改都不得丢失
    anns = [ann(f"a{i}", 3 * i + 0, 3 * i + 2, "L") for i in range(10)]
    cid = store.create(TEXT, anns)["id"]
    for i in range(10):
        store.claim(cid, f"a{i}", f"r{i}")
    base_rev = store.get(cid)["revision"]

    barrier = threading.Barrier(10)

    def edit(i):
        barrier.wait()
        s, e = 3 * i, 3 * i + 1  # 各自缩短，仍互不重叠 / 不相接到他人
        store.modify(cid, f"a{i}", f"r{i}", s, e, f"lab{i}")

    threads = [threading.Thread(target=edit, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snap = store.get(cid)
    assert snap["revision"] == base_rev + 10
    by_id = {a["id"]: a for a in snap["annotations"]}
    for i in range(10):
        a = by_id[f"a{i}"]
        assert (a["start"], a["end"], a["label"], a["holder"]) == \
            (3 * i, 3 * i + 1, f"lab{i}", f"r{i}")


def test_concurrent_modify_race_into_other_holder(store):
    # a、b 初始相距不连边，分属 alice/bob；两人同时把范围向中间扩展到
    # 重叠区：任一时刻必有人先成功、另一人看到他人持有 → 409 且结果自洽
    cid = store.create(TEXT, [ann("a", 0, 1), ann("b", 5, 6)])["id"]
    store.claim(cid, "a", "alice")
    store.claim(cid, "b", "bob")
    outcomes = {"ok": [], "conflict": []}
    barrier = threading.Barrier(2)

    def grow(ann_id, reviewer, bucket):
        barrier.wait()
        try:
            store.modify(cid, ann_id, reviewer, 0, 4, "L") if ann_id == "a" \
                else store.modify(cid, ann_id, reviewer, 2, 6, "L")
            outcomes["ok"].append(bucket)
        except ModifyConflictError:
            outcomes["conflict"].append(bucket)

    t1 = threading.Thread(target=grow, args=("a", "alice", "a"))
    t2 = threading.Thread(target=grow, args=("b", "bob", "b"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(outcomes["ok"]) == 1 and len(outcomes["conflict"]) == 1
    snap = store.get(cid)
    holders = _ids_holders(snap)
    # 冲突失败方的范围与持有权保持修改前状态
    loser = outcomes["conflict"][0]
    a = {x["id"]: x for x in snap["annotations"]}[loser]
    assert (a["start"], a["end"]) == ((0, 1) if loser == "a" else (5, 6))
    assert holders[loser] == ("alice" if loser == "a" else "bob")


def test_snapshot_groups_consistent_after_reopen(tmp_path):
    db = tmp_path / "copies.db"
    s = CopyStore(db)
    cid = s.create(TEXT, [ann("a", 0, 4), ann("b", 3, 7), ann("c", 12, 15)])["id"]
    s.claim(cid, "b", "alice")
    snap = CopyStore(db).get(cid)
    assert snap["groups"] == [
        {"annotation_ids": ["a", "b"], "holder": "alice"},
        {"annotation_ids": ["c"], "holder": None},
    ]
