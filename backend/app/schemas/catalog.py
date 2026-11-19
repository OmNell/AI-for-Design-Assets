"""项目页、商品页、下载与永久链接的响应；不包含 NAS 的共享名、主机或绝对路径。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ProjectRow(BaseModel):
    id: int
    project_code: str
    project_name: str
    company_name: str
    industry_name: str
    won: bool
    link_url: str | None
    status: str
    ingested_at: datetime | None
    products: int
    files: int


class ProjectPage(BaseModel):
    items: list[ProjectRow]
    total: int
    page: int
    page_size: int


class ProductBrief(BaseModel):
    id: int
    product_code: str
    display_name: str
    kind: str
    status: str
    link_id: str | None
    related: list[int]
    tags: list[str] = Field(default_factory=list)
    link_url: str | None


class FolderCount(BaseModel):
    name: str
    count: int


class HistoryRow(BaseModel):
    id: int
    file_id: int
    old_rel_path: str
    new_rel_path: str
    reason: str
    created_at: datetime


class LogRow(BaseModel):
    ts: datetime
    user: str
    action: str
    result: str


class ProjectDetail(BaseModel):
    project: ProjectRow
    folder_name: str
    code_source: str
    link_id: str | None
    products_list: list[ProductBrief]
    folders: list[FolderCount]
    history: list[HistoryRow]
    logs: list[LogRow]


class FileRow(BaseModel):
    id: int
    name: str
    rel_path: str
    size: int
    mtime: int
    status: str
    category: str
    link_id: str | None
    link_url: str | None
    is_image: bool


class TagRow(BaseModel):
    key: str
    label: str
    value: str
    terms: list[str]
    source: str
    confirmation_status: str


class ProductDetail(BaseModel):
    id: int
    product_code: str
    display_name: str
    kind: str
    status: str
    free_text: str
    file_id: int
    tags: list[TagRow]
    related: list[ProductBrief]
    project: ProjectRow
    product_link: str | None
    project_link: str | None
    product_url: str | None
    ingested_at: datetime


class FileCheck(BaseModel):
    id: int
    name: str
    size: int
    status: str
    is_image: bool
    warning: str | None
    url: str


class DownloadManyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_ids: list[int] = Field(min_length=1, max_length=500)


class DownloadItem(BaseModel):
    file_id: int
    name: str | None = None
    size: int | None = None
    url: str | None = None
    error: str | None = None
    code: str | None = None

class ProjectFilters(BaseModel):
    q: str = Field(default="", max_length=120)
    company: str = Field(default="", max_length=120)
    industry: str = Field(default="", max_length=120)
    won: bool | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    sort: Literal["newest", "oldest", "code", "name"] = "newest"


class AssetFinding(BaseModel):
    id: int
    project_id: int
    file_id: int
    kind: str
    detail: str
    created_at: datetime

class DownloadProgress(BaseModel):
    status: Literal["pending", "sending", "sent", "interrupted", "failed"]
    message: str | None = None
