"""核对页的请求与响应；标签取值是自由文本，不做取值校验。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TagView(BaseModel):
    value: str
    terms: list[str]
    source: str
    ai_value: str
    flag: str | None


class RelationView(BaseModel):
    batch_product_id: int
    source: str


class ReviewProduct(BaseModel):
    id: int
    batch_file_id: int
    kind: str
    display_name: str
    file_name: str
    fingerprint: str | None
    thumb_status: str | None
    thumb_error: str | None
    ai_status: str
    ai_error: str | None
    manually_filled: bool
    tags: dict[str, TagView]
    free_text: str
    free_text_source: str
    ai_free_text: str
    flags: list[str]
    relation_items: list[RelationView]
    relation_confirmed: bool
    # 补充时被替换的主图、修改标签时的已入库商品：沿用的商品编号。
    existing_product_id: int | None = None
    existing_code: str | None = None


class ReviewField(BaseModel):
    key: str
    label: str
    hint: str


class Pending(BaseModel):
    yellow: int
    relations_unconfirmed: int
    failed: int
    processing: int
    can_submit: bool


class FolderFile(BaseModel):
    name: str
    size: int


class FolderGroup(BaseModel):
    name: str
    files: list[FolderFile]


class ReviewBatch(BaseModel):
    id: int
    batch_no: str
    project_code: str
    folder_name: str
    company_name: str
    project_name: str
    status: str
    won: bool
    round: int
    owner_id: int
    owner_name: str
    uploader_id: int
    recommended_path: str | None
    note_to_reviewer: str | None
    editable: bool
    kind: str = "ingest"
    kind_label: str = "首次入库"
    project_id: int | None = None


class PinView(BaseModel):
    batch_product_id: int
    product_name: str
    x: float
    y: float
    text: str


class RoundView(BaseModel):
    round: int
    stage: str
    reviewer_name: str
    decision: str
    comment: str | None
    decided_at: datetime | None
    pins: list[PinView]


class FileVersion(BaseModel):
    size: int
    mtime: int
    fingerprint: str | None


class CompareItem(BaseModel):
    """补充包里与库中同位置的文件：新旧并排对比。"""
    rel_path: str
    choice: str | None
    is_image: bool
    old_file_id: int
    old_product_id: int | None
    old: FileVersion
    batch_file_id: int
    new: FileVersion


class SupplementSummary(BaseModel):
    new: int
    replaced: int
    kept_both: int
    skipped: int
    unresolved: int
    compare: list[CompareItem]
    skipped_files: list[str]
    notices: list[str]


class ReviewResponse(BaseModel):
    supplement: SupplementSummary | None = None
    history: list[RoundView] = Field(default_factory=list)
    batch: ReviewBatch
    fields: list[ReviewField]
    products: list[ReviewProduct]
    pending: Pending
    folders: list[FolderGroup]


class ProductUpdate(BaseModel):
    product: ReviewProduct
    pending: Pending


class ProductPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tags: dict[str, str] = Field(default_factory=dict)
    free_text: str | None = None
    confirm_fields: list[str] = Field(default_factory=list)
    resolve_flags: list[Literal["multi_object"]] = Field(default_factory=list)


class BulkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_ids: list[int] = Field(min_length=1, max_length=2000)
    tags: dict[str, str] = Field(min_length=1)

    @field_validator("product_ids")
    @classmethod
    def distinct(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("商品不能重复")
        return value


class RelationsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_ids: list[int] = Field(max_length=2000)


class SubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = ""


class OwnerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: int = Field(gt=0)


class OwnerResponse(BaseModel):
    id: int
    owner_id: int
    owner_name: str


class TagValue(BaseModel):
    term: str
    count: int
