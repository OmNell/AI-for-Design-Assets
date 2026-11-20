"""NAS 工作根中的只读文件与分页。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BrowseQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(default="", max_length=768)
    page: int = Field(default=1, ge=1, le=100_000)
    sort: Literal["name", "size", "modified"] = "name"
    kind: Literal["all", "folder", "image", "file"] = "all"
    q: str = Field(default="", max_length=255)


class FileEntry(BaseModel):
    name: str
    path: str
    kind: Literal["folder", "image", "file"]
    is_dir: bool
    extension: str
    size: int
    modified_at: datetime


class FileInfo(FileEntry):
    exists: bool = True
    accessible: bool = True
    catalog_status: Literal["unmanaged", "unverified"] = "unmanaged"


class DirectoryPage(BaseModel):
    root_name: str
    path: str
    accessible: bool = True
    folders: list[FileEntry]
    files: list[FileEntry]
    page: int
    page_size: int
    total: int
