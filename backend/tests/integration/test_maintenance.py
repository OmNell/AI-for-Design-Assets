"""隔离工作台的只读巡检、双审修复、数据库备份及临时区清理。"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from pathlib import Path
from threading import Event
import importlib.util
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import BackupRecord, Batch, BatchFile, CatalogFile, InspectionFinding, Job, Link, PostopPlan, Product, Project
from app.main import create_app
from app.services import archive
from app.services.backup.format import inventory
from app.services.inspection import InspectionService
from app.services.maintenance.settings import CST
from app.services.nas import NasPermissionDenied, Region
from app.services.nas.factory import create_paths
from test_ingest import start
from test_two_stage_review import ALL, Flow, configure


@pytest.fixture
def maintained(settings, tmp_path):
    configure(settings, tmp_path)
    for actor in settings.dev_users:
        actor.permissions["formal"] = "write" if actor.username == "final" else "read"
    config = tmp_path / "config.yaml"
    config.write_text("mode: dev\n", encoding="utf-8")
    with TestClient(create_app(settings, log_dir=tmp_path / "logs", config_file=config)) as client:
        flow = Flow(client, ["单品/杯.jpg", "01定稿文件/缺失.ai", "01定稿文件/漂移.ai"])
        start(flow)
        assert client.app.state.jobs.wait_idle(8)
        with client.app.state.database.session() as session:
            project = session.get(Project, session.get(Batch, flow.bid).project_id)
            assert project.status == "active"
        yield flow, project, settings, config


def inspect(flow):
    trigger = flow.call("admin", "POST", "/api/admin/maintenance/inspection/run")
    assert trigger.status_code == 202, trigger.text
    assert flow.client.app.state.jobs.wait_idle(8)
    runs = flow.call("admin", "GET", "/api/admin/inspection/runs")
    assert runs.status_code == 200, runs.text
    run = runs.json()[0]
    assert run["status"] == "done", run
    response = flow.call("admin", "GET", f"/api/admin/inspection/findings?run_id={run['id']}")
    assert response.status_code == 200, response.text
    return run, response.json()


def test_readonly_drift_and_two_review_archive_repair(maintained):
    flow, project, settings, _ = maintained
    app = flow.client.app
    paths = create_paths(settings)
    folder = settings.paths.dev_nas_dir / paths.root(Region.FORMAL) / project.current_path
    new_root = "02未中标项目/29别的行业/01其他企业/" + folder.name
    target = settings.paths.dev_nas_dir / paths.join(Region.FORMAL, new_root)
    target.parent.mkdir(parents=True)
    folder.rename(target)
    (target / "01定稿文件/漂移.ai").write_bytes(b"changed")
    (target / "01定稿文件/额外.ai").write_bytes(b"unregistered")
    (target / "01定稿文件/缺失.ai").unlink()
    (target / archive.PROJECT_FILE).write_bytes(b"invalid")
    # 项目 TXT 用于定位，单品档案另遭损坏，需独立双审修复。
    parent = paths.join(Region.FORMAL, new_root)
    final = next(row for row in app.state.auth.pool._sessions.values() if row.permissions.get(Region.FORMAL) == "write")
    with app.state.database.session() as session:
        archive.rebuild(session, project, final, parent)
        product = session.scalar(select(Product).where(Product.project_id == project.id))
    (target / product.archive_rel_path).unlink()
    original_sha = sha256((target / "单品/杯.jpg").read_bytes()).hexdigest()
    baseline_before = {str(p.relative_to(target)): p.read_bytes() for p in target.rglob("*") if p.is_file()}
    run, findings = inspect(flow)
    assert baseline_before == {str(p.relative_to(target)): p.read_bytes() for p in target.rglob("*") if p.is_file()}
    # 项目 TXT 保留原路径，因此卡片柜自动定位后仍需重新生成档案。
    assert {row["kind"] for row in findings} == {"moved", "modified", "unregistered", "missing", "archive_broken"}
    with app.state.database.session() as session:
        current = session.get(Project, project.id)
        assert current.current_path == new_root
        assert (current.company_name, current.company_id, current.industry_id, current.won) == (
            project.company_name, project.company_id, project.industry_id, project.won)
        assert session.scalar(select(CatalogFile).where(CatalogFile.rel_path == "01定稿文件/漂移.ai")).fingerprint != sha256(b"changed").hexdigest()
        links = list(session.scalars(select(Link.link_id).order_by(Link.id)))
    run, findings = inspect(flow)
    broken = next(row for row in findings if row["kind"] == "archive_broken")
    assert flow.call("other", "POST", f"/api/admin/inspection/findings/{broken['id']}/ack").status_code == 403
    ack = flow.call("admin", "POST", f"/api/admin/inspection/findings/{broken['id']}/ack")
    assert ack.status_code == 200 and ack.json()["acknowledged_at"]
    assert flow.call("admin", "POST", f"/api/projects/{project.id}/archive/repair").status_code == 201  # 后续改用不同发起人进行本轮审核。
    created = flow.call("final", "GET", "/api/archive/repairs").json()
    assert created and not created[0]["ready"]
    with app.state.database.session() as session:
        batch = session.scalar(select(Batch).where(Batch.kind == "archive_repair"))
        bid = batch.id
    assert flow.call("one", "POST", f"/api/batches/{bid}/operation/submit", {"params": {}}).status_code == 403
    submitted = flow.call("admin", "POST", f"/api/batches/{bid}/operation/submit", {"params": {}})
    assert submitted.status_code == 200, submitted.text
    flow.base = f"/api/review/{bid}"
    assert flow.audit("admin", "claim").status_code == 403
    assert flow.audit("first", "claim").status_code == 200
    assert flow.audit("first", "pass", ALL).status_code == 200
    assert flow.audit("final", "claim").status_code == 200
    assert flow.audit("final", "pass", ALL).status_code == 200
    assert app.state.jobs.wait_idle(8)
    trace = flow.call("final", "GET", f"/api/operations/{bid}/execution").json()
    assert trace["status"] == "已入库", trace
    with app.state.database.session() as session:
        current = session.get(Project, project.id)
        assert archive.verify(session, current, final, parent) == []
        assert list(session.scalars(select(Link.link_id).order_by(Link.id))) == links
        assert session.get(InspectionFinding, broken["id"]).resolved_at is not None
    assert sha256((target / "单品/杯.jpg").read_bytes()).hexdigest() == original_sha
    with pytest.raises(NasPermissionDenied):
        app.state.auth.pool.system().open_write(paths.join(Region.FORMAL, "禁止写入"))
    with pytest.raises(NasPermissionDenied):
        app.state.auth.pool.system().list(paths.root(Region.TEMP))


def test_schedule_and_manual_share_exclusive_job(maintained, monkeypatch):
    flow, _, _, _ = maintained
    service = flow.client.app.state.system
    assert service.scheduler.get_job("inspection").trigger.timezone == CST
    assert service.scheduler.get_job("backup").trigger.timezone == CST
    arrived, go = Event(), Event()
    saved = InspectionService._tree

    def blocking(self, fs, ignored):
        arrived.set()
        assert go.wait(5)
        return saved(self, fs, ignored)

    with monkeypatch.context() as patch:
        patch.setattr(InspectionService, "_tree", blocking)
        at = datetime(2031, 2, 3, 2, 0, 30, tzinfo=CST)
        try:
            service.tick(at)
            assert arrived.wait(5)
            service.tick(at)
            assert flow.call("admin", "POST", "/api/admin/maintenance/inspection/run").status_code == 409
        finally:
            go.set()
        assert flow.client.app.state.jobs.wait_idle(8)
    service.tick(at)
    with flow.client.app.state.database.session() as session:
        jobs = list(session.scalars(select(Job).where(Job.kind == "inspection")))
        assert len(jobs) == 1 and jobs[0].payload["slot"] == "2031-02-03 02:00"


def test_backup_restore_and_access(maintained, tmp_path):
    flow, _, settings, config = maintained
    response = flow.call("admin", "POST", "/api/admin/maintenance/backup/run")
    assert response.status_code == 202, response.text
    assert flow.client.app.state.jobs.wait_idle(8)
    records = flow.call("admin", "GET", "/api/admin/backups").json()
    assert len(records) == 1 and records[0]["status"] == "done", records
    path = f"/api/admin/backups/{records[0]['id']}/download"
    assert flow.call("other", "GET", path).status_code == 403
    data = flow.call("admin", "GET", path)
    assert data.status_code == 200
    package = tmp_path / "downloaded.zip"
    package.write_bytes(data.content)
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("restore_backup", root / "scripts/restore_backup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    restored = tmp_path / "restored"
    assert module.restore(package, restored) == inventory(settings.paths.data_dir / "app.db")
    assert (restored / "config.yaml").read_bytes() == config.read_bytes()
    assert (restored / "data/secret.key").read_bytes() == (settings.paths.data_dir / "secret.key").read_bytes()
    with flow.client.app.state.database.session() as session:
        link = session.scalar(select(Link.link_id).where(Link.kind == "product"))
    copy = settings.model_copy(deep=True)
    copy.paths.data_dir = restored / "data"
    with TestClient(create_app(copy, log_dir=restored / "logs", config_file=restored / "config.yaml")) as recovery:
        assert recovery.post("/api/auth/login", json={"username": "other", "password": "fake"}).status_code == 200
        assert recovery.get("/l/" + link).json()["status"] == "active"
    with pytest.raises(ValueError):
        module.restore(package, restored)
    malicious = tmp_path / "bad.zip"
    with ZipFile(malicious, "w") as archive_file:
        archive_file.writestr("../outside/db", "invalid")
    with pytest.raises(ValueError):
        module.restore(malicious, tmp_path / "invalid")


def test_cleanup_rechecks_active_batch_and_keeps_audit(maintained):
    flow, _, settings, _ = maintained
    app = flow.client.app
    paths = create_paths(settings)
    with app.state.database.session() as session:
        batch = session.get(Batch, flow.bid)
        extra = BatchFile(batch_id=flow.bid, rel_path="01定稿文件/临时留存.ai", mtime=1, size=4)
        session.add(extra)
        target = settings.paths.dev_nas_dir / paths.join(Region.TEMP, batch.temp_path, extra.rel_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"test")
    assert flow.call("other", "GET", "/api/admin/temp-cleanup").status_code == 403
    listed = flow.call("admin", "GET", "/api/admin/temp-cleanup").json()
    row = next(item for item in listed if item["batch_id"] == flow.bid)
    route = f"/api/admin/temp-cleanup/preview/{row['key']}"
    plan = flow.call("admin", "POST", route).json()
    assert len(plan["entries"]) == 1
    endpoint = f"/api/admin/temp-cleanup/execute/{plan['id']}"
    assert flow.call("admin", "POST", endpoint, {"confirmation": "错误"}).status_code == 422
    with app.state.database.session() as session:
        session.get(Batch, flow.bid).status = "入库异常"
    assert flow.call("admin", "POST", endpoint, {"confirmation": row["label"]}).status_code == 409
    assert flow.call("admin", "POST", route).status_code == 409 and target.exists()
    with app.state.database.session() as session:
        session.get(Batch, flow.bid).status = "已入库"
    assert flow.call("admin", "POST", endpoint, {"confirmation": row["label"]}).status_code == 200
    assert not target.exists()
    with app.state.database.session() as session:
        assert session.get(Batch, flow.bid).status == "已入库"
    logs = flow.call("admin", "GET", "/api/admin/oplog?action=完成临时区清理")
    assert logs.status_code == 200 and logs.json()["total"] == 1


def test_admin_settings_log_export_and_thumbnail(maintained):
    flow, project, _, _ = maintained
    get = flow.call("admin", "GET", "/api/settings/system")
    assert get.status_code == 200, get.text
    payload = get.json()
    assert flow.call("admin", "PUT", "/api/settings/system", {**payload, "cooling_days": 29}).status_code == 422
    assert flow.call("admin", "PUT", "/api/settings/system", {**payload, "single_review": True}).status_code == 422
    assert flow.call("other", "GET", "/api/settings/system").status_code == 403
    assert flow.call("admin", "PUT", "/api/settings/system", {**payload, "chunk_mb": 12, "inspection_time": "01:15"}).status_code == 200
    assert flow.client.app.state.upload.chunk_bytes == 12 * 1024 * 1024
    old = flow.call("one", "GET", f"/api/batches/{flow.bid}").json()
    assert old["chunk_bytes"] == payload["chunk_mb"] * 1024 * 1024
    export = flow.call("admin", "GET", "/api/admin/oplog/export?action=更新系统参数")
    assert export.status_code == 200 and export.content.startswith(b"\xef\xbb\xbf") and "更新系统参数" in export.text
    assert flow.call("other", "GET", "/api/admin/oplog/export").status_code == 403
    searches = flow.call("admin", "GET", "/api/admin/search-logs")
    assert searches.status_code == 200 and searches.json()["total"] == 0
    assert flow.call("admin", "GET", "/api/admin/search-logs/export").content.startswith(b"\xef\xbb\xbf")
    with flow.client.app.state.database.session() as session:
        product = session.scalar(select(Product).where(Product.project_id == project.id))
    assert flow.call("admin", "POST", "/api/admin/thumbnails/rebuild", {"product_ids": [product.id]}).status_code == 202
    assert flow.client.app.state.jobs.wait_idle(8)
    with flow.client.app.state.database.session() as session:
        job = session.scalar(select(Job).where(Job.kind == "catalog_thumb"))
        assert job.status == "done" and job.run_as_user_id == flow.users["admin"]
    assert flow.call("admin", "GET", "/api/admin/maintenance/errors").status_code == 200


def test_inspection_preserves_wp12_history_while_detecting_unknown_files(maintained):
    from app.db.models import ExpectedDeletion
    from test_postop_execution import approve, create, state, submit

    flow, project, settings, _ = maintained
    app = flow.client.app
    paths = create_paths(settings)
    params = {"project_name": "已发布新版本"}
    operation = create(flow, project, "rename", params)
    submit(flow, operation, params)
    approve(flow)
    assert app.state.jobs.wait_idle(8)
    assert state(flow, operation)["status"] == "已入库"
    with app.state.database.session() as session:
        post = session.get(PostopPlan, operation)
        old = post.previous_path
        current = session.get(Project, project.id).current_path
        assert current != old
        row = session.scalar(select(CatalogFile).where(CatalogFile.rel_path == "01定稿文件/缺失.ai"))
        relative = f"{old}/{row.rel_path}"
        session.add(ExpectedDeletion(batch_id=operation, file_id=row.id, path=relative, size=row.size, fingerprint=row.fingerprint))
    (settings.paths.dev_nas_dir / paths.join(Region.FORMAL, relative)).unlink()
    (settings.paths.dev_nas_dir / paths.join(Region.FORMAL, old, "外部文件.ai")).write_bytes(b"unexpected")
    _, findings = inspect(flow)
    assert [(row["kind"], row["path"]) for row in findings] == [("unregistered", f"{old}/外部文件.ai")]
    with app.state.database.session() as session:
        assert session.get(Project, project.id).current_path == current


def test_pending_shadow_remains_hidden_after_interruption(maintained, monkeypatch):
    from app.services.jobs import JobFailure
    from app.services.postops import embeddings
    from test_postop_execution import approve, create, state, submit

    flow, project, _, _ = maintained
    app = flow.client.app

    def fail(*args):
        raise JobFailure("模拟正式向量不可用", retryable=False)

    monkeypatch.setattr(embeddings, "prepare", fail)
    params = {"project_name": "待发布版本"}
    batch = create(flow, project, "rename", params)
    submit(flow, batch, params)
    approve(flow)
    assert app.state.jobs.wait_idle(8)
    assert state(flow, batch)["status"] == "入库异常"
    report, findings = inspect(flow)
    assert not findings and report["summary"]["active_operations"] == 1
    with app.state.database.session() as session:
        assert session.get(Project, project.id).current_path == project.current_path


def test_backup_write_denial_preserves_local_and_allows_retry(maintained, monkeypatch):
    from app.services.nas import LocalFs

    flow, _, settings, _ = maintained
    app = flow.client.app
    system = app.state.auth.pool.system()
    old = LocalFs.open_write

    def denied(fs, path, append=False):
        if fs is system:
            raise NasPermissionDenied("隔离测试备份权限")
        return old(fs, path, append)

    with monkeypatch.context() as patch:
        patch.setattr(LocalFs, "open_write", denied)
        r = flow.call("admin", "POST", "/api/admin/maintenance/backup/run")
        assert r.status_code == 202, r.text
        assert app.state.jobs.wait_idle(8)
    with app.state.database.session() as session:
        record = session.scalar(select(BackupRecord))
        assert session.get(Job, record.job_id).status == "failed"
        assert record.verified_at is None
    path = settings.paths.data_dir / "backup-stage" / record.name
    saved = path.read_bytes()
    assert flow.call("admin", "POST", f"/api/admin/jobs/{record.job_id}/retry").status_code == 200
    assert app.state.jobs.wait_idle(8)
    assert path.read_bytes() == saved
    assert flow.call("admin", "GET", "/api/admin/backups").json()[0]["verified_at"]


def test_orphan_cleanup_needs_personal_nas_write_permission(maintained, monkeypatch):
    from app.services.nas import LocalFs

    flow, _, settings, _ = maintained
    paths = create_paths(settings)
    target = settings.paths.dev_nas_dir / paths.join(Region.TEMP, "未关联.ai")
    target.write_bytes(b"temporary")
    listed = flow.call("admin", "GET", "/api/admin/temp-cleanup").json()
    row = next(entry for entry in listed if entry["label"] == target.name)
    plan = flow.call("admin", "POST", f"/api/admin/temp-cleanup/preview/{row['key']}").json()
    original = LocalFs.delete
    observed = []

    def denied(fs, path, recursive=False):
        observed.append(fs)
        if path.endswith(target.name):
            raise NasPermissionDenied("个人 NAS 无删除权限")
        return original(fs, path, recursive)

    with monkeypatch.context() as patch:
        patch.setattr(LocalFs, "delete", denied)
        result = flow.call("admin", "POST", f"/api/admin/temp-cleanup/execute/{plan['id']}", {"confirmation": target.name})
        assert result.status_code == 403
    assert len(observed) == 1 and observed[0] is not flow.client.app.state.auth.pool.system() and target.exists()


def test_stable_10k_metadata_scan(maintained):
    from time import perf_counter

    flow, project, settings, _ = maintained
    total = 10_000
    paths = create_paths(settings)
    app = flow.client.app
    parent = settings.paths.dev_nas_dir / paths.join(Region.FORMAL, project.current_path, "01定稿文件", "批量样本")
    parent.mkdir()
    with app.state.database.session() as session:
        rows = []
        for index in range(total - 3):
            path = parent / f"{index:05}.ai"
            path.write_bytes(b"sample")
            rows.append(CatalogFile(project_id=project.id, size=6, mtime=int(path.stat().st_mtime * 1000),
                rel_path=f"01定稿文件/批量样本/{path.name}", fingerprint=sha256(b"sample").hexdigest(), category="01定稿文件"))
        session.add_all(rows)
        session.flush()
        session.add_all(Link(kind="file", target_id=row.id, link_id=f"fixture_file_{row.id}") for row in rows)
    final = next(row for row in app.state.auth.pool._sessions.values() if row.permissions.get(Region.FORMAL) == "write")
    with app.state.database.session() as session:
        archive.rebuild(session, session.get(Project, project.id), final, paths.join(Region.FORMAL, project.current_path))
    started = perf_counter()
    run, findings = inspect(flow)
    elapsed = perf_counter() - started
    assert not findings
    assert run["summary"]["files"] >= total and run["summary"]["hashed_files"] == 0
    assert elapsed < 120
    print(f"LocalFs 已扫描 {total} 个资产文件，用时 {elapsed:.3f} 秒（报告 {run['summary']['elapsed_ms']} 毫秒）")


def test_local_retention_keeps_latest_seven_remote_archives_to_remain(maintained):
    flow, _, settings, _ = maintained
    app = flow.client.app
    for _ in range(8):
        assert flow.call("admin", "POST", "/api/admin/maintenance/backup/run").status_code == 202
        assert app.state.jobs.wait_idle(8)
    data = flow.call("admin", "GET", "/api/admin/backups").json()
    assert len(data) == 8 and all(item["status"] == "done" for item in data)
    assert len(list((settings.paths.data_dir / "backup-stage").glob("*.zip"))) == 7
    assert data[-1]["local_retained"] is False
    assert len(list((settings.paths.dev_nas_dir / create_paths(settings).root(Region.BACKUP)).glob("*.zip"))) == 8


def test_ambiguous_file_drift_keeps_registered_path(maintained):
    flow, project, settings, _ = maintained
    root = settings.paths.dev_nas_dir / create_paths(settings).join(Region.FORMAL, project.current_path)
    name = "01定稿文件/缺失.ai"
    source = root / name
    data = source.read_bytes()
    (root / "版本一.ai").write_bytes(data)
    (root / "版本二.ai").write_bytes(data)
    source.unlink()
    _, found = inspect(flow)
    assert any(row["kind"] == "missing" and row["path"].endswith(name) for row in found)
    assert not any(row["kind"] == "moved" for row in found)
    with flow.client.app.state.database.session() as session:
        row = session.scalar(select(CatalogFile).where(CatalogFile.rel_path == name))
        assert row.physical_rel_path is None


def test_search_export_only_unclicked_and_retains_full_facts(maintained):
    flow, _, _, _ = maintained
    from app.db.models import SearchClick, SearchLog

    with flow.client.app.state.database.session() as session:
        user_id = flow.users["one"]
        base = {"user_id": user_id, "parsed": {}, "result_count": 1, "duration_ms": 21, "index_generation": 1,
                "warnings": ["本地降级"], "ai_usage": [], "targets": {}}
        clicked = SearchLog(query="点击查询", **base)
        unclicked = SearchLog(query="=HYPERLINK(\"样本\")", **base)
        session.add_all([clicked, unclicked])
        session.flush()
        session.add(SearchClick(search_id=clicked.id, user_id=user_id, target_kind="product", target_id=1))
    logs = flow.call("admin", "GET", "/api/admin/search-logs")
    assert {row["clicks"] for row in logs.json()["items"]} == {0, 1}
    result = flow.call("admin", "GET", "/api/admin/search-logs/export")
    assert result.status_code == 200 and "点击查询" not in result.text
    assert "'=HYPERLINK" in result.text
    assert flow.call("other", "GET", "/api/admin/search-logs").status_code == 403
