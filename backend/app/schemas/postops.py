"""入库后操作的请求与响应。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app.schemas.audit import ChangePreview, IngestInfo
from app.schemas.review import RoundView


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProductRename(Strict):
    id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=100)


class RenameRequest(Strict):
    project_name: str | None = Field(default=None, max_length=60)
    products: list[ProductRename] = Field(default_factory=list, max_length=500)


class VoidRequest(Strict):
    product_ids: list[int] = Field(default_factory=list, max_length=1000)
    file_ids: list[int] = Field(default_factory=list, max_length=5000)
    reason: str = Field(max_length=500)
    # 作废套装时连同它包含的单品一起作废。
    cascade: StrictBool = False


class ReasonRequest(Strict):
    reason: str = Field(max_length=500)


class RestoreRequest(Strict):
    product_ids: list[int] = Field(default_factory=list, max_length=1000)
    file_ids: list[int] = Field(default_factory=list, max_length=5000)
    project: StrictBool = False
    reason: str = Field(max_length=500)


class MoveRequest(Strict):
    won: StrictBool
    company_id: int = Field(gt=0)
    reason: str = Field(max_length=500)


class PurgeTargets(Strict):
    history_file_ids: list[int] = Field(default_factory=list, max_length=5000)
    product_ids: list[int] = Field(default_factory=list, max_length=1000)
    project: StrictBool = False


class PurgeRequest(PurgeTargets):
    reason: str = Field(max_length=500)
    exception_reason: str = Field(default="", max_length=500)
    # 手动输入的立项编号或商品编号。
    confirm: str = Field(max_length=100)


class PurgeFile(BaseModel):
    path: str
    size: int


class PurgePreview(BaseModel):
    files: list[PurgeFile]
    count: int
    total_size: int
    confirm_label: str
    cooling_days: int
    in_cooling: bool
    earliest_allowed: datetime | None
    summary: list[str]


class OpDetail(BaseModel):
    id: int
    batch_no: str
    kind: str
    kind_label: str
    status: str
    project_id: int | None
    project_code: str
    folder_name: str
    owner_id: int
    owner_name: str
    round: int
    created_at: datetime
    preview: ChangePreview | None
    progress: IngestInfo | None
    can_withdraw: bool
    history: list[RoundView]
