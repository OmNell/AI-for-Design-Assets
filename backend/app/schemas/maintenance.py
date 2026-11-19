"""管理后台接口模型。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RecordView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int


class RunView(RecordView):
    job_id: int
    started_at: datetime
    finished_at: datetime | None
    status: str
    summary: dict
    error: str | None


class FindingView(RecordView):
    run_id: int | None
    kind: str
    path: str
    project_id: int | None
    file_id: int | None
    detail: str
    auto_fixed: bool
    acknowledged_at: datetime | None
    acknowledged_by: int | None
    resolved_at: datetime | None
    created_at: datetime


class BackupView(RecordView):
    job_id: int
    name: str
    status: str
    size: int | None
    verified_at: datetime | None
    local_retained: bool
    error: str | None
    created_at: datetime


class CleanupTarget(BaseModel):
    key: str
    label: str
    kind: str
    batch_id: int | None
    project_code: str | None
    owner: str | None
    status: str
    eligible: bool
    files: int
    size: int
    created_at: datetime | None
    updated_at: datetime | None


class CleanupFile(BaseModel):
    path: str
    size: int
    mtime: float
    fingerprint: str


class CleanupPreview(RecordView):
    batch_id: int | None
    target: str
    entries: list[CleanupFile]
    confirmed_at: datetime | None


class CleanupConfirm(BaseModel):
    confirmation: str = Field(min_length=1, max_length=1024)


class ErrorItem(BaseModel):
    kind: str
    reason: str
    object_id: int
    batch_id: int | None = None
    job_id: int | None = None
    retry: Literal["system", "candidate", "actor"] | None = None
    generation: int | None = None


class LogFilter(BaseModel):
    since: datetime | None = None
    until: datetime | None = None
    user_id: int | None = None
    action: str | None = Field(default=None, max_length=160)
    object_type: str | None = Field(default=None, max_length=160)
    object_id: str | None = Field(default=None, max_length=160)
    result: str | None = Field(default=None, max_length=80)
    unclicked: bool = False
    page: int = Field(default=1, ge=1, le=100000)
    limit: int = Field(default=50, ge=1, le=200)


class LogView(RecordView):
    ts: datetime
    user_id: int | None
    username: str | None = None
    role: str
    action: str
    object_type: str
    object_id: str | None
    before: object | None
    after: object | None
    result: str
    client_host: str


class SearchLogView(RecordView):
    ts: datetime
    user_id: int
    username: str | None
    query: str
    result_count: int
    clicks: int
    duration_ms: int
    warnings: list[str]


class LogPage(BaseModel):
    total: int
    items: list[LogView]


class SearchLogPage(BaseModel):
    total: int
    items: list[SearchLogView]


class RepairCandidate(BaseModel):
    project_id: int
    project_code: str
    project_name: str
    ready: bool
    detail: str


class RepairRequest(BaseModel):
    project_ids: list[int] = Field(default_factory=list, max_length=1000)


class RepairResult(BaseModel):
    project_id: int
    batch_id: int | None
    error: str | None


class ThumbRebuild(BaseModel):
    product_ids: list[int] = Field(default_factory=list, max_length=100)
