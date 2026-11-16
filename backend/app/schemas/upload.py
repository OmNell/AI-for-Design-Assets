from __future__ import annotations

from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


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


class UploadFileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    rel_path: str
    size: int
    mtime: int
    upload_status: str
    bytes_received: int
    fingerprint: str | None


class BatchResponse(BaseModel):
    id: int
    batch_no: str
    project_code: str
    folder_name: str
    original_folder_name: str
    status: str
    queued: bool
    won: bool
    updated_at: datetime
    files: list[UploadFileResponse]
    directories: list[str]
    chunk_bytes: int
    parallel_files: int
    upload_token: str | None = None
    differences: dict[str, list[str]] | None = None
