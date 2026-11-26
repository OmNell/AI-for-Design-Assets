"""入库后操作请求和审核前预览。"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LightKind = Literal["rename", "retag", "void", "restore", "move", "purge", "archive_repair"]
OperationKind = Literal["supplement", "rename", "retag", "void", "restore", "move", "purge", "archive_repair"]


class OperationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RenameProduct(OperationModel):
    id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=200)
    rename_file: bool = False


class RenameParams(OperationModel):
    project_name: str | None = Field(default=None, min_length=1, max_length=200)
    products: list[RenameProduct] = Field(default_factory=list, max_length=2000)


class TagChange(OperationModel):
    value: str = Field(max_length=500)
    confirmation_status: Literal["confirmed_value", "confirmed_unknown"]


class RetagProduct(OperationModel):
    id: int = Field(gt=0)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    free_text: str | None = Field(default=None, max_length=5000)
    tags: dict[str, TagChange] = Field(default_factory=dict, max_length=11)
    item_ids: list[int] | None = Field(default=None, max_length=2000)


class RetagParams(OperationModel):
    products: list[RetagProduct] = Field(min_length=1, max_length=2000)


class TargetParams(OperationModel):
    project: bool = False
    product_ids: list[int] = Field(default_factory=list, max_length=2000)
    file_ids: list[int] = Field(default_factory=list, max_length=2000)
    reason: str = Field(min_length=1, max_length=500)
    confirm: str | None = Field(default=None, max_length=200)


class MoveParams(OperationModel):
    company_id: int = Field(gt=0)
    won: bool
    reason: str = Field(min_length=1, max_length=500)


class ConflictChoice(OperationModel):
    id: int = Field(gt=0)
    choice: Literal["replace", "keep_both"]


class ProductAssignment(OperationModel):
    id: int = Field(gt=0)
    existing_product_id: int | None = Field(default=None, gt=0)


class SupplementParams(OperationModel):
    files: list[ConflictChoice] = Field(default_factory=list, max_length=2000)
    products: list[ProductAssignment] = Field(default_factory=list, max_length=2000)
    relation_reviews: dict[int, list[int]] = Field(default_factory=dict, max_length=2000)
    batch_set_items: dict[int, list[int]] = Field(default_factory=dict, max_length=2000)
    relation_batch_items: dict[int, list[int]] = Field(default_factory=dict, max_length=2000)


class OperationBody(OperationModel):
    params: dict[str, object]
    note: str = Field(default="", max_length=2000)


class OperationFile(BaseModel):
    id: int
    rel_path: str
    size: int
    mtime: int
    fingerprint: str | None
    conflict: Literal["new", "duplicate", "conflict"] | None
    choice: Literal["replace", "keep_both"] | None
    file_id: int | None


class ProjectOperations(BaseModel):
    project_id: int
    project_code: str
    folder_name: str
    status: str
    before: dict[str, object]
    kinds: list[OperationKind]
    active_batch_id: int | None


class OperationResponse(BaseModel):
    id: int
    batch_id: int
    project_id: int
    kind: OperationKind
    status: str
    editable: bool
    params: dict[str, object]
    after: dict[str, object]
    before: dict[str, object]
    conflicts: list[OperationFile]
    purge: dict[str, object] | None = None
    required_relation_reviews: list[int] = Field(default_factory=list)
