"""运行参数及每日调度，共用持久任务队列。"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Literal

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.db.models import Batch, Job, Setting, User
from app.errors import AppError
from app.services.auth.service import AuthService
from app.services.jobs import JobQueue
from app.services.oplog import OplogService
from app.services.upload.service import UploadService

SYSTEM_KEY = "system_runtime"
# 当前部署约定为中国标准时间。固定偏移可在缺少 IANA 数据的 Windows 上运行。
CST = timezone(timedelta(hours=8), "Asia/Shanghai")
SystemJob = Literal["inspection", "backup"]


class SystemParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_hours: int = Field(ge=1, le=168)
    chunk_mb: int = Field(ge=1, le=128)
    parallel_files: int = Field(ge=1, le=32)
    overdue_days: int = Field(ge=1, le=365)
    cooling_days: int = Field(ge=30, le=3650)
    inspection_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    backup_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"


def daily_time(value: str) -> str:
    match = re.fullmatch(r"(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*", value)
    if match is None:
        raise ValueError("调度配置必须是每日固定时分的 cron 表达式")
    minute, hour = map(int, match.groups())
    return f"{hour:02}:{minute:02}"


class SystemService:
    def __init__(self, auth: AuthService, queue: JobQueue) -> None:
        self.auth, self.queue = auth, queue
        self.upload: UploadService | None = None
        config = auth.config
        with auth.database.session() as session:
            row = session.scalar(select(Setting).where(Setting.key == SYSTEM_KEY))
            data = SystemParameters.model_validate(row.value) if row else SystemParameters(
                session_hours=config.server.session_hours, chunk_mb=config.upload.chunk_mb,
                parallel_files=config.upload.parallel_files, overdue_days=config.review.overdue_days,
                cooling_days=config.purge.cooling_days, inspection_time=daily_time(config.schedule.inspection),
                backup_time=daily_time(config.schedule.backup))
            if row is None:
                session.add(Setting(key=SYSTEM_KEY, value=data.model_dump(), encrypted=False))
        self.scheduler = BackgroundScheduler(timezone=CST)
        self._apply(data)

    def _apply(self, data: SystemParameters) -> None:
        config = self.auth.config
        config.server.session_hours = data.session_hours
        config.upload.chunk_mb = data.chunk_mb
        if self.upload is not None:
            self.upload.chunk_bytes = data.chunk_mb * 1024 * 1024
        config.upload.parallel_files = data.parallel_files
        config.review.overdue_days = data.overdue_days
        config.purge.cooling_days = data.cooling_days
        self.parameters = data
        for kind, value in (("inspection", data.inspection_time), ("backup", data.backup_time)):
            hour, minute = map(int, value.split(":"))
            self.scheduler.add_job(self._scheduled, CronTrigger(hour=hour, minute=minute, timezone=CST),
                                   args=[kind], id=kind, replace_existing=True,
                                   coalesce=True, max_instances=1, misfire_grace_time=45)

    def save(self, body: SystemParameters, actor: User, host: str) -> SystemParameters:
        with self.auth.lock:
            with self.auth.database.session() as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                self.queue._require_admin(session, actor)
                row = session.scalar(select(Setting).where(Setting.key == SYSTEM_KEY))
                before = row.value
                if (before["chunk_mb"], before["parallel_files"]) != (body.chunk_mb, body.parallel_files):
                    for batch in session.scalars(select(Batch).where(Batch.lock_active.is_(True), Batch.temp_path != "")):
                        if "chunk_bytes" not in batch.payload:
                            batch.payload = {**batch.payload, "chunk_bytes": before["chunk_mb"] * 1024 * 1024,
                                             "parallel_files": before["parallel_files"]}
                row.value = body.model_dump()
                OplogService(session).write(actor.id, "admin", "更新系统参数", "settings", SYSTEM_KEY,
                                            before, row.value, "成功", host)
            self._apply(body)
        return body

    def trigger(self, kind: SystemJob, actor: User | None = None, host: str = "定时调度",
                slot: str | None = None) -> Job | None:
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            if actor is not None:
                self.queue._require_admin(session, actor)
            if slot:
                key = f"schedule_last_{kind}"
                last = session.scalar(select(Setting).where(Setting.key == key))
                if last and last.value == slot:
                    return None
                if last:
                    last.value = slot
                else:
                    session.add(Setting(key=key, value=slot, encrypted=False))
            existing = session.scalar(select(Job).where(Job.kind == kind, Job.status.in_(("queued", "running"))))
            if existing:
                if actor:
                    raise AppError("maintenance_busy", "此任务已在等待或执行", 409)
                return None
            job = self.queue.enqueue(kind, {"requested_by": actor.id if actor else None, "slot": slot, "exclusive": True}, session=session)
            if actor:
                OplogService(session).write(actor.id, "admin", "手动触发维护", "job", str(job.id), None,
                                            {"kind": kind}, "成功", host)
            return job

    def tick(self, when: datetime) -> None:
        now = when.astimezone(CST)
        minute = now.strftime("%H:%M")
        slot = now.strftime("%Y-%m-%d %H:%M")
        data = self.parameters
        if minute == data.inspection_time:
            self.trigger("inspection", slot=slot)
        if minute == data.backup_time:
            self.trigger("backup", slot=slot)

    def _scheduled(self, kind: SystemJob) -> None:
        try:
            now = datetime.now(CST)
            if now.strftime("%H:%M") == getattr(self.parameters, f"{kind}_time"):
                self.trigger(kind, slot=now.strftime("%Y-%m-%d %H:%M"))
        except Exception as exc:
            logging.getLogger("nas_workbench").error("维护调度失败，类型：%s", type(exc).__name__)

    def start(self) -> None:
        self.scheduler.start()
        # 服务在调度分钟内重启时，由持久时隙防止重复启动。
        for kind in ("inspection", "backup"):
            self._scheduled(kind)

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=True)
