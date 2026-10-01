"""可领取的审阅工作副本核心逻辑测试（纯标准库，无需启动服务）。"""

import threading

import pytest

from app.copies import CopyConflict, CopyError, CopyNotFound, CopyStore


def ann(id, start, end, label="L"):
    return {"id": id, "start": start, "end": end, "label": label}


TEXT = "目标是统一口径，验收时复核样本。"  # 16 码点


@pytest.fixture
def store():
    s = CopyStore(":memory:")
    yield s
    s.close()


def make_copy(store, anns, text=TEXT):
    return store.create_copy(text, anns)["copy_id"]


def holders(view):
    return {a["id"]: a["holder"] for a in view["annotations"]}


# ---------- 创建与读取 ----------

def test_create_returns_unheld_copy_in_input_order(store):
    view = store.create_copy(TEXT, [ann("b", 5, 7, "甲"), ann("a", 0, 4, "乙")])
    assert view["copy_id"] == "copy-1"
    assert view["text"] == TEXT
    assert view["count"] == 2
    assert [a["id"] for a in view["annotations"]] == ["b", "a"]  # 保持输入顺序
    assert holders(view) == {"b": None, "a": None}  # 初始无人持有


def test_copy_ids_are_sequential(store):
    assert store.create_copy(TEXT, [])["copy_id"] == "copy-1"
    assert store.create_copy(TEXT, [])["copy_id"] == "copy-2"


def test_read_returns_same_committed_state(store):
    cid = make_copy(store, [ann("a", 0, 4)])
    store.claim(cid, "r1", ["a"])
    view = store.get_copy(cid)
    assert view == store.get_copy(cid)
    assert holders(view) == {"a": "r1"}


def test_read_unknown_copy_raises_not_found(store):
    with pytest.raises(CopyNotFound):
        store.get_copy("copy-9")
    for op in (lambda: store.claim("copy-9", "r", ["a"]),
               lambda: store.release("copy-9", "r", ["a"]),
               lambda: store.modify("copy-9", "r", "a", 0, 1, "L")):
        with pytest.raises(CopyNotFound):
            op()


def test_create_validates_annotations(store):
    with pytest.raises(CopyError):
        store.create_copy(TEXT, [ann("a", 0, 4), ann("a", 1, 2)])  # 重复 id
    with pytest.raises(CopyError):
        store.create_copy(TEXT, [{"id": 1, "start": 0, "end": 2, "label": "L"}])
    with pytest.raises(CopyError):
        store.create_copy(TEXT, [ann("a", 0, 4, "")])  # 空 label
    with pytest.raises(CopyError):
        store.create_copy(TEXT, [ann("a", 0, len(TEXT) + 1)])  # 越界
    with pytest.raises(CopyError):
        store.create_copy(TEXT, [ann("a", 3, 3)])  # start >= end


# ---------- 分组与领取 ----------

def _overlap_copy(store):
    # a[0,3) 与 b[2,5) 重叠、b 与 c[4,6) 重叠 → 三者传递连通；d[6,9) 与 c 相接不连边
    return make_copy(store, [ann("a", 0, 3), ann("b", 2, 5),
                             ann("c", 4, 6), ann("d", 6, 9)])


def test_claim_covers_whole_transitive_group(store):
    cid = _overlap_copy(store)
    view = store.claim(cid, "r1", ["a"])
    assert holders(view) == {"a": "r1", "b": "r1", "c": "r1", "d": None}


def test_touching_annotations_are_not_connected(store):
    cid = _overlap_copy(store)
    view = store.claim(cid, "r1", ["c"])
    # c 与 d 端点相接（end == start），不连边 → d 不在组内
    assert holders(view) == {"a": "r1", "b": "r1", "c": "r1", "d": None}


def test_claim_can_span_multiple_groups(store):
    cid = _overlap_copy(store)
    view = store.claim(cid, "r1", ["a", "d"])
    assert holders(view) == {"a": "r1", "b": "r1", "c": "r1", "d": "r1"}


def test_claim_by_same_reviewer_is_idempotent(store):
    cid = _overlap_copy(store)
    store.claim(cid, "r1", ["a"])
    view = store.claim(cid, "r1", ["c"])
    assert holders(view) == {"a": "r1", "b": "r1", "c": "r1", "d": None}


