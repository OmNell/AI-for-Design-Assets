"""WP07 核对、WP08 双审到 WP09 正式发布的模拟 NAS 回归。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.config import Settings
from app.db.database import Database
from app.db.models import Batch, BatchProduct, CatalogFile, HistoryVersion, IngestPlan, Job, Link, Product, ProductEmbedding, Project
from app.main import create_app
from app.services import archive
from app.services.jobs import JobFailure
from app.services.nas import Region
from app.services.nas.factory import create_paths
from test_two_stage_review import ALL, CATALOG, Flow, configure


def app_for(settings: Settings, tmp_path: Path):
    configure(settings, tmp_path)
    for actor in settings.dev_users:
        actor.permissions["formal"] = "write" if actor.username == "final" else "read"
    settings.server.public_base_url = "http://assets.example.local:8080"
    return create_app(settings, log_dir=tmp_path / "logs")


def start(flow: Flow):
    assert flow.call("admin", "PUT", "/api/admin/company-map", CATALOG).status_code == 200
    for reviewer in ("first", "final"):
        assert flow.audit(reviewer, "claim").status_code == 200
        assert flow.audit(reviewer, "pass", ALL).status_code == 200


def progress(flow: Flow, actor: str = "final") -> dict:
    response = flow.call(actor, "GET", flow.base + "/ingest")
    assert response.status_code == 200, response.text
    return response.json()


def resume(flow: Flow):
    response = flow.call("final", "POST", flow.base + "/resume-ingest", {})
    assert response.status_code == 200, response.text
    assert flow.client.app.state.jobs.wait_idle(8)
    return progress(flow)


def test_full_ingest_preserves_links_and_restores_from_nas(settings, tmp_path):
    app = app_for(settings, tmp_path)
    with TestClient(app) as client:
        flow = Flow(client, ["单品/Z.jpg", "单品/A.jpg", "套装/S.jpg", "01定稿文件/层/资料.ai"])
        start(flow)
        assert app.state.jobs.wait_idle(8)
        data = progress(flow)
        assert (data["status"], data["step"], data["vectors_ready"], data["files"]) == ("已入库", 9, 3, 4), data
        assert flow.call("other", "GET", flow.base + "/ingest").status_code == 403
        final_session = next(row for row in app.state.auth.pool._sessions.values() if row.permissions.get(Region.FORMAL) == "write")
        paths = create_paths(settings)
        with app.state.database.session() as session:
            project = session.get(Project, session.get(Batch, flow.bid).project_id)
            assert project.status == "active"
            source = paths.join(Region.FORMAL, project.current_path)
            assert archive.verify(session, project, final_session, source) == []
            saved = archive.parse(final_session, source)
            products = sorted(saved["products"], key=lambda row: row["product_code"])
            assert [p["display_name"] for p in products] == ["A", "Z", "S"]
            assert len({f["id"] for f in saved["files"]}) == 4
            assert len({f["fingerprint"] for f in saved["files"]}) == 1
            docs = archive.documents(saved)
            assert saved["relations"][0]["set_product_id"] == saved["relations"][1]["set_product_id"]
            assert "所属套装" in docs[products[0]["archive_rel_path"]]
            assert "包含单品" in docs[products[-1]["archive_rel_path"]]
            assert len(saved["tags"]) == 33
            assert len(list(session.scalars(select(ProductEmbedding)))) == 3
            assert final_session.exists(source + "/01定稿文件/层/资料.ai")
            for name in ("02设计方案", "03拍摄照片及视频", "04授权文件", "05 详情页"):
                assert final_session.stat(source + "/" + name).is_dir
        link = next(row for row in saved["links"] if row["kind"] == "product")
        response = flow.call("other", "GET", "/l/" + link["link_id"])
        assert response.status_code == 200 and response.json()["target_id"] == link["target_id"]
        assert response.json()["url"].startswith("http://assets.example.local:8080/l/")
        assert flow.call("other", "GET", "/l/does-not-exist").status_code == 404
        relative = project.current_path.rsplit("/", 1)[0] + "/P001_企业_改名"
        destination = paths.join(Region.FORMAL, relative)
        final_session.rename(source, destination)
        with app.state.database.session() as session:
            session.get(Project, project.id).current_path = relative
        assert flow.call("other", "GET", "/l/" + link["link_id"]).json() == response.json()
        # 空的新数据库只用 NAS 档案重建，原 SQLite 对象并未参与恢复。
        recovered = Database(tmp_path / "recovered")
        recovered.migrate()
        try:
            with recovered.session() as session:
                rebuilt = archive.restore(session, final_session, destination, relative)
                assert rebuilt.id == project.id and rebuilt.status == "ingesting"
                assert {(r.kind, r.target_id, r.link_id) for r in session.scalars(select(Link))} == {
                    (r["kind"], r["target_id"], r["link_id"]) for r in saved["links"]}
                assert {p.id for p in session.scalars(select(Product))} == {r["id"] for r in saved["products"]}
        finally:
            recovered.close()


@pytest.mark.parametrize("checkpoint", range(1, 9))
def test_each_durable_checkpoint_can_resume(settings, tmp_path, monkeypatch, checkpoint):
    app = app_for(settings, tmp_path)
    with TestClient(app) as client:
        flow = Flow(client)
        run = app.state.ingest._run_step

        def interrupted(context, fs, step):
            run(context, fs, step)
            if step == checkpoint:
                raise JobFailure("测试中断", retryable=False)

        with monkeypatch.context() as patch:
            patch.setattr(app.state.ingest, "_run_step", interrupted)
            start(flow)
            assert app.state.jobs.wait_idle(8)
        before = progress(flow)
        assert before["status"] == "入库异常" and before["step"] == checkpoint, before
        with app.state.database.session() as session:
            plan = session.get(IngestPlan, flow.bid)
            manifest = plan.manifest if plan else None
        assert flow.call("admin", "POST", flow.base + "/resume-ingest", {}).status_code == 403
        assert resume(flow)["status"] == "已入库"
        assert flow.call("final", "POST", flow.base + "/resume-ingest", {}).status_code == 409
        with app.state.database.session() as session:
            plan = session.get(IngestPlan, flow.bid)
            assert not manifest or plan.manifest == manifest
            assert len(list(session.scalars(select(Product)))) == 1
            assert len(list(session.scalars(select(Link)))) == 3


def test_rename_before_sql_checkpoint_and_two_resume_requests(settings, tmp_path, monkeypatch):
    app = app_for(settings, tmp_path)
    with TestClient(app) as client:
        flow = Flow(client)
        move = app.state.ingest._move

        def interrupt(fs, execution):
            fs.rename(execution.source, execution.target)
            raise JobFailure("移动后连接终止", retryable=False)

        def interrupted(fs, execution, context):
            archive.write(fs, execution.source, {"_工作台入库校验": execution.batch.payload["ingest_move_id"]})
            interrupt(fs, execution)

        with monkeypatch.context() as patch:
            patch.setattr(app.state.ingest, "_move", interrupted)
            start(flow)
            assert app.state.jobs.wait_idle(8)
        assert progress(flow)["step"] == 2
        assert app.state.ingest._move == move
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: flow.call("final", "POST", flow.base + "/resume-ingest", {}).status_code, range(2)))
        assert 200 in responses and set(responses) <= {200, 409}
        assert app.state.jobs.wait_idle(8) and progress(flow)["status"] == "已入库"
        with app.state.database.session() as session:
            assert len(list(session.scalars(select(Job).where(Job.kind == "ingest")))) == 2


def test_partial_vectors_and_restart_only_retry_missing(settings, tmp_path, monkeypatch):
    app = app_for(settings, tmp_path)
    with TestClient(app) as client:
        flow = Flow(client, ["单品/A.jpg", "单品/向量失败.jpg"])
        start(flow)
        assert app.state.jobs.wait_idle(8)
        state = progress(flow)
        assert (state["status"], state["step"], state["vectors_ready"]) == ("入库异常", 8, 1), state
        with app.state.database.session() as session:
            success = session.scalar(select(ProductEmbedding).where(ProductEmbedding.state == "ready"))
            success_id, prior_blob = success.product_id, success.embedding
            link = session.scalar(select(Link.link_id))
        assert flow.call("other", "GET", "/l/" + link).status_code == 404
        assert flow.call("one", "GET", flow.base + "/ingest").json()["status"] == "入库异常"
    restarted = app_for(settings, tmp_path)
    with TestClient(restarted) as client:
        flow.client = client
        flow.sign_in("final")
        fake = restarted.state.ai.client()
        monkeypatch.setattr(fake, "embed", lambda text, batch: [0.25] * 32)
        assert resume(flow)["status"] == "已入库"
        with restarted.state.database.session() as session:
            assert [(row.state, row.attempts) for row in session.scalars(
                select(ProductEmbedding).where(ProductEmbedding.product_id != success_id))] == [("ready", 2)]
            row = session.scalar(select(ProductEmbedding).where(ProductEmbedding.product_id == success_id))
            assert row.embedding == prior_blob and row.attempts == 1


def test_sha_mismatch_prevents_any_formal_write(settings, tmp_path):
    app = app_for(settings, tmp_path)
    with TestClient(app) as client:
        flow = Flow(client)
        uploaded = next(settings.paths.dev_nas_dir.rglob("杯.jpg"))
        original = uploaded.read_bytes()
        uploaded.write_bytes(b"x" + original[1:])
        start(flow)
        assert app.state.jobs.wait_idle(8)
        data = progress(flow)
        assert data["step"] == 0 and "SHA-256" in data["error"], data
        assert not list((settings.paths.dev_nas_dir / create_paths(settings).formal).iterdir())
        uploaded.write_bytes(original)
        assert resume(flow)["status"] == "已入库"


def test_session_loss_requires_new_login_by_final_reviewer(settings, tmp_path, monkeypatch):
    app = app_for(settings, tmp_path)
    with TestClient(app) as client:
        flow = Flow(client)
        pin = app.state.jobs._pin_actor
        allow = Event()

        def lose_session(job):
            if job.kind == "ingest" and not allow.is_set():
                from app.services.nas import NasSessionExpired
                raise NasSessionExpired("测试断开")
            return pin(job)

        with monkeypatch.context() as patch:
            patch.setattr(app.state.jobs, "_pin_actor", lose_session)
            start(flow)
            assert app.state.jobs.wait_idle(8)
            assert progress(flow)["error"] == "操作人会话已失效"
            assert flow.call("final", "POST", "/api/auth/logout").status_code == 200
            flow.sign_in("final")
            allow.set()
            assert resume(flow)["status"] == "已入库"


def test_allocation_rollback_and_partial_archive_are_recoverable(settings, tmp_path, monkeypatch):
    app = app_for(settings, tmp_path)
    with TestClient(app) as client:
        flow = Flow(client, ["单品/A.jpg", "单品/B.jpg"])
        plan = app.state.ingest._plan

        def fail_before_commit(session, execution, times):
            plan(session, execution, times)
            session.flush()
            raise JobFailure("模拟发号时断电", retryable=False)

        with monkeypatch.context() as patch:
            patch.setattr(app.state.ingest, "_plan", fail_before_commit)
            start(flow)
            assert app.state.jobs.wait_idle(8) and progress(flow)["step"] == 3
        with app.state.database.session() as session:
            assert session.get(IngestPlan, flow.bid) is None
            assert list(session.scalars(select(BatchProduct.product_code))) == [None, None]

        write = archive.write

        def interrupt_archive(fs, root, docs, **kwargs):
            images = list(docs.items())
            if len(images) == 2:
                write(fs, root, dict(images[:1]))
                with fs.open_write(root + "/" + images[1][0] + ".tmp") as stream:
                    stream.write(b"partial json")
                raise JobFailure("模拟商品档案写入中断", retryable=False)
            write(fs, root, docs, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(archive, "write", interrupt_archive)
            assert resume(flow)["step"] == 4
        with app.state.database.session() as session:
            saved_ids = session.get(IngestPlan, flow.bid).manifest["links"]
            assert list(session.scalars(select(Product))) == []
        assert resume(flow)["status"] == "已入库"
        assert not list(settings.paths.dev_nas_dir.rglob("*.json.tmp"))
        with app.state.database.session() as session:
            assert session.get(IngestPlan, flow.bid).manifest["links"] == saved_ids


def test_formal_permission_denied_even_with_role(settings, tmp_path):
    configure(settings, tmp_path)
    with TestClient(create_app(settings, log_dir=tmp_path / "logs")) as client:
        flow = Flow(client)
        start(flow)
        assert client.app.state.jobs.wait_idle(8)
        error = progress(flow)
        assert error["step"] == 0 and "权限" in error["error"], error
        with client.app.state.database.session() as session:
            job = session.scalar(select(Job).where(Job.kind == "ingest"))
            response = flow.call("admin", "POST", f"/api/admin/jobs/{job.id}/retry", {})
            assert response.json()["code"] == "ingest_resume_required"
        assert not list((settings.paths.dev_nas_dir / create_paths(settings).formal).iterdir())


def test_history_file_link_survives_archive_rebuild(settings, tmp_path):
    app = app_for(settings, tmp_path)
    with TestClient(app) as client:
        flow = Flow(client)
        start(flow)
        assert app.state.jobs.wait_idle(8) and progress(flow)["status"] == "已入库"
        paths = create_paths(settings)
        with app.state.database.session() as session:
            batch = session.get(Batch, flow.bid)
            fs = next(fs for fs in app.state.auth.pool._sessions.values() if fs.permissions.get(Region.FORMAL) == "write")
            project = session.get(Project, batch.project_id)
            root = paths.join(Region.FORMAL, project.current_path)
            old = session.scalar(select(CatalogFile))
            product = session.scalar(select(Product))
            history_path = "_历史版本/杯-old.jpg"
            from app.db.models import utcnow
            fs.rename(root + "/" + old.rel_path, root + "/" + history_path)
            with fs.open_read(root + "/" + history_path) as source, fs.open_write(root + "/" + old.rel_path) as dest:
                dest.write(source.read())
            replacement = CatalogFile(project_id=project.id, rel_path=old.rel_path, size=old.size, mtime=old.mtime,
                fingerprint=old.fingerprint, category=old.category, first_batch_id=batch.id)
            session.add(replacement)
            session.flush()
            old.status = "replaced"
            product.file_id = replacement.id
            session.add_all([HistoryVersion(project_id=project.id, file_id=old.id, product_id=product.id,
                old_fingerprint=old.fingerprint, old_rel_path=old.rel_path, new_rel_path=history_path, reason="replaced"),
                Link(kind="file", target_id=replacement.id, link_id="replacement_link")])
            file_link = session.scalar(select(Link.link_id).where(Link.kind == "file", Link.target_id == old.id))
            product_link = session.scalar(select(Link.link_id).where(Link.kind == "product"))
            old_id, new_id, relative = old.id, replacement.id, project.current_path
            project.archive_updated_at = utcnow()
            session.flush()
            archive.rebuild(session, project, fs, root)
            assert archive.verify(session, project, fs, root) == []
        assert flow.call("other", "GET", "/l/" + file_link).json()["target_id"] == old_id
        assert flow.call("other", "GET", "/l/" + product_link).json()["image_file_id"] == new_id
        rebuilt = Database(tmp_path / "restored-history")
        rebuilt.migrate()
        try:
            with rebuilt.session() as session:
                project = archive.restore(session, fs, root, relative)
                assert archive.verify(session, project, fs, root) == []
                assert session.scalar(select(HistoryVersion)).file_id == old_id
                assert session.scalar(select(Link.target_id).where(Link.link_id == file_link)) == old_id
        finally:
            rebuilt.close()
