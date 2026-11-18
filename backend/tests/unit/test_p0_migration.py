from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy.exc import IntegrityError

from app.db.database import BACKEND_DIR, Database


def legacy_db(tmp_path: Path, *, duplicates: bool = False) -> Database:
    db = Database(tmp_path)
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    with db.engine.begin() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        config.attributes["connection"] = connection
        command.upgrade(config, "0004_ai")
    with sqlite3.connect(tmp_path / "app.db") as raw:
        raw.executescript("""
          INSERT INTO users(id, created_at, updated_at, username, roles, enabled)
          VALUES (7, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'old', '["uploader"]', 1);
          INSERT INTO sessions(id, created_at, updated_at, token_hash, user_id, client_host, expires_at)
          VALUES (9, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'legacy-hash', 7, '旧设备', '2099-01-01');
          INSERT INTO batches(id, created_at, updated_at, batch_no, project_code, kind, status,
                              owner_id, uploader_id, round, temp_path, won, payload, ingest_step)
          VALUES (10, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'B10', 'H2020-0023', 'ingest', '上传中断',
                  7, 7, 1, 'B10', 1, '{"fingerprint_kinds":{"15":"quick"}}', 0);
          INSERT INTO batch_files (id, created_at, updated_at, batch_id, rel_path, size, mtime,
                                   upload_status, bytes_received, fingerprint, removed)
          VALUES (15, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 10, '单品/a.jpg', 4, 1, 'done', 4, 'legacy', 0);
          INSERT INTO batch_products (id, created_at, updated_at, batch_id, batch_file_id, kind, display_name, ai_status,
                                      tags, free_text, flags, relation_items, relation_confirmed)
          VALUES (17, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 10, 15, 'item', 'a', 'done',
                  '{"legacy":{"value":"旧属性"}}', '', '[]', '[]', 0);
          UPDATE tag_fields SET enabled = 0, label = '旧名', hint = '自定义提示' WHERE key = 'material';
          INSERT INTO tag_fields (created_at, updated_at, key, label, hint, sort_order, enabled, version)
          VALUES (CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'legacy', '旧字段', '旧提示', 11, 1, 7);
        """)
        if duplicates:
            raw.execute("""
                INSERT INTO batches(created_at, updated_at, batch_no, project_code, kind, status,
                                    owner_id, uploader_id, round, temp_path, won, payload, ingest_step)
                SELECT created_at, updated_at, 'B11', project_code, kind, status, owner_id, uploader_id,
                       round, temp_path, won, payload, ingest_step FROM batches WHERE id = 10
            """)
    return db


def test_legacy_upgrade_preserves_ids_tags_legacy_hashes_and_locks(tmp_path: Path) -> None:
    db = legacy_db(tmp_path)
    db.migrate()
    db.migrate()
    with sqlite3.connect(tmp_path / "app.db") as raw:
        assert raw.execute("SELECT id, fingerprint, fingerprint_kind FROM batch_files").fetchall() == [(15, "legacy", "quick")]
        assert raw.execute("SELECT year, last_sequence FROM h_code_sequences").fetchall() == [(2020, 23)]
        assert raw.execute("SELECT id, lock_active FROM batches").fetchall() == [(10, 1)]
        assert raw.execute("SELECT key, label, hint, enabled FROM tag_fields WHERE key IN ('legacy', 'material') ORDER BY key").fetchall() == [
            ("legacy", "旧字段", "旧提示", 0), ("material", "材质", "自定义提示", 1)]
        tag, proof = raw.execute("SELECT json_extract(tags, '$.legacy.value'), ai_success_at FROM batch_products WHERE id=17").fetchone()
        assert tag == "旧属性" and proof is None
        assert raw.execute("SELECT revoked_at FROM sessions WHERE id=9").fetchone()[0]
        assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(IntegrityError, match="标签字段"):
        with db.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE tag_fields SET label='伪造字段' WHERE key='category'")
    db.close()


def test_conflicting_legacy_lock_rolls_back_upgrade(tmp_path: Path) -> None:
    db = legacy_db(tmp_path, duplicates=True)
    with pytest.raises(RuntimeError, match="在途批次"):
        db.migrate()
    with sqlite3.connect(tmp_path / "app.db") as raw:
        assert raw.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0004_ai"
        assert raw.execute("SELECT count(*) FROM batches").fetchone()[0] == 2
        assert "revoked_at" not in {row[1] for row in raw.execute("PRAGMA table_info(sessions)")}
    db.close()


def test_h_code_sequence_is_atomic_across_database_connections(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from app.services.upload.service import UploadService

    db = legacy_db(tmp_path)
    db.migrate()

    def allocate(_: int) -> str:
        independent = Database(tmp_path)
        try:
            with independent.session() as session:
                return UploadService._next_h_code(session, 2020)
        finally:
            independent.close()

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(allocate, range(8)))
    assert sorted(results) == [f"H2020-{n:04d}" for n in range(24, 32)]
    db.close()
