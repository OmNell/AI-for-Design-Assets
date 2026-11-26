"""正式入库进度与永久链接解析。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class IngestProgress(BaseModel):
    status: str
    step: int
    step_name: str
    error: str | None
    products: int
    files: int
    vectors_ready: int
    can_resume: bool


class ResolvedLink(BaseModel):
    kind: Literal["project", "product", "file"]
    target_id: int
    project_id: int
    link_id: str
    url: str
    status: str
    image_file_id: int | None = None
    deleted_at: datetime | None = None
    delete_reason: str | None = None
