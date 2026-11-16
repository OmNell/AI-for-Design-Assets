"""项目页、商品页、下载与永久链接的响应；不包含 NAS 的共享名、主机或绝对路径。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProjectRow(BaseModel):
    id: int
    project_code: str
    project_name: str
    company_name: str
    industry_name: str
    won: bool
    current_path: str
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


class TagRow(BaseModel):
    key: str
    label: str
    value: str
    terms: list[str]
    source: str


class ProductDetail(BaseModel):
    id: int
    product_code: str
    display_name: str
    kind: str
    status: str
    free_text: str
    file_id: int
    image_rel_path: str
    tags: list[TagRow]
    related: list[ProductBrief]
    project: ProjectRow
    product_link: str | None
    project_link: str | None
    ingested_at: datetime


class FileCheck(BaseModel):
    id: int
    name: str
    size: int


class DownloadManyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_ids: list[int] = Field(min_length=1, max_length=500)


class DownloadItem(BaseModel):
    file_id: int
    name: str
    size: int
    url: str


class LinkTarget(BaseModel):
    link_id: str
    kind: str
    target_id: int
    status: str
    url: str | None
    message: str | None
