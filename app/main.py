"""批注迁移台 —— FastAPI 后端。"""

import os
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from app.arbitration import ArbitrationError, apply_arbitration, build_groups
from app.copies import CopyConflict, CopyError, CopyNotFound, CopyStore
from app.migration import DecisionError, apply_decisions, migrate_annotations
from app.nesting import NestingError, audit_nesting, export_nesting, validate_rules
from app.redaction import (
    RedactionError,
    redact_export as run_redact_export,
    validate_redact_labels,
)
from app.relabel import RelabelError, export_labels, migrate_labels, validate_relabel_rules
from app.review import ReviewError, apply_reviews, build_fragments, to_jsonl
from app.sequence import SequenceError, build_sequence
from app.units import UnitsError, convert_offsets, preview_offsets
from app.validation import ARBITRATE, LABELED, MIGRATE, RELABEL, REVIEW, UNITS, validate

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="批注迁移台")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# 可领取的审阅工作副本：SQLite 持久化（容器内经 COPIES_DB_PATH 指向挂卷目录）
copies_store = CopyStore(os.environ.get("COPIES_DB_PATH", "copies.db"))


class AnnotationIn(BaseModel):
    id: str | int
    start: int
    end: int


class UnitsAnnotationIn(BaseModel):
    # 允许并保留 label 等其余字段，导出时原样带回
    model_config = ConfigDict(extra="allow")

    id: str | int
    start: int
    end: int


class UnitsRequest(BaseModel):
    text: str
    annotations: list[UnitsAnnotationIn]
    source_unit: Literal["codepoint", "utf16", "utf8_byte"]
    target_unit: Literal["codepoint", "utf16", "utf8_byte"]


class MigrateRequest(BaseModel):
    old_text: str
    new_text: str
    annotations: list[AnnotationIn]


class Decision(BaseModel):
    action: Literal["choose", "abandon"]
    start: int | None = None
    end: int | None = None


class ExportRequest(MigrateRequest):
    decisions: dict[str, Decision] = {}


class ArbAnnotationIn(BaseModel):
    id: str | int
    start: int
    end: int
    label: str


class ArbitrateRequest(BaseModel):
    text: str
    left: list[ArbAnnotationIn] = []
    right: list[ArbAnnotationIn] = []


class ArbitrateExportRequest(ArbitrateRequest):
    decisions: dict[str, str] = {}


class SequenceRequest(BaseModel):
    text: str
    annotations: list[ArbAnnotationIn] = []


class RulesIn(BaseModel):
    root_labels: list[str] = []
    children: dict[str, list[str]] = {}


class NestingRequest(BaseModel):
    text: str
    annotations: list[ArbAnnotationIn] = []
    rules: RulesIn = RulesIn()


class ReviewAnnotationIn(BaseModel):
    # 审阅切片要求 id 为唯一字符串（pydantic 默认不把数字强转为字符串）
    id: str
    start: int
    end: int
    label: str


class ReviewRequest(BaseModel):
    text: str
    annotations: list[ReviewAnnotationIn] = []
    max_chars: int
    context_chars: int


class ReviewDecisionIn(BaseModel):
    id: str
    action: Literal["keep", "drop", "correct"]
    start: int | None = None
    end: int | None = None


class ReviewFragmentIn(BaseModel):
    fragment_id: str
    sha256: str
    decisions: list[ReviewDecisionIn] = []


class ReviewImportRequest(ReviewRequest):
    reviews: list[ReviewFragmentIn] = []


class RelabelAnnotationIn(BaseModel):
    # 允许并保留 label 以外的其余字段，导出时原样带回（只替换 label）
    model_config = ConfigDict(extra="allow")

    id: str | int
    start: int
    end: int
    label: str


class RelabelRuleIn(BaseModel):
    # from 是 Python 关键字，字段名用 from_、按别名 from 收发
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from")
    to: str
    pattern: str | None = None


class RelabelRulesIn(BaseModel):
    target_labels: list[str] = []
    rules: list[RelabelRuleIn] = []


class RelabelRequest(BaseModel):
    text: str
    annotations: list[RelabelAnnotationIn] = []
    rules: RelabelRulesIn = RelabelRulesIn()


class RedactRequest(BaseModel):
    text: str
    annotations: list[ArbAnnotationIn] = []
    redact_labels: list[str] = []


