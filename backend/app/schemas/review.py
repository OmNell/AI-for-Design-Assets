"""核对页的请求与响应；标签取值是自由文本，不做取值校验。"""
from __future__ import annotations

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


class ReviewResponse(BaseModel):
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