def test_claim_held_by_other_conflicts_and_keeps_state(store):
    cid = _overlap_copy(store)
    before = store.claim(cid, "r1", ["a"])
    with pytest.raises(CopyConflict) as exc:
        store.claim(cid, "r2", ["c"])  # c 与 a 同组，整组已归 r1
    assert exc.value.detail["conflicts"] == [
        {"id": "a", "holder": "r1"}, {"id": "b", "holder": "r1"},
        {"id": "c", "holder": "r1"}]
    assert store.get_copy(cid) == before  # 失败不改变副本


def test_claim_invalid_input_raises_and_keeps_state(store):
    cid = _overlap_copy(store)
    before = store.get_copy(cid)
    for kwargs in ({"reviewer": "", "ids": ["a"]},
                   {"reviewer": "r1", "ids": []},
                   {"reviewer": "r1", "ids": ["zzz"]}):
        with pytest.raises(CopyError):
            store.claim(cid, **kwargs)
    assert store.get_copy(cid) == before


# ---------- 释放 ----------

def test_release_by_holder_clears_whole_group(store):
    cid = _overlap_copy(store)
    store.claim(cid, "r1", ["a"])
    view = store.release(cid, "r1", ["c"])  # 给组内任一 id 即整组释放
    assert holders(view) == {"a": None, "b": None, "c": None, "d": None}


def test_release_then_others_can_claim(store):
    cid = _overlap_copy(store)
    store.claim(cid, "r1", ["a"])
    store.release(cid, "r1", ["a"])
    view = store.claim(cid, "r2", ["a"])
    assert holders(view)["a"] == "r2"


def test_release_by_non_holder_conflicts_and_keeps_state(store):
    cid = _overlap_copy(store)
    store.claim(cid, "r1", ["a"])
    before = store.get_copy(cid)
    with pytest.raises(CopyConflict) as exc:
        store.release(cid, "r2", ["a"])  # 他人持有
    assert exc.value.detail["conflicts"]
    with pytest.raises(CopyConflict):
        store.release(cid, "r2", ["d"])  # 无人持有也不是请求者持有
    assert store.get_copy(cid) == before


# ---------- 修改 ----------

def test_modify_by_holder_updates_range_and_label(store):
    cid = make_copy(store, [ann("a", 0, 4, "旧"), ann("b", 10, 12)])
    store.claim(cid, "r1", ["a"])
    view = store.modify(cid, "r1", "a", 1, 6, "新")
    a = next(x for x in view["annotations"] if x["id"] == "a")
    assert (a["start"], a["end"], a["label"], a["holder"]) == (1, 6, "新", "r1")
    assert view["count"] == 2  # 不增删批注


def test_modify_merges_unclaimed_into_holders_group(store):
    cid = make_copy(store, [ann("a", 0, 2), ann("b", 5, 7)])
    store.claim(cid, "r1", ["a"])
    view = store.modify(cid, "r1", "a", 0, 6, "L")  # 扩到与 b 重叠
    assert holders(view) == {"a": "r1", "b": "r1"}  # 并入的未领取批注归该持有者


def test_modify_touching_other_holder_conflicts_and_rolls_back(store):
    cid = make_copy(store, [ann("a", 0, 2), ann("b", 5, 7), ann("c", 8, 10)])
    store.claim(cid, "r1", ["a"])
    store.claim(cid, "r2", ["b"])
    before = store.get_copy(cid)
    with pytest.raises(CopyConflict) as exc:
        store.modify(cid, "r1", "a", 0, 9, "L")  # 触及 r2 持有的 b（并经 b 触及 c）
    assert exc.value.detail["conflicts"] == [{"id": "b", "holder": "r2"}]
    after = store.get_copy(cid)
    assert after == before  # 范围和持有权均不变
    a = next(x for x in after["annotations"] if x["id"] == "a")
    assert (a["start"], a["end"]) == (0, 2)


def test_modify_split_group_keeps_original_holder(store):
    cid = make_copy(store, [ann("a", 0, 3), ann("b", 2, 5)])
    store.claim(cid, "r1", ["a"])  # a、b 同组归 r1
    view = store.modify(cid, "r1", "a", 10, 12, "L")  # a 移走，组拆开
    assert holders(view) == {"a": "r1", "b": "r1"}  # 拆开的组保留原持有者


