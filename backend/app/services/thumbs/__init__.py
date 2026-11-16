"""主图三档本地缓存；来源始终使用上传人 NAS 会话。"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.models import Batch, BatchFile, Job, User
from app.errors import AppError
from app.services.auth.service import AuthService
from app.services.jobs import JobContext, JobFailure, JobQueue
from app.services.nas import NasPermissionDenied, Region
from app.services.nas.factory import create_paths
from app.services.oplog import OplogService
from .images import ImageDecodeError, render_variants


def is_main_image(file: BatchFile) -> bool:
    return re.fullmatch(r"(?:套装|单品)/[^/]+\.(?:jpg|jpeg|png)", file.rel_path, re.IGNORECASE) is not None


def source_version(file: BatchFile) -> str:
    return hashlib.sha256(f"{file.id}\0{file.rel_path}\0{file.size}\0{file.mtime}\0{file.fingerprint}".encode()).hexdigest()


class ThumbService:
    def __init__(self, auth: AuthService, queue: JobQueue) -> None:
        self.auth = auth
        self.queue = queue
        self.root = auth.config.paths.data_dir / "thumbs" / "batch"
        self.paths = create_paths(auth.config)

    def _directory(self, batch_id: int, file_id: int) -> Path:
        return self.root / str(batch_id) / str(file_id)

    @staticmethod
    def _source(session: Session, job: Job) -> tuple[Batch, BatchFile] | None:
        payload = job.payload if isinstance(job.payload, dict) else {}
        batch = session.get(Batch, payload.get("batch_id"))
        file = session.get(BatchFile, payload.get("batch_file_id"))
        if (batch is None or file is None or file.batch_id != batch.id or batch.status == "已撤回"
            or file.upload_status != "done" or not is_main_image(file)
            or job.run_as_user_id != batch.uploader_id or payload.get("source_version") != source_version(file)):
            return None
        return batch, file

    def enqueue(self, session: Session, batch: Batch, file: BatchFile) -> None:
        if not is_main_image(file):
            return
        version = source_version(file)
        # 调用方持有认证锁，与 complete 同一事务，重复完成不会派发两次。
        references = batch.payload.get("thumb_jobs", {})
        if not isinstance(references, dict):
            references = {}
        existing_id = references.get(str(file.id))
        job = session.get(Job, existing_id) if isinstance(existing_id, int) else None
        if job is not None and isinstance(job.payload, dict) and job.payload.get("source_version") == version:
            return
        job = self.queue.enqueue("thumb", {"batch_id": batch.id, "batch_file_id": file.id, "source_version": version},
                                 run_as_user_id=batch.uploader_id, session=session)
        batch.payload = {**batch.payload, "thumb_jobs": {**references, str(file.id): job.id}}

    def on_job_state(self, session: Session, job: Job) -> None:
        source = self._source(session, job)
        if source is None:
            return
        _, file = source
        file.thumb_status = "pending" if job.status == "queued" else job.status
        file.thumb_error = job.error if job.status == "failed" else None

    def handle(self, context: JobContext) -> None:
        with self.auth.lock, self.auth.database.session() as session:
            source = self._source(session, context.job)
            if source is None:
                raise JobFailure("图片已变更或批次已撤回", retryable=False)
            batch, file = source
            path = self.paths.join(Region.TEMP, batch.batch_no, str(batch.payload["folder_name"]), file.rel_path)
            batch_id, file_id = batch.id, file.id
        if context.fs is None:
            raise JobFailure("操作人会话已失效", retryable=False)
        self.root.parent.mkdir(parents=True, exist_ok=True)
        # 只在本地暂存派生 JPEG；NAS 原件始终通过本人流读取，不复制第二份原件。
        with tempfile.TemporaryDirectory(prefix="thumb-", dir=self.root.parent) as stage:
            try:
                with context.fs.open_read(path) as stream:
                    render_variants(stream, Path(stage))
            except ImageDecodeError as error:
                raise JobFailure(str(error), retryable=False) from None
            except NasPermissionDenied:
                raise JobFailure("NAS 无权限，无法读取主图", retryable=False) from None
            with self.auth.lock, self.auth.database.session() as session:
                if self._source(session, context.job) is None:
                    raise JobFailure("图片已变更或批次已撤回", retryable=False)
                directory = self._directory(batch_id, file_id)
                directory.mkdir(parents=True, exist_ok=True)
                for size in (300, 1600, 1024):
                    os.replace(Path(stage) / f"{size}.jpg", directory / f"{size}.jpg")

    def image(self, batch_id: int, file_id: int, size: int, actor: User) -> bytes:
        with self.auth.lock, self.auth.database.session() as session:
            current = session.get(User, actor.id)
            batch = session.get(Batch, batch_id)
            if batch is None:
                raise AppError("batch_not_found", "批次不存在", 404)
            if current is None or not current.enabled or not (
                "admin" in current.roles or current.id == batch.owner_id
                or current.id == batch.first_reviewer_id and "first_reviewer" in current.roles
                or current.id == batch.final_reviewer_id and "final_reviewer" in current.roles):
                raise AppError("forbidden", "无权查看该批次缩略图", 403)
            file = session.get(BatchFile, file_id)
            if batch.status == "已撤回" or file is None or file.batch_id != batch.id or not is_main_image(file):
                raise AppError("thumbnail_not_found", "缩略图不存在", 404, {"retryable": False})
            if size not in (300, 1600, 1024):
                raise AppError("invalid_thumbnail_size", "图片尺寸不支持", 422)
            if file.thumb_status == "failed":
                raise AppError("thumbnail_failed", "缩略图生成失败", 404, {"retryable": False, "reason": file.thumb_error})
            target = self._directory(batch_id, file_id) / f"{size}.jpg"
            if file.upload_status != "done" or file.thumb_status != "done" or not target.is_file():
                raise AppError("thumbnail_pending", "缩略图尚未生成，请稍后重试", 404, {"retryable": True})
            return target.read_bytes()

    def invalidate(self, batch_id: int, file_id: int | None = None) -> None:
        directory = self.root / str(batch_id) if file_id is None else self._directory(batch_id, file_id)
        if directory.exists():
            shutil.rmtree(directory)

    def resume_for_user(self, user_id: int) -> None:
        # 重启后凭据仅能由本人重新登录恢复，不借用管理员或系统 NAS 账号。
        with self.auth.lock, self.auth.database.session() as session:
            for job in session.scalars(select(Job).where(Job.kind == "thumb", Job.run_as_user_id == user_id,
                                                         Job.status == "failed", Job.error == "操作人会话已失效")):
                if self._source(session, job) is None:
                    continue
                session.execute(update(Job).where(Job.id == job.id, Job.status == "failed").values(status="queued", attempts=0,
                                error=None, scheduled_at=None, started_at=None, finished_at=None))
                session.refresh(job)
                self.on_job_state(session, job)
                OplogService(session).write(user_id, "uploader", "恢复缩略图任务", "job", str(job.id),
                                           {"status": "failed"}, {"status": "queued"}, "成功", "本人重新登录")
        self.queue.wake()
