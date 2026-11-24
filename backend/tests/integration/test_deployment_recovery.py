"""隔离模拟 NAS 和 SQLite 上演练 WP14 两种恢复。"""
from pathlib import Path
from time import perf_counter
from zipfile import ZIP_DEFLATED, ZipFile
import hashlib
import json
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.db.database import Database
from app.db.models import CatalogFile, Link, Product, ProductTag, Project, ProductRelation, User
from app.main import create_app
from app.services.archive import publication
from app.services.jobs import JobFailure
from app.services.nas import Region
from app.services.nas.factory import create_paths
from app.services.recovery import rebuild, rebuild_auxiliary, scan
from test_catalog_access import published
from test_maintenance import maintained
from test_postop_execution import approve, create, state, submit

ROOT = Path(__file__).resolve().parents[3]


def test_archive_rebuild_preserves_links_history_and_single_latest_version(maintained, tmp_path):
    flow, project, settings, config = maintained
    app = flow.client.app
    fs = app.state.auth.pool.system()
    paths = create_paths(settings)
    root = paths.root(Region.FORMAL)
    params = {"project_name": "确认最新版"}
    bid = create(flow, project, "rename", params)
    submit(flow, bid, params)
    approve(flow)
    assert app.state.jobs.wait_idle(8)
    assert state(flow, bid)["status"] == "已入库"
    started = perf_counter()
    report, selected = scan(fs, root)
    assert len(selected) == 1 and not report["unresolved"]
    assert selected[0].version == 1 and selected[0].verified
    with app.state.database.session() as session:
        before = list(session.execute(select(Link.kind, Link.target_id, Link.link_id).order_by(Link.id)))
        tags = [(r.product_id, r.field_key, r.value) for r in session.scalars(select(ProductTag))]
        src = session.get(Project, project.id)
        assert selected[0].path == src.current_path
    target = tmp_path / "new-db"
    totals = rebuild(fs, root, target, selected)
    with pytest.raises(FileExistsError):
        rebuild(fs, root, target, selected)
    assert totals["projects"] == 1 and totals["files"] == 3 and totals["tags"] == 11
    with Database(target).session() as session:
        restored = session.get(Project, project.id)
        for kind, id_, link in before:
            assert (kind, id_, link) in session.execute(select(Link.kind, Link.target_id, Link.link_id)).all()
        assert restored.status == "ingesting"
        assert not session.scalars(select(User)).all()
        assert sorted(tags) == sorted((r.product_id, r.field_key, r.value) for r in session.scalars(select(ProductTag)))
    old_link = next(link for kind, _, link in before if kind == "product")
    settings_new = settings.model_copy(deep=True)
    settings_new.paths.data_dir = target
    with TestClient(create_app(settings_new, log_dir=tmp_path / "hidden-logs", config_file=config)) as hidden:
        assert hidden.post("/api/auth/login", json={"username":"other", "password":"fake"}).status_code == 200
        assert hidden.get("/l/" + old_link).status_code == 404
    result = rebuild_auxiliary(settings, target, fs, root, selected)
    assert result == {"active": [project.id], "blocked": [], "thumbnails_failed": []}
    boot = perf_counter()
    with TestClient(create_app(settings_new, log_dir=tmp_path / "recovered-logs", config_file=config)) as active:
        print(f"WP14 隔离 TestClient 启动 {(perf_counter() - boot) * 1000:.3f} 毫秒")
        assert active.post("/api/auth/login", json={"username":"other", "password":"fake"}).status_code == 200
        resolved = active.get("/l/" + old_link)
        assert resolved.status_code == 200 and resolved.json()["status"] == "active"
        assert active.post("/api/search", json={"query": "杯", "limit": 10}).status_code == 200
    print(f"WP14 场景 B · 扫描、重建、向量和启动验证 {perf_counter() - started:.3f} 秒")


