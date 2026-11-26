"""仅使用隔离 SQLite 和模拟 NAS，验证正式档案增量恢复。"""
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import delete, select
import yaml

from app.db.database import Database
from app.db.models import CatalogFile, Link, Product, ProductEmbedding, ProductTag, Project, Setting
from app.main import create_app
from app.services import archive
from app.services.ai.client import AiCallError, FakeAiClient
from app.services.archive import publication
from app.services.crypto import SecretCipher
from app.services.nas import NasPermissionDenied, NasUnavailable, Region
from app.services.nas.factory import create_paths
from app.services.recovery.incremental import apply_incremental, plan_incremental
from test_catalog_access import published
from test_maintenance import maintained
from test_postop_execution import approve, create, state, submit

ROOT = Path(__file__).resolve().parents[3]


@contextmanager
def new_environment(settings, name, tmp_path):
    recovered = settings.model_copy(deep=True)
    recovered.paths.data_dir = tmp_path / name
    with TestClient(create_app(recovered, log_dir=tmp_path / (name + "-logs"))) as client:
        yield client


def finish_change(flow, project, kind, params):
    batch = create(flow, project, kind, params)
    submit(flow, batch, params)
    approve(flow)
    assert flow.client.app.state.jobs.wait_idle(8)
    outcome = state(flow, batch)
    assert outcome["status"] == "已入库", outcome


def item(plan, project_id):
    return next(row for row in plan["items"] if row["project_id"] == project_id)


def test_archive_restore_old_database_advances_and_preserves_stable_ids(maintained, tmp_path, monkeypatch):
    flow, project, settings, _ = maintained
    source = flow.client.app
    fs = source.state.auth.pool.system()
    root = create_paths(settings).root(Region.FORMAL)
    with source.state.database.session() as session:
        before = archive.from_catalog(session, session.get(Project, project.id))
    with new_environment(settings, "restored-db", tmp_path) as restored:
        target = restored.app.state
        plan = plan_incremental(target.database, fs, root)
        assert item(plan, project.id)["kind"] == "missing"
        result = apply_incremental(target.ai, fs, root, plan)
        assert result["recovered"] == [project.id] and not result["blocked"]
        assert apply_incremental(target.ai, fs, root, plan)["skipped"] == [project.id]

        display = "人工确认新名称"
        product = before["products"][0]
        finish_change(flow, project, "retag", {"products": [{"id": product["id"], "name": display}]})
        with source.state.database.session() as session:
            after = archive.from_catalog(session, session.get(Project, project.id))
        plan = plan_incremental(target.database, fs, root)
        assert item(plan, project.id)["kind"] == "newer"

        original = FakeAiClient.embed
        with monkeypatch.context() as patch:
            def fail(*args):
                raise AiCallError("模拟向量重建异常")

            patch.setattr(FakeAiClient, "embed", fail)
            result = apply_incremental(target.ai, fs, root, plan)
            assert [row["project_id"] for row in result["blocked"]] == [project.id]
            with target.database.session() as session:
                assert archive.matches_catalog(session, session.get(Project, project.id), before)
                index = session.scalar(select(ProductEmbedding))
                assert index.state == "ready"
        assert FakeAiClient.embed is original
        assert apply_incremental(target.ai, fs, root, plan)["recovered"] == [project.id]
        with target.database.session() as session:
            assert archive.matches_catalog(session, session.get(Project, project.id), after)
            assert session.get(Product, product["id"]).display_name == display
        assert item(plan_incremental(target.database, fs, root), project.id)["kind"] == "same"


def test_missing_records_can_be_filled_but_local_divergence_is_kept(published, settings, tmp_path):
    flow, project, _, _, products, _ = published
    source = flow.client.app.state
    fs = source.auth.pool.system()
    root = create_paths(settings).root(Region.FORMAL)
    pid = products["item"].id
    with new_environment(settings, "partial-db", tmp_path) as restored:
        target = restored.app.state
        first = plan_incremental(target.database, fs, root)
        assert apply_incremental(target.ai, fs, root, first)["recovered"] == [project.id]
        with target.database.session() as session:
            session.execute(delete(ProductTag).where(ProductTag.product_id == pid))
            session.execute(delete(Link).where(Link.kind == "product", Link.target_id == pid))
            session.execute(delete(ProductEmbedding).where(ProductEmbedding.product_id == pid))
        plan = plan_incremental(target.database, fs, root)
        assert item(plan, project.id)["kind"] == "incomplete"
        result = apply_incremental(target.ai, fs, root, plan)
        assert result["recovered"] == [project.id], result
        with source.database.session() as session:
            manifest = archive.from_catalog(session, session.get(Project, project.id))
        with target.database.session() as session:
            assert archive.matches_catalog(session, session.get(Project, project.id), manifest)
            assert session.scalar(select(ProductEmbedding).where(ProductEmbedding.product_id == pid)).state == "ready"
            session.get(Product, pid).display_name = "有待核实的本地变化"
        assert item(plan_incremental(target.database, fs, root), project.id)["kind"] == "conflict"
        with target.database.session() as session:
            assert session.get(Product, pid).display_name == "有待核实的本地变化"


