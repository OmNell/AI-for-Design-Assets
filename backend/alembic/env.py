"""命令行与应用启动共享迁移入口。"""
from __future__ import annotations

from alembic import context
from sqlalchemy.engine import Connection

from app.config import load_settings
from app.db.database import Database
from app.db.models import Base


def run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


provided = context.config.attributes.get("connection")
if context.is_offline_mode():
    raise RuntimeError("仅支持在线 SQLite 迁移，请直接执行 alembic upgrade head")
elif provided is not None:
    run(provided)
else:
    database = Database(load_settings().paths.data_dir)
    try:
        with database.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            run(connection)
    finally:
        database.close()
