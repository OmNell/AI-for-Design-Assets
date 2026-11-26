"""入库后操作进度和删除确认。"""
from pydantic import BaseModel, Field


class ExecuteInput(BaseModel):
    confirm_code: str | None = Field(default=None, max_length=256)


class DeleteFileView(BaseModel):
    file_id: int
    path: str
    size: int
    fingerprint: str
    history: bool


class OperationExecution(BaseModel):
    status: str
    step: int
    step_name: str
    error: str | None
    can_resume: bool
    confirmation_code: str | None = None
    purge_files: list[DeleteFileView] = Field(default_factory=list)
    product_codes: list[str] = Field(default_factory=list)
    relationships: list[str] = Field(default_factory=list)