def test_modify_requires_holder(store):
    cid = make_copy(store, [ann("a", 0, 2), ann("b", 5, 7)])
    store.claim(cid, "r1", ["a"])
    before = store.get_copy(cid)
    with pytest.raises(CopyConflict):
        store.modify(cid, "r2", "a", 0, 3, "L")  # 他人持有
    with pytest.raises(CopyConflict):
        store.modify(cid, "r2", "b", 5, 6, "L")  # 无人持有同样不可改
    assert store.get_copy(cid) == before


def test_modify_invalid_input_raises_and_keeps_state(store):
    cid = make_copy(store, [ann("a", 0, 4)])
    store.claim(cid, "r1", ["a"])
    before = store.get_copy(cid)
    for kwargs in ({"start": 0, "end": len(TEXT) + 1},  # 越界
                   {"start": 2, "end": 2},              # start >= end
                   {"start": 0, "end": 2, "label": ""}):  # 空 label
        with pytest.raises(CopyError):
            store.modify(cid, "r1", "a", **{"label": "L", **kwargs})
    with pytest.raises(CopyError):
        store.modify(cid, "r1", "zzz", 0, 2, "L")  # 未知批注
    with pytest.raises(CopyError):
        store.modify(cid, "", "a", 0, 2, "L")  # 空审阅者
    assert store.get_copy(cid) == before


# ---------- 持久化（重启保留副本） ----------

def test_state_survives_reopen(tmp_path):
    db = str(tmp_path / "copies.db")
    s1 = CopyStore(db)
    cid = s1.create_copy(TEXT, [ann("a", 0, 4), ann("b", 3, 6)])["copy_id"]
    s1.claim(cid, "r1", ["a"])
    s1.modify(cid, "r1", "a", 0, 5, "新")
    s1.close()

    s2 = CopyStore(db)  # 模拟重启后重开同一库文件
    view = s2.get_copy(cid)
    assert holders(view) == {"a": "r1", "b": "r1"}
    a = next(x for x in view["annotations"] if x["id"] == "a")
    assert (a["start"], a["end"], a["label"]) == (0, 5, "新")
    assert s2.create_copy(TEXT, [])["copy_id"] == "copy-2"  # 编号续号不冲突
    s2.close()


# ---------- 并发：相当于逐次完整执行 ----------

def test_concurrent_claims_of_same_group_exactly_one_wins(store):
    cid = make_copy(store, [ann("a", 0, 3), ann("b", 2, 5)])
    outcomes = {}

    def worker(name):
        try:
            store.claim(cid, name, ["a"])
            outcomes[name] = "ok"
        except CopyConflict:
            outcomes[name] = "conflict"

    threads = [threading.Thread(target=worker, args=(f"r{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert list(outcomes.values()).count("ok") == 1  # 恰好一个审阅者领取成功
    winner = [n for n, o in outcomes.items() if o == "ok"][0]
    assert holders(store.get_copy(cid)) == {"a": winner, "b": winner}


def test_concurrent_modifies_do_not_lose_each_other(store):
    text = "x" * 100
    cid = store.create_copy(text, [ann("a", 0, 2), ann("c", 20, 22)])["copy_id"]
    store.claim(cid, "r1", ["a"])
    store.claim(cid, "r2", ["c"])
    a_ends, c_ends = list(range(3, 13)), list(range(23, 33))

    def mod_a():
        for e in a_ends:
            store.modify(cid, "r1", "a", 0, e, "L")

    def mod_c():
        for e in c_ends:
            store.modify(cid, "r2", "c", 20, e, "L")

    t1, t2 = threading.Thread(target=mod_a), threading.Thread(target=mod_c)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    view = store.get_copy(cid)
    by_id = {a["id"]: a for a in view["annotations"]}
    # 两个序列各自完整推进，互不覆盖对方的修改
    assert by_id["a"]["end"] == a_ends[-1] and by_id["a"]["holder"] == "r1"
    assert by_id["c"]["end"] == c_ends[-1] and by_id["c"]["holder"] == "r2"
