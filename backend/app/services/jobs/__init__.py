"""以 jobs 表为队列，条件更新抢占；任务处理器只使用本人 NAS 会话。"""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import ContextManager

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.db.database import Database
from app.db.models import Job, User, UserSession, utcnow
from app.errors import AppError
from app.services.auth.service import is_expired
from app.services.nas import NasFs, NasNotFound, NasPermissionDenied, NasSessionExpired, NasUnavailable, SessionPool
from app.services.oplog import OplogService

logger = logging.getLogger("nas_workbench")


class JobFailure(Exception):
    """处理器提供可公开的中文原因，不把底层异常原文写入数据库。"""
    def __init__(self, message: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True)
class JobContext:
    job: Job
    fs: NasFs | None


JobHandler = Callable[[JobContext], None]
StateCallback = Callable[[Session, Job], None]


@dataclass(frozen=True)
class Registration:
    handler: JobHandler
    concurrency: int
    retries: int
    recoverable: bool
    state_callback: StateCallback | None


AI_KINDS = frozenset({"ai_tag_batch", "ai_tag_product", "embed"})
RECOVERABLE_KINDS = AI_KINDS | {"thumb"}
DEFAULT_LIMITS = {"thumb": 2, "ingest": 1, "postop": 1, "inspection": 1, "backup": 1}


