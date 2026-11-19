"""应用工厂、前端静态托管及服务生命周期。"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import AsyncIterator

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
from app.services.ingest import IngestService
from app.services.links import LinkService
from app.api.health import router
from app.api.auth import router as auth_router
from app.api.admin import router as admin_router
from app.api.upload import router as upload_router
from app.services.upload.service import UploadService
from app.services.auth.service import AuthService
from app.services.admin import RoleService
from app.services.nas.factory import create_auth_backend, create_session_pool
from app.config import Settings, load_settings
from app.db.database import Database
from app.errors import error_response, register_handlers
from app.logging_config import configure_logging
from app.services.crypto import SecretCipher
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
from app.db.models import Job
from sqlalchemy.orm import Session

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(settings: Settings | None = None, static_dir: Path = STATIC_DIR,
               log_dir: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        config = settings or load_settings()
        directory = log_dir or Path(os.environ.get("NASWB_CONFIG", "config.yaml")).resolve().parent / "logs"
        handler = configure_logging(directory)
        logger = logging.getLogger("nas_workbench")
        database: Database | None = None
        pool = None
        upload = None
        jobs = None
        ai = None
        try:
            database = Database(config.paths.data_dir)
            application.state.settings = config
            application.state.cipher = SecretCipher(config.paths.data_dir)
            database.migrate()
            application.state.database = database
            pool = create_session_pool(config)
            application.state.auth = AuthService(database, pool, create_auth_backend(config), config)
            application.state.roles = RoleService(application.state.auth)
            jobs = JobQueue(database, pool, workers=config.jobs.workers, auth_lock=application.state.auth.lock)
            application.state.jobs = jobs
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
            application.state.review = ReviewService(application.state.auth, thumbs, ai)
            application.state.auth.on_close = upload.session_closed
            application.state.notify = NotifyService(application.state.auth)
            application.state.company_map = CompanyMapService(application.state.auth)
            ingest = IngestService(application.state.auth, jobs, ai, application.state.notify)
            application.state.ingest = ingest
            application.state.catalog = CatalogService(application.state.auth)
            store = VectorStore()
            with database.session() as session:
                logger.info("向量计算后端：%s", store.backend(session))
            indexes = IndexService(ai, store)
            application.state.indexes = indexes
            application.state.search = SearchService(ai, application.state.catalog, store)
            ingest.on_published = indexes.sync_after_publish
            jobs.register(INDEX_JOB, indexes.handle, concurrency=1, retries=0, recoverable=True)
            application.state.links = LinkService(application.state.auth, application.state.catalog)
            application.state.audit = AuditService(application.state.auth, application.state.review, application.state.notify, ingest)
            jobs.register("ingest", ingest.handle, concurrency=1, retries=1, recoverable=False, state_callback=ingest.on_job_state)
            jobs.start()
            ai.start()
            logger.info("工作台启动完成，模式：%s", config.mode)
            yield
        finally:
            try:
                try:
                    try:
                        try:
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
        if request.url.path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if request.headers.get("sec-fetch-site") == "cross-site" or (
                origin is not None and origin != f"{request.url.scheme}://{request.url.netloc}"
            ):
                return error_response(403, "unsafe_origin", "此操作只允许从工作台页面发起")
        return await call_next(request)
    application.include_router(router)
    application.include_router(auth_router)
    application.include_router(admin_router)
    application.include_router(upload_router)
    application.include_router(ai_settings_router)
    application.include_router(ai_runtime_router)
    application.include_router(review_router)
    application.include_router(audit_router)
    application.include_router(ingest_router)
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
