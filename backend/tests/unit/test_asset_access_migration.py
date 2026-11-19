"""使用 WP09 数据库验证 WP10 迁移保留正式资产身份。"""
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from app.db.database import BACKEND_DIR, Database
from app.db.models import Link, Project


def test_upgrade_keeps_wp09_project_file_product_and_link_ids(tmp_path: Path) -> None:
    database = Database(tmp_path)
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    try:
        with database.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            config.attributes["connection"] = connection
            command.upgrade(config, "0016_catalog")
            connection.execute(text("""INSERT INTO projects
                (id, project_code, code_source, company_name, won, project_name, folder_name, current_path, status, created_at, updated_at)
                VALUES (71, 'P71', 'company', '企业', 1, '项目', 'P71_企业_项目', '路径', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"""))
            connection.execute(text("""INSERT INTO files
                (id, project_id, rel_path, size, mtime, fingerprint, fingerprint_kind, category, status, created_at, updated_at)
                VALUES (72, 71, '单品/杯.jpg', 0, 0, :sha, 'full', '单品', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"""),
                               {"sha": "a" * 64})
            connection.execute(text("""INSERT INTO products
                (id, product_code, project_id, kind, display_name, file_id, archive_rel_path, status, free_text,
                 vision_model, ingested_at, created_at, updated_at)
                VALUES (73, 'P71-001', 71, 'item', '杯', 72, '档案', 'active', '', 'fake-vision',
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"""))
            connection.execute(text("""INSERT INTO links (id, kind, target_id, link_id, created_at, updated_at)
                VALUES (74, 'product', 73, 'same_short_id', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"""))
        database.migrate()
        with database.session() as session:
            assert session.get(Project, 71).project_code == "P71"
            assert session.execute(text("SELECT id, physical_rel_path FROM files")).one() == (72, None)
            assert session.execute(text("SELECT id, file_id FROM products")).one() == (73, 72)
            assert session.execute(select(Link.id, Link.target_id, Link.link_id)).one() == (74, 73, "same_short_id")
            assert list(session.execute(text("PRAGMA foreign_key_check"))) == []
    finally:
        database.close()
