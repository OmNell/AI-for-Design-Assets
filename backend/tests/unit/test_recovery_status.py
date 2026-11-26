from contextlib import closing
import json
from threading import Event

import pytest

from app.config import DevUser

from app.db.database import Database
from app.db.models import Project
from app.services.archive import PROJECT_FILE
from app.services.archive.publication import RECEIPT
from app.services.nas import NasPermissionDenied, Region
from app.services.nas.factory import create_fs, create_paths, create_system_fs
from app.services.recovery.status import RecoveryStatus, sample_formal


def test_startup_formal_sample_detects_empty_and_missing_version(settings, tmp_path):
    settings.paths.dev_nas_dir = tmp_path / "simulated-nas"
    database = Database(settings.paths.data_dir)
    database.migrate()
    status = RecoveryStatus(settings, database)
    assert status.local()["catalog_state"] == "empty_unconfirmed"
    fs = create_system_fs(settings)
    root = create_paths(settings).root(Region.FORMAL)
    status.sample = sample_formal(fs, root, Event())
    assert status.local()["catalog_state"] == "empty_confirmed"

    relative = "中标/行业/企业/P1_企业_项目"
    directory = settings.paths.dev_nas_dir / root / relative
    (directory / RECEIPT).parent.mkdir(parents=True)
    (directory / PROJECT_FILE).touch()
    (directory / RECEIPT).write_text(json.dumps({
        "format": 1, "root": relative, "project_id": 12, "project_code": "P1", "asset_version": 3,
    }), encoding="utf-8")
    assert sample_formal(fs, root, Event(), limit=1).state == "unconfirmed"
    status.sample = sample_formal(fs, root, Event())
    assert status.local()["catalog_state"] == "needs_scan"
    with database.session() as session:
        session.add(Project(id=12, project_code="P1", asset_version=2, won=True,
                            project_name="项目", company_name="企业", folder_name="P1_企业_项目"))
    assert status.local()["catalog_state"] == "needs_scan"
    with database.session() as session:
        session.get(Project, 12).asset_version = 3
    assert status.local()["catalog_state"] == "local_present"
    fs.close()
    database.close()


def test_read_only_recovery_preserves_reads_and_blocks_both_identities_from_writing(settings, tmp_path):
    settings.paths.dev_nas_dir = tmp_path / "simulated-nas"
    account = DevUser(username="local-writer", password="fake-password", permissions={"temp": "write"})
    settings.dev_users = [account]
    settings.recovery.read_only = True
    paths = create_paths(settings)
    with (
        closing(create_system_fs(settings)) as system,
        closing(create_fs(settings, account.username, account.password.get_secret_value())) as user,
    ):
        assert system.list(paths.formal) == []
        assert system.list(paths.backup) == []
        with pytest.raises(NasPermissionDenied):
            system.open_write(paths.join(Region.BACKUP, "测试.txt"))
        with pytest.raises(NasPermissionDenied):
            user.mkdir(paths.join(Region.TEMP, "测试"))