def test_active_batch_and_uncertain_nas_never_trigger_restore(maintained, tmp_path, monkeypatch):
    flow, project, settings, _ = maintained
    source = flow.client.app.state
    fs = source.auth.pool.system()
    root = create_paths(settings).root(Region.FORMAL)
    create(flow, project, "rename", {"project_name": "保留本地草稿"})
    existing = plan_incremental(source.database, fs, root)
    assert item(existing, project.id)["reason"] == "此项目或编号已有未完成业务批次，保留原批次"

    ordinary = settings.paths.dev_nas_dir / root / "普通文件夹"
    ordinary.mkdir()
    original = fs.list

    def restricted(path):
        if path == f"{root}/普通文件夹":
            raise NasPermissionDenied("模拟文件夹 ACL")
        return original(path)

    monkeypatch.setattr(fs, "list", restricted)
    with new_environment(settings, "uncertain-db", tmp_path) as restored:
        target = restored.app.state
        plan = plan_incremental(target.database, fs, root)
        assert item(plan, project.id)["kind"] == "missing"
        assert plan["inaccessible"] == [{"path": "普通文件夹", "reason": "目录不存在或系统账号无法读取"}]
        with monkeypatch.context() as patch:
            def disconnected(path):
                raise NasUnavailable("模拟连接中断")

            patch.setattr(fs, "list", disconnected)
            with pytest.raises(NasUnavailable):
                apply_incremental(target.ai, fs, root, plan)
        receipt = settings.paths.dev_nas_dir / root / project.current_path / publication.RECEIPT
        receipt.unlink()
        result = apply_incremental(target.ai, fs, root, plan)
        assert not result["recovered"] and result["unresolved"] and result["blocked"]
        with target.database.session() as session:
            assert session.get(Project, project.id) is None


def test_completed_vectors_resume_and_voided_asset_stays_voided(published, settings, tmp_path, monkeypatch):
    flow, project, _, _, products, links = published
    fs = flow.client.app.state.auth.pool.system()
    root = create_paths(settings).root(Region.FORMAL)
    voided = products["item"]
    finish_change(flow, project, "void", {"product_ids": [voided.id], "reason": "已审核作废"})
    with new_environment(settings, "resumed-db", tmp_path) as restored:
        target = restored.app.state
        plan = plan_incremental(target.database, fs, root)
        called = []
        original = FakeAiClient.embed

        def unstable(client, text, batch_id):
            called.append(text)
            if len(called) == 2:
                raise AiCallError("模拟单个商品向量失败")
            return original(client, text, batch_id)

        monkeypatch.setattr(FakeAiClient, "embed", unstable)
        initial = apply_incremental(target.ai, fs, root, plan)
        assert initial["blocked"] and not initial["recovered"]
        with target.database.session() as session:
            assert session.get(Project, project.id) is None
            assert session.scalar(select(Setting).where(Setting.key.startswith("archive_restore_vector:")))
        assert apply_incremental(target.ai, fs, root, plan)["recovered"] == [project.id]
        assert len(called) == 3 and called[0] != called[-1] == called[1]
        with target.database.session() as session:
            assert session.get(Product, voided.id).status == "voided"
            assert session.get(CatalogFile, voided.file_id).status == "voided"
            assert not session.scalar(select(Setting).where(Setting.key.startswith("archive_restore_vector:")))
            assert session.scalar(select(Link).where(Link.link_id == links["product", voided.id])).target_id == voided.id


def test_offline_compare_and_approved_incremental_cli(maintained, tmp_path):
    _, project, settings, _ = maintained
    directory = tmp_path / "offline-data"
    config = tmp_path / "offline.yml"
    config.write_text(yaml.safe_dump({"mode": "dev", "paths": {
        "data_dir": str(directory), "dev_nas_dir": str(settings.paths.dev_nas_dir),
    }}, allow_unicode=True), encoding="utf-8")
    db = Database(directory)
    db.migrate()
    db.close()
    SecretCipher(directory)
    plan, result = tmp_path / "plan.json", tmp_path / "result.json"
    command = [sys.executable, str(ROOT / "scripts/rebuild_catalog.py"), "--config", str(config), "--compare-data", str(directory)]
    process = subprocess.run([*command, "--report", str(plan)], capture_output=True, text=True)
    assert process.returncode == 0, process.stderr
    assert item(json.loads(plan.read_text()), project.id)["kind"] == "missing"
    args = [*command, "--report", str(result), "--apply-plan", str(plan)]
    process = subprocess.run(args, capture_output=True, text=True)
    assert process.returncode == 2 and not result.exists()
    process = subprocess.run([*args, "--service-stopped"], capture_output=True, text=True)
    assert process.returncode == 0, process.stderr
    assert json.loads(result.read_text())["recovered"] == [project.id]
    with db.session() as session:
        assert session.get(Project, project.id).status == "active"
    db.close()
