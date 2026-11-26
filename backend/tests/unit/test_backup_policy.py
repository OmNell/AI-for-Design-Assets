from datetime import datetime, timedelta, timezone
from zipfile import ZipFile

import pytest
from sqlalchemy import select

from app import __version__
from app.config import Settings
from app.db.database import Database
from app.db.models import BackupRecord, Job, Project, Setting, User, utcnow
from app.errors import AppError
from app.services.auth.service import AuthService
from app.services.backup import BackupService
from app.services.backup.format import read_manifest
from app.services.backup.recovery import stage_backup
from app.services.crypto import SecretCipher
from app.services.jobs import JobContext, JobFailure, JobQueue
from app.services.maintenance import settings as policy
from app.services.nas.factory import create_session_pool


@pytest.fixture
def production(tmp_path):
    config = Settings(mode="prod", paths={"data_dir": tmp_path / "data"})
    config_path = tmp_path / "config.yaml"
    config_path.write_text("mode: prod\n", encoding="utf-8")
    database = Database(config.paths.data_dir)
    database.migrate()
    SecretCipher(config.paths.data_dir)
    pool = create_session_pool(config)
    auth = AuthService(database, pool, config)
    queue = JobQueue(database, pool, auth_lock=auth.lock)
    backup = BackupService(auth, config_path)
    queue.register("backup", backup.handle)
    try:
        yield policy.SystemService(auth, queue), backup
    finally:
        pool.shutdown()
        database.close()


def project(code, *, published):
    return Project(project_code=code, company_name="测试企业", won=True, project_name="测试项目", folder_name=code,
                   status="active" if published else "ingesting", ingested_at=utcnow() if published else None)


def test_production_backup_requires_verified_state_and_formal_assets(production):
    system, backup = production
    database = system.auth.database
    with database.session() as session:
        actor = User(username="admin", roles=["admin"])
        session.add(actor)
        session.add(project("未发布", published=False))
    assert system.trigger("backup", slot="2031-02-03 03:00") is None
    with pytest.raises(AppError) as exc:
        system.trigger("backup", actor)
    assert exc.value.code == "backup_unconfirmed"
    with database.session() as session:
        session.add(Setting(key=policy.ASSET_RECOVERY_CONFIRMED, value=True, encrypted=False))
    assert system.trigger("backup", slot="2031-02-03 03:00") is None
    with database.session() as session:
        session.add(project("已发布", published=True))
    job = system.trigger("backup", slot="2031-02-03 03:00")
    assert job is not None and job.kind == "backup"
    system.auth.config.recovery.read_only = True
    with pytest.raises(JobFailure, match="只读模式"):
        backup.handle(JobContext(job, None))
    assert not backup.directory.exists()


def test_publish_backup_is_persistent_deferred_and_coalesced(production, monkeypatch):
    system, _ = production
    database = system.auth.database
    now = datetime(2031, 2, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(policy, "utcnow", lambda: now)
    system.auth.config.recovery.backup_on_publication = True
    system.auth.config.recovery.backup_delay_minutes = 15
    with database.session() as session:
        session.add(project("已发布", published=True))
        system.schedule_after_publication(session)
        assert session.scalar(select(Job)) is None
        session.add(Setting(key=policy.ASSET_RECOVERY_CONFIRMED, value=True, encrypted=False))
        system.schedule_after_publication(session)
        system.schedule_after_publication(session)
    with database.session() as session:
        jobs = list(session.scalars(select(Job)))
        assert len(jobs) == 1
        assert jobs[0].scheduled_at.replace(tzinfo=timezone.utc) == now + timedelta(minutes=15)
        assert jobs[0].payload["source"] == "publication"
        jobs[0].status = "running"
        system.schedule_after_publication(session)
    with database.session() as session:
        running = session.scalar(select(Job))
        assert running.status == "running"
        assert session.scalar(select(Setting.value).where(Setting.key == policy.PUBLICATION_BACKUP_PENDING)) is True
        running.status = "done"
        system.backup_job_changed(session, running)
    with database.session() as session:
        assert [job.status for job in session.scalars(select(Job).order_by(Job.id))] == ["done", "queued"]
        assert session.scalar(select(Setting.value).where(Setting.key == policy.PUBLICATION_BACKUP_PENDING)) is None


def test_existing_verified_local_backup_can_establish_baseline(production):
    system, backup = production
    database = system.auth.database
    with database.session() as session:
        session.add(project("归档项目", published=True))
    backup.directory.mkdir()
    filename = backup.directory / "wb-backup-20310201-0200.zip"
    size, digest = backup._package(filename)
    with ZipFile(filename) as archive:
        assert read_manifest(archive)["app_version"] == __version__
    with database.session() as session:
        job = Job(kind="backup", status="done", payload={})
        session.add(job)
        session.flush()
        session.add(BackupRecord(job_id=job.id, name=filename.name, size=size, fingerprint=digest, verified_at=utcnow()))
        assert system.backup_allowed(session)
        marker = Setting(key=policy.ASSET_RECOVERY_CONFIRMED, value=False, encrypted=False)
        session.add(marker)
        assert not system.backup_allowed(session)
        session.delete(marker)
    filename.write_bytes(b"changed")
    with database.session() as session:
        assert not system.backup_allowed(session)


def test_staging_requires_destination_to_verify_recovered_catalog(production, tmp_path):
    system, backup = production
    with system.auth.database.session() as session:
        session.add(project("已核验", published=True))
        session.add(Setting(key=policy.ASSET_RECOVERY_CONFIRMED, value=True, encrypted=False))
    backup.directory.mkdir()
    filename = backup.directory / "wb-backup-20310201-0200.zip"
    backup._package(filename)
    target = tmp_path / "recovery"
    stage_backup(filename, target)
    restored = Database(target / "data")
    try:
        with restored.session() as session:
            assert session.scalar(select(Setting.value).where(Setting.key == policy.ASSET_RECOVERY_CONFIRMED)) is False
    finally:
        restored.close()
