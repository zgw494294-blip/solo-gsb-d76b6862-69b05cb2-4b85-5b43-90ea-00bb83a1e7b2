"""可领取审阅工作副本：HTTP 接口端到端测试（FastAPI TestClient）。"""

import importlib

import pytest

# 在导入 app.main 之前指定临时数据库，避免污染本地 data/copies.db
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COPY_DB_PATH", str(tmp_path / "copies.db"))
    import app.main as main
    importlib.reload(main)  # 用新环境变量重新初始化全局仓库
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        yield c
    main._store = None


def ann(id, start, end, label="L"):
    return {"id": id, "start": start, "end": end, "label": label}


def _create(client, text="x" * 30, anns=None):
    r = client.post("/api/copies", json={"text": text,
                                         "annotations": anns or []})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------- 创建 / 读取 ----------

def test_create_and_get_snapshot(client):
    cid = _create(client, anns=[ann("a", 0, 3), ann("b", 2, 5)])
    snap = client.get(f"/api/copies/{cid}").json()
    assert snap["revision"] == 1
    assert snap["text"] == "x" * 30
    assert [(a["id"], a["holder"]) for a in snap["annotations"]] == \
        [("a", None), ("b", None)]
    assert snap["groups"] == [{"annotation_ids": ["a", "b"], "holder": None}]

    got = client.get(f"/api/copies/{cid}").json()
    assert got["id"] == cid


def test_create_accepts_empty_annotations(client):
    cid = _create(client, text="abc", anns=[])
    snap = client.get(f"/api/copies/{cid}").json()
    assert snap["annotations"] == [] and snap["groups"] == []


def test_get_unknown_copy_404(client):
    r = client.get("/api/copies/deadbeef")
    assert r.status_code == 404


def test_create_invalid_inputs_422(client):
    bodies = [
        {"text": "abc", "annotations": [{"id": "a", "start": 0, "end": 9,
                                          "label": "L"}]},  # 超出文本
        {"text": "abc", "annotations": [{"id": "a", "start": 2, "end": 2,
                                          "label": "L"}]},  # 零长
        {"text": "abc", "annotations": [{"id": "a", "start": 0, "end": 1,
                                          "label": ""}]},  # 空 label
        {"text": "abc", "annotations": [ann("a", 0, 1), ann("a", 1, 2)]},  # 重复 id
        {"text": "abc", "annotations": [{"id": 1, "start": 0, "end": 1,
                                          "label": "L"}]},  # 非字符串 id
        {"text": "abc", "annotations": [{"id": "a", "start": True, "end": 1,
                                          "label": "L"}]},  # 布尔偏移
    ]
    for body in bodies:
        r = client.post("/api/copies", json=body)
        assert r.status_code == 422, body


# ---------- 领取 / 释放 ----------

def test_claim_group_then_release(client):
    cid = _create(client, anns=[ann("a", 0, 4), ann("b", 3, 7),
                                ann("c", 12, 15)])
    r = client.post(f"/api/copies/{cid}/annotations/a/claim",
                    json={"reviewer": "alice"})
    assert r.status_code == 200
    assert {a["id"]: a["holder"] for a in r.json()["annotations"]} == \
        {"a": "alice", "b": "alice", "c": None}
    assert r.json()["revision"] == 2

    # 从组内另一条整组释放
    r = client.post(f"/api/copies/{cid}/annotations/b/release",
                    json={"reviewer": "alice"})
    assert r.status_code == 200
    assert all(a["holder"] is None for a in r.json()["annotations"])


def test_claim_idempotent_same_reviewer_no_revision_bump(client):
    cid = _create(client, anns=[ann("a", 0, 3)])
    client.post(f"/api/copies/{cid}/annotations/a/claim",
                json={"reviewer": "alice"})
    r = client.post(f"/api/copies/{cid}/annotations/a/claim",
                    json={"reviewer": "alice"})
    assert r.status_code == 200 and r.json()["revision"] == 2