def test_unpublished_version_blocks_automatic_fallback_and_manual_evidence(maintained, monkeypatch):
    flow, project, settings, _ = maintained
    app = flow.client.app
    monkeypatch.setattr(app.state.postop_executor, "_publish",
                        lambda *args: (_ for _ in ()).throw(JobFailure("模拟 SQLite 提交之前断电", retryable=False)))
    params = {"project_name": "已安装待发布"}
    bid = create(flow, project, "rename", params)
    submit(flow, bid, params)
    approve(flow)
    assert app.state.jobs.wait_idle(8)
    assert state(flow, bid)["status"] == "入库异常"
    root = create_paths(settings).root(Region.FORMAL)
    fs = app.state.auth.pool.system()
    report, chosen = scan(fs, root)
    assert not chosen and len(report["unresolved"]) == 1
    candidates = report["unresolved"][0]["candidates"]
    assert {(row["version"], row["publication_receipt"]) for row in candidates} == {(0, True), (1, False)}
    older = next(row for row in candidates if row["version"] == 0)
    report, chosen = scan(fs, root, {project.project_code.casefold(): {**older, "basis": "人工核对上一批准发布记录和未提交日志"}})
    assert len(chosen) == 1 and chosen[0].path == project.current_path


def test_missing_receipt_needs_review_and_vector_failure_keeps_gate(maintained, tmp_path, monkeypatch):
    from app.services.ingest import vectors
    flow, project, settings, _ = maintained
    final_fs = next(row for row in flow.client.app.state.auth.pool._sessions.values() if row.permissions.get(Region.FORMAL) == "write")
    paths = create_paths(settings)
    root = paths.root(Region.FORMAL)
    final_fs.delete(paths.join(Region.FORMAL, project.current_path, publication.RECEIPT))
    readonly = flow.client.app.state.auth.pool.system()
    report, chosen = scan(readonly, root)
    assert not chosen and report["unresolved"]
    candidate = report["unresolved"][0]["candidates"][0]
    _, picked = scan(readonly, root, {project.project_code.casefold(): {**candidate, "basis": "隔离测试人工比对审批回单"}})
    target = tmp_path / "isolated"
    rebuild(readonly, root, target, picked)
    def fail(*args):
        raise JobFailure("模拟向量服务拒绝", retryable=False)
    monkeypatch.setattr(vectors, "generate", fail)
    results = rebuild_auxiliary(settings, target, readonly, root, picked)
    assert not results["active"] and results["blocked"]
    with Database(target).session() as session:
        assert session.get(Project, project.id).status == "ingesting"


def test_archive_rebuild_preserves_confirmed_set_relations(published, settings, tmp_path):
    flow, project, _, _, _, links = published
    source = flow.client.app.state.database
    with source.session() as session:
        relations = session.execute(select(ProductRelation.set_product_id, ProductRelation.item_product_id,
                                           ProductRelation.status).order_by(ProductRelation.id)).all()
    assert len(relations) == 2
    fs = flow.client.app.state.auth.pool.system()
    root = create_paths(settings).root(Region.FORMAL)
    report, selected = scan(fs, root)
    assert not report["unresolved"] and len(selected) == 1
    target = tmp_path / "relations"
    totals = rebuild(fs, root, target, selected)
    assert totals["relations"] == 2 and totals["tags"] == 33
    with Database(target).session() as session:
        assert session.get(Project, project.id).status == "ingesting"
        assert session.execute(select(ProductRelation.set_product_id, ProductRelation.item_product_id,
                                      ProductRelation.status).order_by(ProductRelation.id)).all() == relations
        assert {(r.kind, r.target_id): r.link_id for r in session.scalars(select(Link))} == links


