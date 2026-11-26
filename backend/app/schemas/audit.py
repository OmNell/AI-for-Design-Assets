"""对照表、目录树、审核与待办的请求和响应。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app.schemas.review import PinView, ReviewResponse, RoundView

CHECKLIST = ("path", "naming", "one_product", "relations", "tags", "content")
CHECKLIST_LABELS = {"path": "存放位置正确", "naming": "项目命名与编号无误", "one_product": "一图一品",
                    "relations": "套装与单品关系正确", "tags": "标签合理", "content": "无不当内容"}


class IndustryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int | None = None
    name: str = Field(min_length=1, max_length=30)
    seq: int | None = None


class CompanyItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int | None = None
    name: str = Field(min_length=2, max_length=10)
    aliases: list[str] = Field(default_factory=list, max_length=50)
    industry_id: int | None = None
    industry_name: str | None = None
    seq: int | None = None


class CompanyMap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    industries: list[IndustryItem] = Field(default_factory=list)
    companies: list[CompanyItem] = Field(default_factory=list)


class TreeCompany(BaseModel):
    id: int
    name: str
    folder: str


class TreeIndustry(BaseModel):
    id: int
    name: str
    folder: str
    companies: list[TreeCompany]


class TreeRoot(BaseModel):
    won: bool
    folder: str
    industries: list[TreeIndustry]


class PersonView(BaseModel):
    id: int
    name: str


class QueueItem(BaseModel):
    id: int
    kind: str
    batch_no: str
    folder_name: str
    round: int
    uploader_name: str
    products: int
    files: int
    total_size: int
    waiting_since: datetime
    waiting_seconds: int
    claimed_by: PersonView | None
    claimed_by_me: bool
    blocked: bool
    self_review_exception: bool


class PathInfo(BaseModel):
    recommended: str | None
    final: str | None
    effective: str | None
    company_name: str
    company_in_tree: bool


class AuditStats(BaseModel):
    manual_edits: int
    ai_confirmed: int
    confirmed_unknown: int
    appended: int
    removed: int


class RelationSummary(BaseModel):
    set_id: int
    set_name: str
    items: list[str]
    ai_count: int
    manual_count: int
    confirmed: bool


class FolderCount(BaseModel):
    name: str
    count: int


class PinInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    batch_product_id: int
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    text: str = Field(default="", max_length=500)


class PinsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pins: list[PinInput] = Field(max_length=200)


class SupplementComparedFile(BaseModel):
    id: int
    rel_path: str
    size: int
    mtime: int
    fingerprint: str


class SupplementReplacement(BaseModel):
    original: SupplementComparedFile
    uploaded: SupplementComparedFile


class SupplementComparison(BaseModel):
    added: int = 0
    replaced: int = 0
    skipped: int = 0
    kept_both: int = 0
    materials: list[SupplementReplacement] = Field(default_factory=list)


class AuditResponse(BaseModel):
    version: str
    review: ReviewResponse
    stage: Literal["first", "final"] | None
    claimed_by: PersonView | None
    claimed_by_me: bool
    can_claim: bool
    blocked: bool
    can_edit_path: bool
    path: PathInfo
    acks: list[str]
    stats: AuditStats
    relations: list[RelationSummary]
    note: str | None
    folder_counts: list[FolderCount]
    history: list[RoundView]
    pins: list[PinView]
    checklist: dict[str, str]
    operation: dict | None = None
    supplement: SupplementComparison | None = None


class PathRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    won: StrictBool
    company_id: int = Field(gt=0)


class PassRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    checklist: dict[str, StrictBool]


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comment: str = Field(max_length=2000)
    pins: list[PinInput] | None = Field(default=None, max_length=200)


class TodoBatch(BaseModel):
    id: int
    label: str


class TodoItem(BaseModel):
    key: str
    label: str
    count: int
    link: str
    batches: list[TodoBatch]