class CopyCreateRequest(BaseModel):
    text: str
    annotations: list[ReviewAnnotationIn] = []


class CopyIdsRequest(BaseModel):
    reviewer: str
    ids: list[str]


class CopyModifyRequest(BaseModel):
    reviewer: str
    id: str
    start: int
    end: int
    label: str


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/arbitrate")
def arbitrate_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "arbitrate.html")


@app.get("/sequence")
def sequence_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "sequence.html")


@app.get("/nesting")
def nesting_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "nesting.html")


@app.get("/units")
def units_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "units.html")


@app.get("/review")
def review_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "review.html")


@app.get("/relabel")
def relabel_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "relabel.html")


@app.post("/api/migrate")
def migrate(req: MigrateRequest):
    (anns,) = validate(req, MIGRATE)
    return {"results": migrate_annotations(req.old_text, req.new_text, anns)}


@app.post("/api/export")
def export(req: ExportRequest):
    (anns,) = validate(req, MIGRATE)
    results = migrate_annotations(req.old_text, req.new_text, anns)
    decisions = {k: v.model_dump() for k, v in req.decisions.items()}
    try:
        exported, unresolved = apply_decisions(results, decisions)
    except DecisionError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if unresolved:
        raise HTTPException(status_code=409, detail={
            "message": "存在未处理的待确认批注，禁止导出",
            "pending_ids": unresolved,
        })
    return {"annotations": exported}


@app.post("/api/arbitrate")
def arbitrate(req: ArbitrateRequest):
    left, right = validate(req, ARBITRATE)
    return {"groups": build_groups(req.text, left, right)}


@app.post("/api/arbitrate/export")
def arbitrate_export(req: ArbitrateExportRequest):
    left, right = validate(req, ARBITRATE)
    groups = build_groups(req.text, left, right)  # 后端重新计算分组
    try:
        annotations, records, unresolved = apply_arbitration(groups, req.decisions)
    except ArbitrationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if unresolved:
        raise HTTPException(status_code=409, detail={
            "message": "存在未处理的仲裁组，禁止导出",
            "pending_groups": unresolved,
        })
    return {"annotations": annotations, "decisions": records}


@app.post("/api/sequence/preview")
def sequence_preview(req: SequenceRequest):
    (anns,) = validate(req, LABELED)
    try:
        result = build_sequence(req.text, anns)
    except SequenceError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"layers": result["layers"], "tokens": result["tokens"]}


@app.post("/api/sequence/export")
def sequence_export(req: SequenceRequest):
    (anns,) = validate(req, LABELED)
    try:
        result = build_sequence(req.text, anns)
    except SequenceError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"conll": result["conll"], "layers": result["layers"],
            "token_count": len(result["tokens"])}


@app.post("/api/nesting/preview")
def nesting_preview(req: NestingRequest):
    (anns,) = validate(req, LABELED)
    rules = _validate_rules(req.rules)
    return audit_nesting(req.text, anns, rules)


@app.post("/api/nesting/export")
def nesting_export(req: NestingRequest):
    (anns,) = validate(req, LABELED)
    rules = _validate_rules(req.rules)
    result = audit_nesting(req.text, anns, rules)  # 后端重新计算审计结果
    if result["errors"]:
        raise HTTPException(status_code=409, detail={
            "message": "存在嵌套审计错误，禁止导出",
            "errors": result["errors"],
        })
    return {"tree": export_nesting(anns), "count": len(anns)}


@app.post("/api/units/preview")
def units_preview(req: UnitsRequest):
    (anns,) = validate(req, UNITS)
    try:
        return preview_offsets(req.text, anns, req.source_unit, req.target_unit)
    except UnitsError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/api/units/export")
def units_export(req: UnitsRequest):
    (anns,) = validate(req, UNITS)
    try:
        # 导出时后端重新建立边界映射并重算，不信任前端坐标
        result = convert_offsets(req.text, anns, req.source_unit, req.target_unit)
    except UnitsError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if result["errors"]:
        raise HTTPException(status_code=409, detail={
            "message": "存在偏移非法的批注，禁止导出",
            "errors": result["errors"],
        })
    return {"annotations": result["annotations"],
            "source_unit": req.source_unit, "target_unit": req.target_unit,
            "count": len(result["annotations"])}


