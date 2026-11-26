"""验证行业目录边界、别名匹配和超期提醒计算。"""
from datetime import datetime
from pathlib import Path

from app.db.database import Database
from app.db.models import CompanyIndustryMap
from app.services.admin.company_map import company_path, match_company
from app.services.notify import review_overdue, LOCAL


def test_casefolded_alias_and_paths(database: Database) -> None:
    with database.session() as session:
        row = CompanyIndustryMap(company_name="甲公司", aliases=["Alpha"], industry_name="设计", company_seq=3, industry_seq=2)
        session.add(row)
        assert match_company(session, "ALPHA") == row
        assert company_path(False, row) == "02未中标项目/02设计/03甲公司"
        assert match_company(session, "甲") is None


def test_overdue_excludes_weekends_and_partial_days() -> None:
    friday = datetime(2026, 11, 13, 16, tzinfo=LOCAL)
    assert not review_overdue(friday, datetime(2026, 11, 15, 18, tzinfo=LOCAL), 1)
    assert not review_overdue(friday, datetime(2026, 11, 16, 16, tzinfo=LOCAL), 1)
    assert review_overdue(friday, datetime(2026, 11, 16, 16, 1, tzinfo=LOCAL), 1)


def test_wp07_upgrade_keeps_snapshot_and_existing_directory_numbers(tmp_path: Path) -> None:
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import select, text
    from app.db.database import BACKEND_DIR
    from app.db.models import Batch, Industry, ReviewSnapshot, User

    db = Database(tmp_path)
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    try:
        with db.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            config.attributes["connection"] = connection
            command.upgrade(config, "0014_review_snapshots")
        with db.session() as session:
            owner = User(username="原上传人")
            session.add(owner)
            session.flush()
            batch = Batch(batch_no="B1", project_code="P1", kind="ingest", status="待初审", owner_id=owner.id,
                          uploader_id=owner.id, temp_path="B1", won=True, review_snapshot_hash="a" * 64)
            session.add(batch)
            session.flush()
            snapshot = ReviewSnapshot(batch_id=batch.id, round=1, submitted_by=owner.id, digest=batch.review_snapshot_hash,
                                      content={"files": [{"fingerprint": "b" * 64}]})
            session.add_all([snapshot, CompanyIndustryMap(company_name="旧企业", industry_seq=5,
                            industry_name="旧行业", company_seq=9, aliases=["原别名"])])
        db.migrate()
        with db.session() as session:
            assert session.get(ReviewSnapshot, snapshot.id).content == snapshot.content
            assert session.get(Batch, batch.id).review_snapshot_hash == snapshot.digest
            assert session.get(Batch, batch.id).payload["stage_since"]
            assert session.scalar(select(Industry)).last_company_seq == 9
            assert match_company(session, "原别名").company_seq == 9
            assert list(session.execute(text("PRAGMA foreign_key_check"))) == []
    finally:
        db.close()
