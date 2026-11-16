from __future__ import annotations

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.db.database import Database
from app.db.models import User, Oplog, Job, UserSession, Setting, utcnow
from app.services.oplog import OplogService


def test_schema_and_sqlite_pragmas(database: Database) -> None:
    assert set(inspect(database.engine).get_table_names()) == {"users", "sessions", "settings", "oplog", "jobs", "projects", "batches", "batch_files", "preflight_acks", "alembic_version", "tag_fields", "batch_products", "company_industry_map", "ai_calls", "ai_alerts", "industries", "review_rounds", "review_pins"}
    with database.engine.connect() as connection:
        assert connection.scalar(text("PRAGMA journal_mode")) == "wal"
        assert connection.scalar(text("PRAGMA busy_timeout")) == 5000
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1
    database.migrate()
    assert database.is_available()


def test_oplog_insert_and_database_triggers(database: Database) -> None:
    with database.session() as session:
        user = User(username="测试用户", roles=["admin"])
        session.add(user)
        session.flush()
        entry = OplogService(session).write(user.id, "admin", "测试动作", "测试对象", "1", None,
                                           {"中文": True}, "成功", "127.0.0.1")
        entry_id = entry.id
    with database.session() as session:
        assert session.get(Oplog, entry_id).after == {"中文": True}
    # 测试专门尝试直接 SQL，验证不能绕过服务层；业务代码不包含更新/删除日志。
    for statement, message in (("UPDATE oplog SET result='篡改' WHERE id=:id", "禁止修改"),
                               ("DELETE FROM oplog WHERE id=:id", "禁止删除")):
        with pytest.raises(IntegrityError, match=message):
            with database.engine.begin() as connection:
                connection.execute(text(statement), {"id": entry_id})
    with database.session() as session:
        assert session.get(Oplog, entry_id).result == "成功"


def test_oplog_joins_caller_transaction(database: Database) -> None:
    with pytest.raises(RuntimeError):
        with database.session() as session:
            OplogService(session).write(None, "system", "测试", "测试", None, {}, {}, "成功", "本机")
            raise RuntimeError("模拟业务失败")
    with database.session() as session:
        assert session.scalar(select(Oplog)) is None


def test_models_roundtrip_and_foreign_key_enforced(database: Database) -> None:
    with database.session() as session:
        user = User(username="演示用户")
        session.add(user)
        session.flush()
        session.add_all([
            UserSession(token_hash="hash", user_id=user.id, client_host="本机", expires_at=utcnow()),
            Job(kind="thumb", payload={"id": 1}, run_as_user_id=user.id),
            Setting(key="演示配置", value={"enabled": True}, encrypted=False),
        ])
    with database.session() as session:
        assert session.scalar(select(Job)).status == "queued"
        assert session.scalar(select(UserSession)).token_hash == "hash"
        assert session.scalar(select(Setting)).value == {"enabled": True}
    with pytest.raises(IntegrityError):
        with database.session() as session:
            session.add(UserSession(token_hash="bad", user_id=9999, client_host="本机", expires_at=utcnow()))