@app.post("/api/review/preview")
def review_preview(req: ReviewRequest):
    (anns,) = validate(req, REVIEW)
    try:
        fragments = build_fragments(req.text, anns, req.max_chars, req.context_chars)
    except ReviewError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"fragments": fragments, "count": len(fragments)}


@app.post("/api/review/export")
def review_export(req: ReviewRequest):
    (anns,) = validate(req, REVIEW)
    try:
        # 导出时后端重新装箱切片，不信任前端结果
        fragments = build_fragments(req.text, anns, req.max_chars, req.context_chars)
    except ReviewError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"jsonl": to_jsonl(fragments), "count": len(fragments)}


@app.post("/api/review/import")
def review_import(req: ReviewImportRequest):
    (anns,) = validate(req, REVIEW)
    reviews = [r.model_dump() for r in req.reviews]
    try:
        # 后端用同一输入重新切片，校验 fragment_id、摘要与归属
        result = apply_reviews(req.text, anns, req.max_chars, req.context_chars, reviews)
    except ReviewError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if result["errors"]:
        raise HTTPException(status_code=422, detail={
            "message": "审阅结果校验失败，已拒绝整批",
            "errors": result["errors"],
        })
    return {"annotations": result["annotations"], "dropped": result["dropped"],
            "count": len(result["annotations"])}


@app.post("/api/relabel/preview")
def relabel_preview(req: RelabelRequest):
    (anns,) = validate(req, RELABEL)
    rules = _validate_relabel_rules(req.rules)
    return migrate_labels(req.text, anns, rules)


@app.post("/api/relabel/export")
def relabel_export(req: RelabelRequest):
    (anns,) = validate(req, RELABEL)
    rules = _validate_relabel_rules(req.rules)
    # 导出时后端重新求值迁移链，不信任前端结果
    result = export_labels(req.text, anns, rules)
    if result["errors"]:
        raise HTTPException(status_code=409, detail={
            "message": "存在标签迁移错误，禁止导出",
            "errors": result["errors"],
        })
    return {"annotations": result["annotations"], "count": len(result["annotations"])}


@app.post("/api/redact/export")
def redact_export_api(req: RedactRequest):
    (anns,) = validate(req, LABELED)
    try:
        labels = validate_redact_labels(req.redact_labels)
    except RedactionError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    # 导出时后端重新合并遮盖区并换算片段坐标，不信任前端结果
    return run_redact_export(req.text, anns, labels)


@app.post("/api/copies")
def copy_create(req: CopyCreateRequest):
    (anns,) = validate(req, REVIEW)
    return _copy_call(copies_store.create_copy, req.text, anns)


@app.get("/api/copies/{copy_id}")
def copy_read(copy_id: str):
    return _copy_call(copies_store.get_copy, copy_id)


@app.post("/api/copies/{copy_id}/claim")
def copy_claim(copy_id: str, req: CopyIdsRequest):
    return _copy_call(copies_store.claim, copy_id, req.reviewer, req.ids)


@app.post("/api/copies/{copy_id}/release")
def copy_release(copy_id: str, req: CopyIdsRequest):
    return _copy_call(copies_store.release, copy_id, req.reviewer, req.ids)


@app.post("/api/copies/{copy_id}/modify")
def copy_modify(copy_id: str, req: CopyModifyRequest):
    return _copy_call(copies_store.modify, copy_id, req.reviewer,
                      req.id, req.start, req.end, req.label)


def _copy_call(fn, *args):
    """把工作副本核心的异常映射为 HTTP 状态码：404 未知副本 / 409 持有权冲突 / 422 非法输入。"""
    try:
        return fn(*args)
    except CopyNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except CopyConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail)
    except CopyError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


def _validate_rules(rules: RulesIn) -> dict:
    """校验结构规则：root_labels 与每个 children 列表均不得含重复 label。"""
    try:
        return validate_rules({"root_labels": rules.root_labels,
                               "children": rules.children})
    except NestingError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


def _validate_relabel_rules(rules: RelabelRulesIn) -> dict:
    """校验标签迁移规则：目标标签非空不重复、正则合法、回退规则至多一条且排最后。"""
    try:
        return validate_relabel_rules(rules.model_dump(by_alias=True))
    except RelabelError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
