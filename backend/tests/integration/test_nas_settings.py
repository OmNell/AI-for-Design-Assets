"""只在隔离 SQLite、LocalFs 和 HTTP 模拟传输中验证 NAS 设置。"""
from datetime import datetime, timezone
from hashlib import sha256

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import select
import yaml

from app.config import NasConfig
from app.db.models import Batch, CatalogFile, Job, Link, Oplog, Project
from app.main import create_app
from app.services.auth.backends import AuthUnavailable, DsmAuthBackend
from app.services.nas import NasPermissionDenied, Region
from app.services.nas.factory import create_paths
from app.services.nas_settings import probe

SECRET = "Smb-Secret-Only-In-Request"
ADMIN_PASSWORD = "demo-nas-settings"
BASE = "/api/admin/nas"


@pytest.fixture
def instance(tmp_path):
    source = tmp_path / "config.yaml"
    source.write_text(yaml.safe_dump({
        "mode": "dev", "bootstrap_admin": "admin",
        "nas": {"host": "192.168.1.10", "share": "工作台共享"},
        "paths": {"data_dir": "data", "dev_nas_dir": "nas"},
        "dev_users": [
            {"username": "admin", "password": ADMIN_PASSWORD, "permissions": {"formal": "write", "temp": "write"}},
            {"username": "employee", "password": "employee-dev", "permissions": {"formal": "read", "temp": "write"}},
        ]}, allow_unicode=True), encoding="utf-8")
    root = tmp_path / "nas" / "部门" / "工作台"
    for name in probe.REGION_NAMES.values():
        (root / name).mkdir(parents=True)
    (root / "目录链接").symlink_to(tmp_path, target_is_directory=True)
    (root / "资料.txt").write_text("不能被目录浏览读取")
    return source, root


def login(client, name="admin"):
    result = client.post("/api/auth/login", json={"username": name, "password": ADMIN_PASSWORD if name == "admin" else "employee-dev"})
    assert result.status_code == 200, result.text
    return result.json()


def candidate(**changes):
    return {"host": "192.168.1.10", "dsm_port": 5001, "share": "工作台共享",
            "work_root": "部门/工作台", "system_user": "wb-system",
            "actor_password": ADMIN_PASSWORD, "system_password": SECRET, **changes}


def submit(client, **changes):
    stamp = client.get(BASE + "/config").json()["revision"]
    return client.post(BASE + "/apply", json={**candidate(), "revision": stamp, "confirmed": True, **changes})


def test_roles_redaction_and_browse_boundaries(instance):
    config, root = instance
    with TestClient(create_app(config_file=config)) as client:
        assert client.get(BASE + "/config").status_code == 401
        login(client, "employee")
        assert client.get(BASE + "/status").status_code == 403
        assert client.post(BASE + "/apply", json={}).status_code == 403
        login(client)
        active = client.get(BASE + "/config").json()
        assert active["active"]["work_root"] is None and "system_password_enc" not in str(active)

        args = candidate()
        target = {key: args[key] for key in ("host", "share", "dsm_port", "actor_password")}
        result = client.post(BASE + "/browse", json={**target, "path": "部门/工作台"})
        assert result.status_code == 200
        assert set(result.json()["nodes"]) == {"设计资产", "工作台临时区"}
        for invalid in ("部门/../其他", "/本机", r"\\host\share", "部门/目录:流"):
            assert client.post(BASE + "/browse", json={**target, "path": invalid}).status_code in (403, 422)
        assert client.post(BASE + "/browse", json={**target, "path": "部门/工作台/目录链接"}).status_code == 403
        assert client.post(BASE + "/browse", json={**target, "share": "其他共享"}).status_code == 404
        assert client.post(BASE + "/browse", json={**target, "path": "不存在"}).status_code == 404
        assert client.post(BASE + "/browse", json={**target, "actor_password": "wrong"}).status_code == 403
        assert client.post(BASE + "/browse", json={**target, "host": "127.0.0.1"}).status_code == 422
        assert client.post(BASE + "/validate", json=candidate(), headers={"origin": "http://outside.example"}).status_code == 403


def test_connection_failures_and_permission_profiles(instance):
    config, root = instance
    with TestClient(create_app(config_file=config)) as client:
        actor = login(client)
        body = {"host": "192.168.1.10", "share": "工作台共享", "actor_password": "incorrect", "protocol": "dsm"}
        assert client.post(BASE + "/test", json=body).json()[0]["state"] == "failed"
        body["protocol"] = "smb"
        assert client.post(BASE + "/test", json=body).json()[0]["state"] == "failed"
        body.update(username="employee", actor_password="employee-dev", permission_profile="employee", work_root="部门/工作台")
        assert [row["state"] for row in client.post(BASE + "/test", json=body).json()] == ["passed", "manual"]
        body["permission_profile"] = "final"
        assert client.post(BASE + "/test", json=body).json()[0]["state"] == "failed"
        checks = client.post(BASE + "/validate", json=candidate()).json()
        assert checks["eligible"]
        assert all(row["state"] == "passed" for row in checks["checks"] if row["name"].startswith("系统账号"))
        assert any(row["state"] == "manual" for row in checks["checks"])
        assert client.post(BASE + "/validate", json=candidate(work_root="不存在")).json()["eligible"] is False
        with client.app.state.database.session() as session:
            audit = " ".join(str(r.after) for r in session.scalars(select(Oplog).where(Oplog.user_id == actor["id"])))
            assert ADMIN_PASSWORD not in audit and SECRET not in audit


