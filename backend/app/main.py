"""应用工厂与前端静态托管，不包含登录或 NAS 业务。"""
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
from app.config import Settings, load_settings
from app.db.database import Database
from app.errors import register_handlers
from app.logging_config import configure_logging
from app.services.crypto import SecretCipher

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
        try:
            database = Database(config.paths.data_dir)
            application.state.settings = config
            application.state.cipher = SecretCipher(config.paths.data_dir)
            database.migrate()
            application.state.database = database
            logger.info("工作台启动完成，模式：%s", config.mode)
            yield
        finally:
            if database is not None:
                database.close()
            logger.info("工作台已停止")
            logger.removeHandler(handler)
            handler.close()

    application = FastAPI(title="NAS 设计资产工作台", version=__version__, lifespan=lifespan)
    register_handlers(application)
    application.include_router(router)
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