def test_upgrade_checkpoint_rollback_preserves_roles_and_vector_pointer(maintained, tmp_path):
    from deploy.ops import rollback, snapshot
    from app.db.models import ReviewRound, ProductEmbedding, Setting
    from app.services.ingest.vectors import FORMAL_MODEL
    flow, project, settings, config = maintained
    assert settings.paths.data_dir == tmp_path / "data"
    package = tmp_path / "backups" / "previous.zip"
    package.parent.mkdir()
    db = flow.client.app.state.database
    with db.session() as session:
        baseline = session.scalar(select(Setting).where(Setting.key == FORMAL_MODEL)).value
        roles = {u.id: u.roles for u in session.scalars(select(User))}
        review_ids = list(session.scalars(select(ReviewRound.id)))
        blob = session.scalar(select(ProductEmbedding)).embedding
    started = perf_counter()
    snapshot(tmp_path, package)
    with db.session() as session:
        session.get(Project, project.id).project_name = "故障升级留下的状态"
    db.engine.dispose()
    rollback(tmp_path, package)
    with db.session() as session:
        assert session.get(Project, project.id).project_name == project.project_name
        assert session.scalar(select(Setting).where(Setting.key == FORMAL_MODEL)).value == baseline
        assert {u.id: u.roles for u in session.scalars(select(User))} == roles
        assert list(session.scalars(select(ReviewRound.id))) == review_ids
        assert session.scalar(select(ProductEmbedding)).embedding == blob
    print(f"WP14 场景 A · 一致性快照和故障回滚 {perf_counter() - started:.3f} 秒")


def test_migrates_and_restores_p0_legacy_database(tmp_path):
    from alembic import command
    from alembic.config import Config
    from app.services.crypto import SecretCipher
    from deploy.ops import rollback, snapshot

    config = tmp_path / "config.yaml"
    config.write_text("mode: dev\n", encoding="utf-8")
    db = Database(tmp_path / "data")
    cipher = SecretCipher(db.data_dir)
    sealed = cipher.encrypt("恢复密钥测试")
    alembic = Config(str(ROOT / "backend" / "alembic.ini"))
    alembic.set_main_option("script_location", str(ROOT / "backend" / "alembic"))
    with db.engine.begin() as connection:
        alembic.attributes["connection"] = connection
        command.upgrade(alembic, "0011_p0_device_sessions")
        connection.exec_driver_sql(
            "INSERT INTO users(id, username, roles, enabled, created_at, updated_at) "
            "VALUES (42, 'legacy', ?, 1, datetime('now'), datetime('now'))", ('["first_reviewer"]',),
        )
    db.close()
    package = tmp_path / "backups" / "legacy.zip"
    package.parent.mkdir()
    snapshot(tmp_path, package)
    try:
        db.migrate()
        with db.engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == "0023_nas_access_audit"
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
        with db.session() as session:
            assert session.get(User, 42).roles == ["first_reviewer"]
    finally:
        db.close()
    rollback(tmp_path, package)
    with db.engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == "0011_p0_device_sessions"
        assert connection.exec_driver_sql("SELECT username FROM users WHERE id=42").scalar() == "legacy"
    db.close()
    assert SecretCipher(db.data_dir).decrypt(sealed) == "恢复密钥测试"


def test_preflight_never_counts_fake_backend_as_prod(maintained, tmp_path):
    import yaml
    _, _, settings, _ = maintained
    config = tmp_path / "preflight.yaml"
    config.write_text(yaml.safe_dump({"mode":"dev", "paths":{"data_dir":str(settings.paths.data_dir)}}))
    out = tmp_path / "report.json"
    verified = subprocess.run([sys.executable, str(ROOT / "scripts/preflight_check.py"), "--config", str(config),
                               "--network", "--test-ai", "--report", str(out)], capture_output=True, text=True)
    assert verified.returncode == 0, verified.stdout + verified.stderr
    result = {row["name"]:row["status"] for row in json.loads(out.read_text())["checks"]}
    assert result["SQLite 可读写"] == "PASS"
    assert result["SMB 系统账号与正式区读取"] == result["百炼识图"] == result["百炼向量"] == "SKIP"
    assert result["实际 NAS ACL：系统账号禁止写正式区和访问临时区"] == "MANUAL"


