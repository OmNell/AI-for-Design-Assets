"""SQLite 连接与事务；初始化统一走 Alembic，不用 create_all 绕开迁移。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine, URL
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

BACKEND_DIR = Path(__file__).resolve().parents[2]


class Database:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(URL.create("sqlite", database=str(data_dir / "app.db")),
                                    connect_args={"check_same_thread": False})
        event.listen(self.engine, "connect", self._configure)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    @staticmethod
    def _configure(connection: sqlite3.Connection, record: object) -> None:
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    def load_vector_extension(self) -> bool:
        """尝试为之后的每个连接加载 sqlite-vec；任何一步失败都返回 False，由调用方改用后备实现。"""
        try:
            import sqlite_vec

            def load(connection: sqlite3.Connection, record: object) -> None:
                connection.enable_load_extension(True)
                try:
                    sqlite_vec.load(connection)
                finally:
                    connection.enable_load_extension(False)

            probe = sqlite3.connect(":memory:")
            try:
                load(probe, None)
                probe.execute("SELECT vec_version()").fetchone()
            finally:
                probe.close()
            event.listen(self.engine, "connect", load)
            self.engine.dispose()
            return True
        except Exception:
            return False

    def migrate(self) -> None:
        config = Config(str(BACKEND_DIR / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self.sessions.begin() as session:
            yield session

    def is_available(self) -> bool:
        try:
            with self.engine.connect() as connection:
                return connection.scalar(text("SELECT 1")) == 1
        except SQLAlchemyError:
            return False

    def close(self) -> None:
        self.engine.dispose()
