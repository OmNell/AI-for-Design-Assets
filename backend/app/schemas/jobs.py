"""任务管理响应，省略任务内部参数及 NAS 信息。"""
from datetime import datetime
from pydantic import BaseModel, ConfigDict


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    kind: str
    status: str
    attempts: int
    run_as_user_id: int | None
    error: str | None
    scheduled_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime
