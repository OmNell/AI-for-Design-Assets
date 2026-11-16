"""应用工厂、前端静态托管及服务生命周期。"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import FileResponse
from starlette.exceptions import HTTPException

from app import __version__
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
from app.errors import register_handlers
from app.logging_config import configure_logging
from app.services.crypto import SecretCipher
from app.services.jobs import JobQueue
from app.services.thumbs import ThumbService

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
            jobs.register("thumb", thumbs.handle, concurrency=2, retries=1, recoverable=True, state_callback=thumbs.on_job_state)
            application.state.auth.on_login = thumbs.resume_for_user
            upload = UploadService(application.state.auth, thumbs)
            application.state.upload = upload
            jobs.start()
            logger.info("工作台启动完成，模式：%s", config.mode)
            yield
        finally:
            try:
                try:
                    try:
                        if jobs is not None:
                            jobs.shutdown()
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
    application.include_router(router)
    application.include_router(auth_router)
    application.include_router(admin_router)
    application.include_router(upload_router)
    static_root = static_dir.resolve()

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