def test_claim_other_holders_group_409(client):
    cid = _create(client, anns=[ann("a", 0, 4), ann("b", 3, 7)])
    client.post(f"/api/copies/{cid}/annotations/a/claim",
                json={"reviewer": "alice"})
    r = client.post(f"/api/copies/{cid}/annotations/b/claim",
                    json={"reviewer": "bob"})
    assert r.status_code == 409
    assert r.json()["detail"]["holder"] == "alice"
    # 失败后副本未变
    snap = client.get(f"/api/copies/{cid}").json()
    assert {a["id"]: a["holder"] for a in snap["annotations"]} == \
        {"a": "alice", "b": "alice"}
    assert snap["revision"] == 2


def test_release_unheld_409_and_other_holder_409(client):
    cid = _create(client, anns=[ann("a", 0, 4), ann("b", 3, 7)])
    r = client.post(f"/api/copies/{cid}/annotations/a/release",
                    json={"reviewer": "alice"})
    assert r.status_code == 409  # 无人持有
    client.post(f"/api/copies/{cid}/annotations/a/claim",
                json={"reviewer": "alice"})
    r = client.post(f"/api/copies/{cid}/annotations/b/release",
                    json={"reviewer": "bob"})
    assert r.status_code == 409  # 他人持有


def test_claim_release_bad_reviewer_422(client):
    cid = _create(client, anns=[ann("a", 0, 3)])
    for body in ({"reviewer": ""}, {"reviewer": 5}, {}, {"reviewer": True}):
        r = client.post(f"/api/copies/{cid}/annotations/a/claim", json=body)
        assert r.status_code == 422, body


def test_claim_unknown_annotation_404(client):
    cid = _create(client, anns=[])
    r = client.post(f"/api/copies/{cid}/annotations/x/claim",
                    json={"reviewer": "alice"})
    assert r.status_code == 404


def test_claim_unknown_copy_404(client):
    r = client.post("/api/copies/nope/annotations/a/claim",
                    json={"reviewer": "alice"})
    assert r.status_code == 404


# ---------- 修改 ----------

def test_modify_range_and_label_holder_only(client):
    cid = _create(client, anns=[ann("a", 0, 2), ann("b", 4, 7)])
    # 未持有不能修改
    r = client.patch(f"/api/copies/{cid}/annotations/a",
                     json={"reviewer": "alice", "start": 0, "end": 1,
                           "label": "X"})
    assert r.status_code == 409
    client.post(f"/api/copies/{cid}/annotations/a/claim",
                json={"reviewer": "alice"})
    r = client.patch(f"/api/copies/{cid}/annotations/a",
                     json={"reviewer": "alice", "start": 0, "end": 5,
                           "label": "X"})
    assert r.status_code == 200
    by_id = {a["id"]: a for a in r.json()["annotations"]}
    assert (by_id["a"]["start"], by_id["a"]["end"], by_id["a"]["label"]) == \
        (0, 5, "X")
    # 扩大后与 b 正长度重叠 → 未领取的 b 整组并入 alice
    assert by_id["b"]["holder"] == "alice"
    assert r.json()["groups"] == [{"annotation_ids": ["a", "b"],
                                   "holder": "alice"}]


def test_modify_split_group_keeps_holder(client):
    cid = _create(client, anns=[ann("a", 0, 4), ann("b", 3, 7)])
    client.post(f"/api/copies/{cid}/annotations/a/claim",
                json={"reviewer": "alice"})
    r = client.patch(f"/api/copies/{cid}/annotations/a",
                     json={"reviewer": "alice", "start": 15, "end": 18,
                           "label": "L"})
    assert r.status_code == 200
    assert {a["id"]: a["holder"] for a in r.json()["annotations"]} == \
        {"a": "alice", "b": "alice"}
    assert [g["annotation_ids"] for g in r.json()["groups"]] == [["b"], ["a"]]