def test_release_verification_rejects_overrides_and_non_allowlist(tmp_path):
    from scripts.build_release import REQUIRED, verify
    files = {path: (path + "\n").encode() for path in REQUIRED | {"backend/app/static/assets/main.js"}}
    def make(path, update=None):
        manifest = {"format": 1, "version": "v1.0.0-rc.1", "files": {name:{"size":len(data),
             "sha256":hashlib.sha256(data).hexdigest()} for name,data in files.items()}}
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            for name,data in files.items():
                archive.writestr(name, data)
            if update:
                archive.writestr(*update)
            archive.writestr("RELEASE.json", json.dumps(manifest))
        return path
    good = make(tmp_path / "ok.zip")
    assert verify(good, hashlib.sha256(good.read_bytes()).hexdigest(), tmp_path / "expansion")["version"] == "v1.0.0-rc.1"
    with pytest.raises(ValueError, match="SHA-256"):
        verify(good, "a" * 64)
    with pytest.raises(ValueError):
        verify(make(tmp_path / "unsafe.zip", ("../../config.yaml", b"secret")))
    with pytest.raises(ValueError):
        verify(make(tmp_path / "private.zip", ("secret.key", b"secret")))


def test_local_performance_and_search_measurements(maintained):
    from statistics import mean, quantiles
    from app.db.models import AiCall
    from app.services.auth.service import COOKIE_NAME
    from app.services.upload.fingerprint import fingerprint

    flow, project, settings, _ = maintained
    app = flow.client.app
    with app.state.database.session() as session:
        product_id = session.scalar(select(Product.id).where(Product.project_id == project.id))
        calls = [row.duration_ms for row in session.scalars(select(AiCall))]
    ms = {}
    start = perf_counter()
    flow.sign_in("one")
    ms["login"] = (perf_counter() - start) * 1000
    for label, url in (("project_list", "/api/projects"), ("product_detail", f"/api/products/{product_id}")):
        start = perf_counter()
        assert flow.call("one", "GET", url).status_code == 200
        ms[label] = (perf_counter() - start) * 1000
    times = []
    for _ in range(12):
        start = perf_counter()
        assert flow.call("one", "POST", "/api/search", {"query": "杯", "limit": 10}).status_code == 200
        times.append((perf_counter() - start) * 1000)
    block = b"asset-data" * (16 * 1024 * 1024 // 10)
    payload = {"folder_name":"P002_企业_分片性能", "won":True, "acks":["P08"], "files":[
        {"rel_path":"单品/杯.jpg", "size":1, "mtime":1},
        {"rel_path":"01定稿文件/大文件.ai", "size":len(block), "mtime":1},
    ]}
    result = flow.call("one", "POST", "/api/batches", payload)
    assert result.status_code == 200, result.text
    batch = result.json()
    row = next(row for row in batch["files"] if row["rel_path"].endswith(".ai"))
    url = f"/api/batches/{batch['id']}/files/{row['id']}"
    headers = {"Cookie": f"{COOKIE_NAME}={flow.cookies['one']}", "X-Upload-Token": batch["upload_token"]}
    chunk = batch["chunk_bytes"]
    begin = perf_counter()
    for i, offset in enumerate(range(0, len(block), chunk)):
        received = flow.client.put(f"{url}/chunks/{i}", content=block[offset:offset+chunk], headers=headers)
        assert received.status_code == 200
    elapsed = perf_counter() - begin
    assert flow.client.post(url + "/complete", headers=headers).status_code == 200
    paths = create_paths(settings)
    login = app.state.auth.context(flow.cookies['one'], "验收")
    fs = app.state.auth.pool.get(login.session_id)
    begin = perf_counter()
    assert fingerprint(fs, paths.join(Region.TEMP, batch["batch_no"], batch["folder_name"], row["rel_path"]),
                       len(block)) == hashlib.sha256(block).hexdigest()
    ms["sha256_16mib"] = (perf_counter() - begin) * 1000
    ms.update(search_average=mean(times), search_p95=quantiles(times, n=20, method="inclusive")[18],
              fake_ai_average=mean(calls), upload_mib_s=len(block) / 2**20 / elapsed)
    print("WP14 本机性能 " + json.dumps({key:round(val, 3) for key, val in ms.items()}))