class JobQueue:
    def __init__(self, database: Database, pool: SessionPool, workers: int = 4,
                 poll_seconds: float = 0.1, retry_delay_seconds: float = 0.1,
                 auth_lock: ContextManager[object] | None = None) -> None:
        if workers <= 0 or poll_seconds <= 0 or retry_delay_seconds < 0:
            raise ValueError("任务线程数、轮询间隔必须为正数，重试延时不能为负数")
        self.database = database
        self.pool = pool
        self.workers = workers
        self.poll_seconds = poll_seconds
        self.retry_delay_seconds = retry_delay_seconds
        self._auth_lock = auth_lock or nullcontext()
        self._registry: dict[str, Registration] = {}
        self._active: Counter[str] = Counter()
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._started = False

    def register(self, kind: str, handler: JobHandler, concurrency: int | None = None,
                 retries: int | None = None, recoverable: bool | None = None,
                 state_callback: StateCallback | None = None) -> None:
        limit = concurrency if concurrency is not None else DEFAULT_LIMITS.get(kind, 3 if kind in AI_KINDS else 1)
        retries = retries if retries is not None else 3 if kind in AI_KINDS else 1
        recoverable = recoverable if recoverable is not None else kind in RECOVERABLE_KINDS
        if not kind or limit <= 0 or retries < 0:
            raise ValueError("任务名称不能为空，并发数必须为正数，重试次数不能为负数")
        with self._lock:
            if self._started:
                raise RuntimeError("启动后不能修改任务注册表")
            if kind in self._registry:
                raise ValueError("任务类型已注册")
            self._registry[kind] = Registration(handler, limit, retries, recoverable, state_callback)

    def wake(self) -> None:
        self._wake.set()

    def _changed(self, session: Session, job: Job, action: str, before: object | None,
                 actor: User | None = None, host: str = "后台任务") -> None:
        session.flush()
        registration = self._registry.get(job.kind)
        if registration is not None and registration.state_callback is not None:
            registration.state_callback(session, job)
        OplogService(session).write(actor.id if actor else job.run_as_user_id,
            "admin" if actor else "system", action, "job", str(job.id), before,
            {"kind": job.kind, "status": job.status, "attempts": job.attempts, "error": job.error},
            "失败" if job.status == "failed" else "成功", host)

    def enqueue(self, kind: str, payload: object, run_as_user_id: int | None = None,
                scheduled_at: datetime | None = None, session: Session | None = None, *, run_as_session_id: int | None = None) -> Job:
        with self._auth_lock:
            return self._enqueue(kind, payload, run_as_user_id, scheduled_at, session, run_as_session_id)

    def _enqueue(self, kind: str, payload: object, run_as_user_id: int | None,
                 scheduled_at: datetime | None, session: Session | None, run_as_session_id: int | None) -> Job:
        with self._lock:
            if kind not in self._registry:
                raise AppError("job_kind_unregistered", "该任务类型尚未开放", 422)
            if self._stop.is_set():
                raise AppError("job_queue_stopped", "任务队列已停止", 503)
        if scheduled_at is not None and scheduled_at.tzinfo is None:
            scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
        def insert(current: Session) -> Job:
            job = Job(kind=kind, payload=payload, run_as_user_id=run_as_user_id, run_as_session_id=run_as_session_id, status="queued",
                      attempts=0, scheduled_at=scheduled_at)
            current.add(job)
            self._changed(current, job, "任务入队", None)
            return job
        if session is not None:
            job = insert(session)
        else:
            with self.database.session() as current:
                job = insert(current)
        # 外部事务尚未提交时，定时轮询仍会在提交后看到任务；回滚不会留下可执行对象。
        self.wake()
        return job

    def recover(self) -> None:
        with self._auth_lock, self._lock, self.database.session() as session:
            for job in session.scalars(select(Job).where(Job.status == "running")):
                registration = self._registry.get(job.kind)
                recoverable = registration.recoverable if registration else job.kind in RECOVERABLE_KINDS
                before = {"status": job.status, "attempts": job.attempts}
                job.status = "queued" if recoverable else "failed"
                job.error = None if recoverable else "服务重启，需操作人重新登录后重试"
                job.started_at = None
                job.finished_at = None if recoverable else utcnow()
                job.scheduled_at = None
                # 崩溃未得到执行结果，不消耗这次失败重试机会。
                if recoverable:
                    job.attempts = max(job.attempts - 1, 0)
                self._changed(session, job, "启动恢复任务", before)

    def start(self) -> None:
        with self._auth_lock, self._lock:
            if self._started:
                return
            if self._stop.is_set():
                raise RuntimeError("已停止的任务队列不能重新启动")
            self.recover()
            self._started = True
            self._threads = [threading.Thread(target=self._worker, daemon=True, name=f"job-worker-{index + 1}")
                             for index in range(self.workers)]
            for thread in self._threads:
                thread.start()

    def _claim(self) -> tuple[Job, Registration] | None:
        with self._auth_lock, self._lock:
            if self._stop.is_set() or sum(self._active.values()) >= self.workers:
                return None
            kinds = [kind for kind, registration in self._registry.items()
                     if self._active[kind] < registration.concurrency]
            if not kinds:
                return None
            claimed: tuple[Job, Registration] | None = None
            with self.database.session() as session:
                # 条件更新是抢占的唯一依据；读出的候选不等于已获得执行权。
                candidates = list(session.scalars(select(Job.id).where(Job.status == "queued", Job.kind.in_(kinds),
                    or_(Job.scheduled_at.is_(None), Job.scheduled_at <= utcnow())).order_by(Job.id).limit(32)))
                for job_id in candidates:
                    now = utcnow()
                    result = session.execute(update(Job).where(Job.id == job_id, Job.status == "queued").values(
                        status="running", attempts=Job.attempts + 1, started_at=now, finished_at=None,
                        error=None, updated_at=now).execution_options(synchronize_session=False))
                    if result.rowcount != 1:
                        continue
                    job = session.get(Job, job_id)
                    self._changed(session, job, "开始执行任务", {"status": "queued", "attempts": job.attempts - 1})
                    registration = self._registry[job.kind]
                    claimed = job, registration
                    break
            if claimed is not None:
                self._active[claimed[0].kind] += 1
            return claimed

    def _pin_actor(self, job: Job) -> tuple[NasFs | None, int | None]:
        if job.run_as_user_id is None:
            return None, None
        with self.database.session() as session:
            user = session.get(User, job.run_as_user_id)
            if user is None or not user.enabled:
                raise JobFailure("操作人已停用", retryable=False)
            login = session.get(UserSession, job.run_as_session_id) if job.run_as_session_id is not None else None
            if login is None or login.user_id != user.id or login.revoked_at is not None or is_expired(login.expires_at):
                raise NasSessionExpired("缺少有效登录会话")
            session_id = login.id
        return self.pool.pin(session_id), session_id

    def run_once(self) -> bool:
        claimed = None
        session_id: int | None = None
        try:
            failure: JobFailure | None = None
            try:
                with self._auth_lock:
                    claimed = self._claim()
                    if claimed is None:
                        return False
                    job, registration = claimed
                    try:
                        fs, session_id = self._pin_actor(job)
                    except NasSessionExpired:
                        self._finish(job, registration, JobFailure("操作人会话已失效", retryable=False))
                        return True
                registration.handler(JobContext(job, fs))
            except JobFailure as error:
                failure = error
            except NasSessionExpired:
                failure = JobFailure("操作人会话已失效", retryable=False)
            except NasPermissionDenied:
                failure = JobFailure("NAS 无权限", retryable=False)
            except NasNotFound:
                failure = JobFailure("NAS 文件或目录不存在", retryable=False)
            except NasUnavailable:
                failure = JobFailure("NAS 暂时不可用，请重试")
            except Exception:
                if claimed is None:
                    raise
                failure = JobFailure("任务执行失败，请检查运行环境后重试")
            finally:
                if session_id is not None:
                    try:
                        self.pool.unpin(session_id)
                    except NasUnavailable:
                        logger.warning("任务的 NAS 会话清理失败，连接已作废")
            if claimed is not None:
                self._finish(*claimed, failure)
                return True
            return False
        finally:
            if claimed is not None:
                with self._lock:
                    self._active[claimed[0].kind] -= 1
                self.wake()

    def _finish(self, job: Job, registration: Registration, failure: JobFailure | None) -> None:
        with self._auth_lock, self._lock, self.database.session() as session:
            current = session.get(Job, job.id)
            # 只完成本次已经抢占的执行；管理员不能重试 running 任务。
            if current is not None and current.status == "running" and current.attempts == job.attempts:
                before = {"status": current.status, "attempts": current.attempts}
                current.error = failure.message if failure else None
                if failure is None:
                    current.status = "done"
                    current.finished_at = utcnow()
                elif failure.retryable and current.attempts <= registration.retries:
                    current.status = "queued"
                    current.scheduled_at = utcnow() + timedelta(seconds=self.retry_delay_seconds)
                    current.finished_at = None
                else:
                    current.status = "failed"
                    current.finished_at = utcnow()
                self._changed(session, current, "任务执行完成" if failure is None else "任务重试排队" if current.status == "queued" else "任务执行失败", before)

    def _worker(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                if self.run_once():
                    continue
            except Exception:
                # 数据库/回调异常不输出底层异常和任务载荷；running 留待重启恢复。
                logger.error("任务调度失败，请检查数据库及任务处理器")
            self._wake.wait(self.poll_seconds)

    def wait_idle(self, timeout: float = 5) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.database.session() as session:
                remaining = session.scalar(select(Job.id).where(Job.status.in_(["queued", "running"])).limit(1))
            if remaining is None:
                return True
            self.wake()
            self._stop.wait(min(self.poll_seconds, max(deadline - time.monotonic(), 0)))
        return False

    @staticmethod
    def _require_admin(session: Session, actor: User) -> None:
        current = session.get(User, actor.id)
        if current is None or not current.enabled or "admin" not in current.roles:
            raise AppError("forbidden", "当前账号没有管理任务的权限", 403)

    def list_jobs(self, actor: User) -> list[Job]:
        with self._auth_lock, self.database.session() as session:
            self._require_admin(session, actor)
            return list(session.scalars(select(Job).order_by(Job.id.desc())))

    def retry(self, job_id: int, actor: User, host: str) -> Job:
        with self._auth_lock, self._lock, self.database.session() as session:
            self._require_admin(session, actor)
            job = session.get(Job, job_id)
            if job is None:
                raise AppError("job_not_found", "任务不存在", 404)
            if job.status != "failed":
                raise AppError("job_not_failed", "只有失败任务可以重试", 409)
            if job.kind not in self._registry:
                raise AppError("job_kind_unregistered", "该任务类型尚未开放", 409)
            before = {"status": job.status, "attempts": job.attempts, "error": job.error}
            result = session.execute(update(Job).where(Job.id == job.id, Job.status == "failed").values(
                status="queued", attempts=0, error=None, scheduled_at=None, started_at=None, finished_at=None,
                updated_at=utcnow()).execution_options(synchronize_session=False))
            if result.rowcount != 1:
                raise AppError("job_not_failed", "任务已由其他操作处理", 409)
            session.refresh(job)
            self._changed(session, job, "管理员重试任务", before, actor, host)
        self.wake()
        return job

    def shutdown(self) -> None:
        self._stop.set()
        self.wake()
        for thread in self._threads:
            thread.join()
