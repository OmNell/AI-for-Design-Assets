"""运行参数及每日调度，共用持久任务队列。"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Literal
from zipfile import BadZipFile, ZipFile

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import BackupRecord, Batch, Job, Project, Setting, User, utcnow
from app.errors import AppError
from app.services.auth.service import AuthService
from app.services.jobs import JobQueue
from app.services.oplog import OplogService
from app.services.upload.service import UploadService

SYSTEM_KEY = "system_runtime"
ASSET_RECOVERY_CONFIRMED = "asset_recovery_confirmed"
PUBLICATION_BACKUP_PENDING = "publication_backup_pending"
PUBLISHED_STATES = ("active", "voided", "deleted")
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


def backup_allowed(session: Session, config: Settings) -> bool:
    if config.recovery.read_only:
        return False
    if config.mode == "dev":
        return True
    published = session.scalar(select(func.count(Project.id)).where(
        Project.ingested_at.is_not(None), Project.status.in_(PUBLISHED_STATES)))
    if not published:
        return False
    confirmation = session.scalar(select(Setting).where(Setting.key == ASSET_RECOVERY_CONFIRMED))
    if confirmation is not None:
        return confirmation.value is True
    return _has_local_archive(session, config, published)


def _has_local_archive(session: Session, config: Settings, published: int) -> bool:
    from app.services.backup.format import BACKUP_NAME, read_manifest

    receipts = session.scalars(select(BackupRecord).where(BackupRecord.verified_at.is_not(None),
        BackupRecord.local_retained.is_(True), BackupRecord.fingerprint.is_not(None))
        .order_by(BackupRecord.id.desc()).limit(7))
    for receipt in receipts:
        if not BACKUP_NAME.fullmatch(receipt.name):
            continue
        filename = config.paths.data_dir / "backup-stage" / receipt.name
        try:
            if filename.is_symlink() or filename.stat().st_size != receipt.size:
                continue
            with ZipFile(filename) as archive:
                original = read_manifest(archive)["identities"].get("projects", {}).get("count")
            if type(original) is int and 0 < original <= published:
                return True
        except (OSError, ValueError, BadZipFile, AttributeError, NotImplementedError):
            continue
    return False


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
            if kind == "backup" and not self.backup_allowed(session):
                if actor is not None:
                    raise AppError("backup_unconfirmed", "资产库尚未完成核验、没有已发布资产或处于只读模式，不能写入 NAS 备份区", 409)
                return None
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

    def backup_allowed(self, session: Session) -> bool:
        return backup_allowed(session, self.auth.config)

    def schedule_after_publication(self, session: Session) -> None:
        if not self.auth.config.recovery.backup_on_publication or not self.backup_allowed(session):
            return
        active = self._active_backup(session)
        if active == "queued":
            return
        pending = session.scalar(select(Setting).where(Setting.key == PUBLICATION_BACKUP_PENDING))
        if pending is None:
            pending = Setting(key=PUBLICATION_BACKUP_PENDING, value=True, encrypted=False)
            session.add(pending)
        if active is None:
            self._enqueue_publication_backup(session, pending)

    @staticmethod
    def _active_backup(session: Session) -> str | None:
        return session.scalar(select(Job.status).where(Job.kind == "backup", Job.status.in_(("queued", "running"))))

    def _enqueue_publication_backup(self, session: Session, pending: Setting) -> None:
        try:
            with session.begin_nested():
                delay = self.auth.config.recovery.backup_delay_minutes
                self.queue.enqueue("backup", {"requested_by": None, "slot": None, "exclusive": True, "source": "publication"},
                    scheduled_at=utcnow() + timedelta(minutes=delay), session=session)
                session.delete(pending)
        except AppError as exc:
            logging.getLogger("nas_workbench").warning("正式发布后的备份未入队（%s）", exc.code)

    def _flush_publication_backup(self, session: Session) -> None:
        pending = session.scalar(select(Setting).where(Setting.key == PUBLICATION_BACKUP_PENDING))
        if (pending is not None and self.auth.config.recovery.backup_on_publication
                and self._active_backup(session) is None and self.backup_allowed(session)):
            self._enqueue_publication_backup(session, pending)

    def backup_job_changed(self, session: Session, job: Job) -> None:
        if job.status in ("done", "failed"):
            self._flush_publication_backup(session)

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
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            self._flush_publication_backup(session)
        # 服务在调度分钟内重启时，由持久时隙防止重复启动。
        for kind in ("inspection", "backup"):
            self._scheduled(kind)

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=True)
