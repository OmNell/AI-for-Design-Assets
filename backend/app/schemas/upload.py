from __future__ import annotations

from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rel_path: str = Field(min_length=1)
    size: int = Field(default=0, ge=0, le=2**63-1)
    mtime: int = Field(default=0, ge=0, le=2**63-1)
    is_dir: bool = False


class PrecheckRequest(BaseModel):
    folder_name: str
    files: list[ManifestEntry]


class Issue(BaseModel):
    rule_code: str
    level: Literal["block", "warn", "ignore"]
    path: str
    message: str


class PrecheckResponse(BaseModel):
    project_code: str
    company_name: str
    project_name: str
    code_status: Literal["new", "exists", "need_h_code"]
    project_id: int | None = None
    issues: list[Issue]
    path_prefix_length: int = 0


class CreateBatchRequest(PrecheckRequest):
    kind: Literal["ingest"] = "ingest"
    won: bool
    historical_year: int | None = Field(default=None, ge=1, le=9999)
    acks: list[str] = Field(default_factory=list)


class ResumeRequest(PrecheckRequest):
    accept_changes: bool = False
    acks: list[str] = Field(default_factory=list)


class TerminateBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=500)
    confirmed: bool = False

    @field_validator("reason", mode="before")
    @classmethod
    def trim_reason(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class UploadFileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    rel_path: str
    size: int
    mtime: int
    upload_status: str
    bytes_received: int
    fingerprint: str | None
    fingerprint_kind: str | None = None
    thumb_status: str | None = None
    thumb_error: str | None = None
    conflict: str | None = None
    conflict_choice: str | None = None


class BatchResponse(BaseModel):
    id: int
    batch_no: str
    project_code: str
    kind: str = "ingest"
    project_id: int | None = None
    folder_name: str
    original_folder_name: str
    status: str
    queued: bool
    won: bool
    terminated_at: datetime | None = None
    termination_reason: str | None = None
    updated_at: datetime
    files: list[UploadFileResponse]
    directories: list[str]
    chunk_bytes: int
    parallel_files: int
    upload_token: str | None = None
    differences: dict[str, list[str]] | None = None
    owner_id: int | None = None
    uploader_id: int | None = None
    recommended_path: str | None = None
    tag_labels: dict[str, str] = Field(default_factory=dict)
    ai_progress: AiProgress = Field(default_factory=lambda: AiProgress())
    products: list[BatchProductResponse] = Field(default_factory=list)


class AiProgress(BaseModel):
    items_total: int = 0
    items_done: int = 0
    sets_total: int = 0
    sets_done: int = 0
    failed: int = 0


class BatchProductResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    batch_file_id: int
    kind: str
    display_name: str
    ai_status: str
    ai_error: str | None
    visual_ai_succeeded: bool = False
    tags: dict[str, object]
    free_text: str
    flags: list[str]
    relation_items: list[dict[str, object]]
    relation_confirmed: bool


BatchResponse.model_rebuild()