def test_new_root_is_pending_until_restart_and_enforces_three_regions(instance):
    config, root = instance
    previous = config.read_bytes()
    with TestClient(create_app(config_file=config)) as client:
        login(client)
        assert submit(client, revision="0" * 64).status_code == 409
        assert config.read_bytes() == previous
        response = submit(client)
        assert response.status_code == 200, response.text
        state = response.json()
        assert state["state"] == "pending_restart" and state["active"]["work_root"] is None
        assert state["pending"]["regions"]["formal"] == "/部门/工作台/设计资产"
        assert SECRET not in response.text and ADMIN_PASSWORD not in response.text
        assert SECRET not in config.read_text()
        assert (config.parent/"data/nas-settings-previous.yaml").read_bytes() == previous
        cipher = client.app.state.cipher
        assert cipher.decrypt(yaml.safe_load(config.read_text())["nas"]["system_password_enc"]) == SECRET
        assert client.get("/api/projects").json()["code"] == "nas_restart_pending"
        assert client.post(BASE + "/apply", json={}).status_code == 503
        assert create_paths(client.app.state.settings).formal == "设计资产"
    with TestClient(create_app(config_file=config)) as client:
        login(client)
        assert client.get(BASE + "/status").json()["state"] == "active"
        fs = client.app.state.auth.pool.system()
        paths = create_paths(client.app.state.settings)
        assert paths.formal == "部门/工作台/设计资产"
        assert fs.list(paths.formal) == []
        for path in ("设计资产", paths.temp):
            with pytest.raises(NasPermissionDenied):
                fs.list(path)
        for path in ("设计资产/越界", paths.formal + "/系统越权"):
            with pytest.raises(NasPermissionDenied):
                fs.open_write(path)
        with fs.open_write(paths.join(Region.BACKUP, "模拟备份")) as stream:
            stream.write(b"backup")
        assert (root / "_工作台备份" / "模拟备份").read_bytes() == b"backup"
        old_secret = yaml.safe_load(config.read_text())["nas"]["system_password_enc"]
        payload = candidate(dsm_port=5002)
        payload.pop("system_password")
        assert client.post(BASE + "/apply", json={**payload, "revision": client.get(BASE + "/config").json()["revision"],
                                                "confirmed": True}).status_code == 200
        assert yaml.safe_load(config.read_text())["nas"]["system_password_enc"] == old_secret


def test_failed_activation_recovers_old_yaml(instance):
    config, root = instance
    original = config.read_bytes()
    with TestClient(create_app(config_file=config)) as client:
        login(client)
        assert submit(client).status_code == 200
    (root / "_工作台备份").rmdir()
    with TestClient(create_app(config_file=config)) as client:
        login(client)
        assert client.get(BASE + "/status").json()["state"] == "failed"
        assert config.read_bytes() == original
        assert client.get("/api/projects").status_code == 200


def test_existing_asset_prevents_root_move_but_same_nas_ip_preserves_link(instance):
    config, root = instance
    doc = yaml.safe_load(config.read_text())
    for key, name in probe.REGION_NAMES.items():
        doc["nas"][{"formal": "root_path", "temp": "temp_path", "backup": "backup_path"}[key]] = f"/部门/工作台/{name}"
    doc["nas"]["work_root"] = "部门/工作台"
    config.write_text(yaml.safe_dump(doc, allow_unicode=True))
    directory = root / "设计资产" / "P1"
    directory.mkdir()
    (directory / "样本.txt").write_bytes(b"file")
    with TestClient(create_app(config_file=config)) as client:
        login(client)
        with client.app.state.database.session() as session:
            project = Project(project_code="P001", company_name="企业", won=True, project_name="样本",
                              folder_name="P1", current_path="P1", status="active", ingested_at=datetime.now(timezone.utc))
            session.add(project)
            session.flush()
            project_id = project.id
            session.add(CatalogFile(project_id=project_id, rel_path="样本.txt", size=4, mtime=0,
                                    fingerprint=sha256(b"file").hexdigest(), category="other"))
            session.add(Link(kind="project", target_id=project_id, link_id="unchanged-short-id"))
        bad = client.post(BASE + "/validate", json=candidate(work_root="另一个根")).json()
        assert not bad["eligible"] and "独立资产迁移" in bad["conflicts"][0]
        assert submit(client, host="192.168.1.11").status_code == 200
    with TestClient(create_app(config_file=config)) as client:
        login(client)
        assert client.get(BASE + "/status").json()["active"]["host"] == "192.168.1.11"
        with client.app.state.database.session() as session:
            assert session.scalar(select(Link).where(Link.link_id == "unchanged-short-id")).target_id == project_id


