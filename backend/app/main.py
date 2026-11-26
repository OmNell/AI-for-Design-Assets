"""应用工厂、前端静态托管及服务生命周期。"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import AsyncIterator

from alembic.util.exc import CommandError
from sqlalchemy.exc import DatabaseError
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from starlette.exceptions import HTTPException

from app import __version__
from app.api.search import router as search_router
from app.services.search import SearchService
from app.services.search.indexes import INDEX_JOB, IndexService
from app.services.search.store import VectorStore
from app.api.catalog import router as catalog_router
from app.services.catalog import CatalogService
from app.api.ingest import router as ingest_router
from app.api.postops import router as postops_router
from app.api.postop_execution import router as postop_execution_router
from app.services.postops.drafts import DraftService
from app.services.postops.executor import PostopExecutor
from app.services.ingest import IngestService
from app.services.links import LinkService
from app.api.health import router
from app.api.recovery import router as recovery_router
from app.api.maintenance import router as maintenance_router
from app.services.backup import BackupService
from app.services.inspection import InspectionService
from app.services.maintenance.admin import AdminService
from app.services.maintenance.cleanup import CleanupService
from app.services.maintenance.settings import SystemService
from app.api.auth import router as auth_router
from app.api.nas_browser import router as nas_browser_router
from app.services.nas_browser import NasBrowser
from app.api.nas_settings import router as nas_settings_router
from app.services.nas_settings import NasSettingsService, activate_at_boot
from app.services.nas_settings.state import NasChangeGate
from app.api.admin import router as admin_router
from app.api.upload import router as upload_router
from app.services.upload.service import UploadService
from app.services.auth.service import AuthService
from app.services.admin import RoleService
from app.services.nas.factory import create_session_pool
from app.services.recovery.admin import APPLY_JOB, RECOVERY_JOB, RecoveryAdmin
from app.services.recovery.status import RecoveryStatus
from app.config import Settings, load_settings
from app.db.database import Database
from app.errors import error_response, register_handlers
from app.logging_config import configure_logging
from app.services.crypto import CryptoError, SecretCipher
from app.services.jobs import JobQueue
from app.services.thumbs import ThumbService
from app.services.ai.orchestrator import AiService
from app.services.ai.settings import AiSettingsService
from app.services.ai.fields import TagFieldService
from app.api.ai_settings import router as ai_settings_router
from app.api.review import router as review_router
from app.api.audit import router as audit_router
from app.services.audit import AuditService
from app.services.notify import NotifyService
from app.services.admin.company_map import CompanyMapService
from app.services.review import ReviewService
from app.api.ai_runtime import router as ai_runtime_router
from app.db.models import Job, Setting
from sqlalchemy import select
from sqlalchemy.orm import Session

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(settings: Settings | None = None, static_dir: Path = STATIC_DIR,
               log_dir: Path | None = None, *, config_file: Path | None = None) -> FastAPI:
    nas_gate = NasChangeGate()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        source = (config_file or Path(os.environ.get("NASWB_CONFIG", "config.yaml"))).resolve()
        config = settings or load_settings(source)
        log_root = source.parent.parent if source.parent.name == "configuration" else source.parent
        directory = log_dir or log_root / "logs"
        handler = configure_logging(directory)
        logger = logging.getLogger("nas_workbench")
        database: Database | None = None
        application.state.database_ready = False
        application.state.startup_message = "本地数据库不可用，请由管理员离线验证并恢复备份"
        recovery_status = None
        pool = None
        upload = None
        jobs = None
        ai = None
        system = None
        try:
            database = Database(config.paths.data_dir)
            application.state.settings = config
            try:
                database.migrate()
            except (DatabaseError, CommandError):
                logger.error("本地数据库不可读或版本不兼容，请离线验证并恢复备份")
                yield
                return
            try:
                if not (config.paths.data_dir / "secret.key").is_file():
                    with database.session() as session:
                        encrypted = session.scalar(select(Setting.id).where(Setting.encrypted.is_(True)).limit(1))
                    configured_secret = (config.nas.system_password_enc.get_secret_value() or
                                         config.ai.api_key_enc.get_secret_value())
                    if encrypted is not None or configured_secret:
                        raise CryptoError("配置密文缺少原密钥")
                application.state.cipher = SecretCipher(config.paths.data_dir)
            except CryptoError:
                application.state.startup_message = "本地密钥不可用，请由管理员核对备份中的原密钥"
                logger.error("本地密钥不可用，工作台暂缓启动业务服务")
                yield
                return
            nas_gate.unfreeze()
            config = activate_at_boot(config, source, database, application.state.cipher)
            application.state.settings = config
            application.state.database = database
            pool = create_session_pool(config)
            application.state.auth = AuthService(database, pool, config)
            application.state.nas_browser = NasBrowser(application.state.auth)
            application.state.roles = RoleService(application.state.auth)
            jobs = JobQueue(database, pool, workers=config.jobs.workers, auth_lock=application.state.auth.lock)
            application.state.jobs = jobs
            application.state.nas_settings = NasSettingsService(source, application.state.auth, application.state.cipher, jobs, nas_gate)
            system = SystemService(application.state.auth, jobs)
            application.state.system = system
            thumbs = ThumbService(application.state.auth, jobs)
            application.state.thumbs = thumbs
            application.state.ai_settings = AiSettingsService(application.state.auth, application.state.cipher)
            application.state.tag_fields = TagFieldService(application.state.auth)
            ai = AiService(application.state.auth, jobs, application.state.ai_settings, application.state.cipher, thumbs)
            application.state.ai = ai
            def thumb_changed(session: Session, job: Job) -> None:
                thumbs.on_job_state(session, job)
                ai.thumb_changed(session, job)
            jobs.register("thumb", thumbs.handle, concurrency=2, retries=1, recoverable=True, state_callback=thumb_changed)
            jobs.register("ai_tag_batch", ai.handle_batch, concurrency=1, retries=3, recoverable=True, state_callback=ai.job_changed)
            # 网络调用的总并发和重试由共享客户端控制，避免两层重试相乘。
            jobs.register("ai_tag_product", ai.handle_product, concurrency=config.jobs.workers, retries=0, recoverable=True, state_callback=ai.job_changed)
            application.state.auth.on_login = thumbs.resume_for_user
            upload = UploadService(application.state.auth, thumbs, ai)
            application.state.upload = upload
            system.upload = upload
            application.state.catalog = CatalogService(application.state.auth)
            application.state.review = ReviewService(application.state.auth, thumbs, ai, application.state.catalog)
            application.state.auth.on_close = upload.session_closed
            application.state.notify = NotifyService(application.state.auth, application.state.ai_settings)
            application.state.company_map = CompanyMapService(application.state.auth)
            ingest = IngestService(application.state.auth, jobs, ai, application.state.notify)
            application.state.ingest = ingest
            store = VectorStore()
            with database.session() as session:
                logger.info("向量计算后端：%s", store.backend(session))
            indexes = IndexService(ai, store)
            application.state.indexes = indexes
            application.state.search = SearchService(ai, application.state.catalog, store)
            def after_publication(session: Session) -> None:
                indexes.sync_after_publish(session)
                system.schedule_after_publication(session)
            ingest.on_published = after_publication
            jobs.register(INDEX_JOB, indexes.handle, concurrency=1, retries=0, recoverable=True)
            application.state.links = LinkService(application.state.auth, application.state.catalog)
            application.state.postops = DraftService(upload, application.state.review)
            executor = PostopExecutor(application.state.auth, jobs, ai, application.state.notify, indexes)
            executor.after_publication = system.schedule_after_publication
            application.state.postop_executor = executor
            jobs.register("postop", executor.handle, concurrency=1, retries=1, recoverable=False, state_callback=executor.on_job_state)
            application.state.audit = AuditService(application.state.auth, application.state.review, application.state.notify, ingest, executor)
            jobs.register("ingest", ingest.handle, concurrency=1, retries=1, recoverable=False, state_callback=ingest.on_job_state)
            inspection = InspectionService(application.state.auth)
            backup = BackupService(application.state.auth, source)
            admin = AdminService(application.state.auth, jobs, application.state.postops, application.state.catalog.media)
            application.state.maintenance = admin
            application.state.cleanup = CleanupService(application.state.auth)
            jobs.register("inspection", inspection.handle, concurrency=1, retries=1, recoverable=True, state_callback=inspection.changed)
            jobs.register("backup", backup.handle, concurrency=1, retries=1, recoverable=True,
                          state_callback=system.backup_job_changed)
            jobs.register("catalog_thumb", admin.rebuild_worker, concurrency=1, retries=0, recoverable=False)
            recovery_status = RecoveryStatus(config, database)
            recovery_admin = RecoveryAdmin(recovery_status, ai, indexes, jobs, system)
            application.state.recovery_status = recovery_status
            application.state.recovery_admin = recovery_admin
            jobs.register(RECOVERY_JOB, recovery_admin.handle, concurrency=1, retries=0, recoverable=False)
            jobs.register(APPLY_JOB, recovery_admin.handle, concurrency=1, retries=0, recoverable=True, exclusive=True)
            jobs.start()
            ai.start()
            system.start()
            recovery_status.start()
            application.state.database_ready = True
            logger.info("工作台启动完成，模式：%s", config.mode)
            yield
        finally:
            application.state.database_ready = False
            if recovery_status is not None:
                recovery_status.shutdown()
            try:
                try:
                    try:
                        try:
                            if system is not None:
                                system.shutdown()
                            if ai is not None:
                                ai.shutdown()
                        finally:
                            try:
                                if jobs is not None:
                                    jobs.shutdown()
                            finally:
                                if ai is not None:
                                    ai.close_clients()
                    finally:
                        if upload is not None:
                            upload.shutdown()
                finally:
                    if pool is not None:
                        pool.shutdown()
            finally:
                if database is not None:
                    database.close()
                logger.info("工作台已停止")
                logger.removeHandler(handler)
                handler.close()

    application = FastAPI(title="NAS 设计资产工作台", version=__version__, lifespan=lifespan)
    register_handlers(application)

    @application.middleware("http")
    async def protect_api_writes(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.url.path.startswith(("/api/", "/l/")) and not request.app.state.database_ready:
            return error_response(503, "database_unavailable", request.app.state.startup_message)
        if request.url.path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if request.headers.get("sec-fetch-site") == "cross-site" or (
                origin is not None and origin != f"{request.url.scheme}://{request.url.netloc}"
            ):
                return error_response(403, "unsafe_origin", "此操作只允许从工作台页面发起")
        return await call_next(request)
    class GateMiddleware:
        def __init__(self, app) -> None:
            self.app = app

        async def __call__(self, scope, receive, send) -> None:
            await nas_gate(self.app, scope, receive, send)

    application.add_middleware(GateMiddleware)
    application.include_router(router)
    application.include_router(nas_settings_router)
    application.include_router(nas_browser_router)
    application.include_router(auth_router)
    application.include_router(admin_router)
    application.include_router(maintenance_router)
    application.include_router(recovery_router)
    application.include_router(upload_router)
    application.include_router(ai_settings_router)
    application.include_router(ai_runtime_router)
    application.include_router(review_router)
    application.include_router(audit_router)
    application.include_router(ingest_router)
    application.include_router(postops_router)
    application.include_router(postop_execution_router)
    application.include_router(catalog_router)
    application.include_router(search_router)
    static_root = static_dir.resolve()
    application.state.static_index = static_root / "index.html"

    @application.get("/{path:path}", include_in_schema=False)
    def frontend(path: str) -> FileResponse:
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404)
        target = (static_root / path).resolve()
        if not target.is_relative_to(static_root):
            raise HTTPException(404)
        if target.is_file():
            return FileResponse(target)
        if path.startswith("assets/"):
            raise HTTPException(404)
        index = static_root / "index.html"
        if not index.is_file():
            raise HTTPException(404)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return application


app = create_app()