def test_modify_touching_other_holder_is_allowed(client):
    # a 与 b 相接不连边：a 改成与 b 仍仅相接，允许
    cid = _create(client, anns=[ann("a", 0, 1), ann("b", 1, 4)])
    client.post(f"/api/copies/{cid}/annotations/a/claim",
                json={"reviewer": "alice"})
    client.post(f"/api/copies/{cid}/annotations/b/claim",
                json={"reviewer": "bob"})
    r = client.patch(f"/api/copies/{cid}/annotations/a",
                     json={"reviewer": "alice", "start": 0, "end": 1,
                           "label": "X"})
    assert r.status_code == 200
    assert {a["id"]: a["holder"] for a in r.json()["annotations"]} == \
        {"a": "alice", "b": "bob"}


def test_modify_into_other_holder_409_atomic(client):
    cid = _create(client, anns=[ann("a", 0, 1), ann("b", 5, 6)])
    client.post(f"/api/copies/{cid}/annotations/a/claim",
                json={"reviewer": "alice"})
    client.post(f"/api/copies/{cid}/annotations/b/claim",
                json={"reviewer": "bob"})
    r = client.patch(f"/api/copies/{cid}/annotations/a",
                     json={"reviewer": "alice", "start": 0, "end": 5,
                           "label": "X"})  # 与 b[5,6) 相接：允许
    assert r.status_code == 200
    r = client.patch(f"/api/copies/{cid}/annotations/a",
                     json={"reviewer": "alice", "start": 0, "end": 6,
                           "label": "X"})  # 与 b 重叠 → 409
    assert r.status_code == 409
    assert r.json()["detail"]["foreign"] == {"b": "bob"}
    snap = client.get(f"/api/copies/{cid}").json()
    by_id = {a["id"]: a for a in snap["annotations"]}
    assert (by_id["a"]["start"], by_id["a"]["end"]) == (0, 5)  # 上次成功的状态
    assert by_id["b"]["holder"] == "bob"


def test_modify_invalid_422_and_unchanged(client):
    cid = _create(client, text="abc", anns=[ann("a", 0, 2)])
    client.post(f"/api/copies/{cid}/annotations/a/claim",
                json={"reviewer": "alice"})
    for body in ({"reviewer": "alice", "start": 2, "end": 2, "label": "X"},
                 {"reviewer": "alice", "start": -1, "end": 2, "label": "X"},
                 {"reviewer": "alice", "start": 0, "end": 9, "label": "X"},
                 {"reviewer": "alice", "start": 0, "end": 1, "label": ""},
                 {"reviewer": "alice", "start": True, "end": 1, "label": "X"}):
        r = client.patch(f"/api/copies/{cid}/annotations/a", json=body)
        assert r.status_code == 422, body
    snap = client.get(f"/api/copies/{cid}").json()
    a = snap["annotations"][0]
    assert (a["start"], a["end"], a["label"]) == (0, 2, "L")
    assert snap["revision"] == 2


def test_modify_unknown_annotation_404(client):
    cid = _create(client, anns=[])
    r = client.patch(f"/api/copies/{cid}/annotations/x",
                     json={"reviewer": "alice", "start": 0, "end": 1,
                           "label": "X"})
    assert r.status_code == 404


# ---------- 副本批注可直接用于原审阅接口 ----------

def test_copy_annotations_usable_in_review_api(client):
    text = "目标是统一口径，验收时复核样本。"
    cid = _create(client, text=text,
                  anns=[ann("a1", 0, 4, "目标"), ann("a2", 13, 15, "对象")])
    # 领取连通组后修改一条，再把读取到的同一提交状态批注喂给原 /review
    client.post(f"/api/copies/{cid}/annotations/a1/claim",
                json={"reviewer": "alice"})
    snap = client.get(f"/api/copies/{cid}").json()
    r = client.post("/api/review/preview", json={
        "text": snap["text"],
        "max_chars": 10,
        "context_chars": 2,
        "annotations": [{"id": a["id"], "start": a["start"], "end": a["end"],
                         "label": a["label"]} for a in snap["annotations"]],
    })
    assert r.status_code == 200
    assert [f["fragment_id"] for f in r.json()["fragments"]] == \
        ["frag-1", "frag-2"]