@pytest.mark.parametrize("status", ["上传中", "待终审", "入库异常"])
def test_inflight_work_blocks_config(instance, status):
    config, _ = instance
    with TestClient(create_app(config_file=config)) as client:
        actor = login(client)
        with client.app.state.database.session() as session:
            session.add(Batch(batch_no="B001", project_code="P001", kind="ingest", status=status, owner_id=actor["id"],
                              uploader_id=actor["id"], temp_path="工作台临时区/B001", won=True))
            session.add(Job(kind="inspection", status="queued", payload={}, scheduled_at=datetime(2090,1,1,tzinfo=timezone.utc)))
        before = config.read_bytes()
        blocked = client.post(BASE + "/validate", json=candidate()).json()
        assert not blocked["eligible"] and any(status in row for row in blocked["conflicts"])
        assert any("inspection" in row for row in blocked["conflicts"])
        assert submit(client).status_code == 409 and config.read_bytes() == before


def test_dsm_redirect_is_blocked_and_certificate_name_is_retained():
    seen = []
    def upstream(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://outside.example/test"})
    backend = DsmAuthBackend(NasConfig(host="nas.internal", verified_address="192.168.1.10"),
                             httpx.MockTransport(upstream))
    with pytest.raises(AuthUnavailable, match="重定向"):
        backend.discover()
    assert len(seen) == 1 and seen[0].url.host == "192.168.1.10"
    assert seen[0].extensions["sni_hostname"] == "nas.internal"
    assert seen[0].headers["host"] == "nas.internal:5001"


def test_concurrent_request_blocks_change(instance):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    config, _ = instance
    before = config.read_bytes()
    app = create_app(config_file=config)
    started, finish = Event(), Event()

    @app.get("/api/isolated-block")
    def hold():
        started.set()
        assert finish.wait(3)
        return {"finished": True}

    app.routes.insert(0, app.routes.pop())
    with TestClient(app) as client, ThreadPoolExecutor() as pool:
        login(client)
        # 在另一个终端使用同一个应用，确保响应结束前禁止切换。
        future = pool.submit(client.get, "/api/isolated-block")
        try:
            assert started.wait(3)
            response = submit(client)
            assert response.status_code == 409 and response.json()["code"] == "nas_requests_in_progress"
            assert config.read_bytes() == before
        finally:
            finish.set()
        assert future.result(timeout=5).status_code == 200


@pytest.mark.parametrize("addresses", [("127.0.0.1",), ("169.254.169.254",), ("8.8.8.8",),
                                        ("192.168.1.2", "203.0.113.1"), ("192.168.1.2", "192.168.1.3")])
def test_dns_uses_one_private_nonlocal_address(monkeypatch, addresses):
    from app.errors import AppError

    monkeypatch.setattr(probe.socket, "getaddrinfo", lambda *args, **kwargs: [(None, None, None, None, (ip, 0)) for ip in addresses])
    with pytest.raises(AppError, match="私网"):
        probe.endpoint("nas.internal")


def test_two_sibling_paths_still_reject_overlap():
    from app.services.nas.paths import NasPaths

    with pytest.raises(ValueError, match="重叠"):
        NasPaths("/部门/设计资产", "/部门/设计资产/临时", "/部门/备份")


def test_windows_config_relocation_keeps_data_and_ca_paths(tmp_path):
    from app.config import load_settings
    from app.db.database import Database
    from app.services.crypto import SecretCipher
    from deploy.ops import config_location, relocate_config, rollback, snapshot

    old = tmp_path / "config.yaml"
    cipher = SecretCipher(tmp_path / "data")
    old.write_text(yaml.safe_dump({"mode": "prod", "bootstrap_admin": "a",
        "nas": {"host": "192.168.1.10", "dsm_ca_file": "./test.pem", "system_password_enc": cipher.encrypt(SECRET)}}))
    (tmp_path / "configuration").mkdir()
    db = Database(tmp_path / "data")
    db.migrate()
    db.close()
    relocate_config(tmp_path)
    new = config_location(tmp_path)
    assert new.parent.name == "configuration"
    config = load_settings(new)
    assert config.paths == load_settings(old).paths
    assert config.nas.dsm_ca_file == tmp_path / "test.pem"
    assert SECRET not in new.read_text()
    package = tmp_path / "backups/pre.zip"
    package.parent.mkdir()
    original = new.read_bytes()
    snapshot(tmp_path, package)
    new.write_bytes(original + b"\n# after")
    rollback(tmp_path, package)
    assert new.read_bytes() == original and old.exists()
