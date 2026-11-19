from __future__ import annotations

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.db.database import Database
from app.db.models import User, Oplog, Job, UserSession, Setting, utcnow
from app.services.oplog import OplogService


def test_schema_and_sqlite_pragmas(database: Database) -> None:
    assert set(inspect(database.engine).get_table_names()) == {"users", "sessions", "settings", "oplog", "jobs", "projects", "batches", "batch_files", "preflight_acks", "alembic_version", "tag_fields", "batch_products", "company_industry_map", "ai_calls", "ai_alerts", "h_code_sequences", "review_snapshots", "industries", "review_rounds", "review_pins", "files", "products", "product_tags", "product_relations", "history_versions", "links", "ingest_plans", "product_embeddings", "inspection_findings", "access_logs"}
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


def test_project_locks_are_enforced_by_sqlite_for_all_batch_kinds(database: Database) -> None:
    from app.db.models import Batch, Project

    with database.session() as session:
        user = User(username="测试账号")
        project = Project(project_code="P1", company_name="企业", won=True, project_name="项目", folder_name="P1_企业_项目")
        session.add_all([user, project]); session.flush()

    def add(batch_no: str, kind: str, status: str, project_id: int | None = None) -> int:
        with database.session() as session:
            batch = Batch(batch_no=batch_no, project_code="P1", project_id=project_id, kind=kind, status=status,
                          owner_id=user.id, uploader_id=user.id, temp_path="", won=True, payload={})
            session.add(batch); session.flush()
            return batch.id

    ingest_id = add("first", "ingest", "入库异常")
    with pytest.raises(IntegrityError, match="op_lock_key"):
        add("duplicate", "ingest", "上传中")
    operation_id = add("operation", "retag", "入库异常", project.id)
    with pytest.raises(IntegrityError, match="op_lock_key"):
        add("second", "supplement", "上传中", project.id)
    with pytest.raises(IntegrityError, match="CHECK constraint"):
        add("invalid", "move", "待初审")
    with database.session() as session:
        session.get(Batch, ingest_id).status = "已终止"
        session.get(Batch, operation_id).status = "已入库"
    assert add("new-ingest", "ingest", "上传中") != ingest_id
    assert add("new-operation", "move", "待初审", project.id) != operation_id
