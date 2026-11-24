"""LocalFs 上核对工作台公开版本、向量门禁和受控删除。"""
from datetime import timedelta
from threading import Event

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Batch, CatalogFile, EmbeddingIndex, ExpectedDeletion, HistoryVersion, Link, PostopPlan, Product, ProductEmbedding, ProductEmbeddingHistory, Project, utcnow
from app.services import archive
from app.services.ai.client import AiCallError, FakeAiClient
from app.services.jobs import JobFailure
from app.services.nas import LocalFs, Region
from app.services.nas.factory import create_paths
from app.services.postops import embeddings
from app.services.postops.shadow import ShadowTree
from test_ingest import app_for, start
from test_two_stage_review import ALL, Flow


def initialized(client, paths=None):
    flow = Flow(client, paths)
    start(flow)
    assert client.app.state.jobs.wait_idle(8)
    with client.app.state.database.session() as session:
        project = session.get(Project, session.get(Batch, flow.bid).project_id)
        product = session.scalar(select(Product).where(Product.project_id == project.id, Product.kind == "item"))
        item_link = session.scalar(select(Link.link_id).where(Link.kind == "product", Link.target_id == product.id))
        assert project.status == "active"
    return flow, project, product, item_link


def create(flow, project, kind, params, actor="one"):
    response = flow.call(actor, "POST", f"/api/projects/{project.id}/ops/{kind}", {"params": params})
    assert response.status_code == 200, response.text
    return response.json()["batch_id"]


def submit(flow, batch_id, params, actor="one"):
    response = flow.call(actor, "POST", f"/api/batches/{batch_id}/operation/submit", {"params": params})
    assert response.status_code == 200, response.text
    flow.base = f"/api/review/{batch_id}"
    return response.json()


def approve(flow, first="first", final="final"):
    for actor in (first, final):
        claimed = flow.audit(actor, "claim")
        assert claimed.status_code == 200, claimed.text
        passed = flow.audit(actor, "pass", ALL)
        assert passed.status_code == 200, passed.text


def state(flow, batch_id, actor="final"):
    response = flow.call(actor, "GET", f"/api/operations/{batch_id}/execution")
    assert response.status_code == 200, response.text
    return response.json()


def interrupt_after_install(patch):
    install = ShadowTree.install

    def interrupted(tree):
        install(tree)
        raise JobFailure("测试安装正式版本后中断", retryable=False)

    patch.setattr(ShadowTree, "install", interrupted)


def test_retag_keeps_old_version_until_vector_is_ready_and_resumes(settings, tmp_path, monkeypatch):
    app = app_for(settings, tmp_path)
    with TestClient(app) as client:
        flow, project, product, link = initialized(client)
        with app.state.database.session() as session:
            old_index = session.scalar(select(ProductEmbedding).where(ProductEmbedding.product_id == product.id)).text_hash
        params = {"products": [{"id": product.id, "name": "用于检索的新名称", "free_text": "已审核描述"}]}
        batch_id = create(flow, project, "retag", params)
        submit(flow, batch_id, params)
        went_in, release = Event(), Event()

        def do_not_embed(*args):
            went_in.set()
            assert release.wait(8)
            raise JobFailure("测试向量失败", retryable=False)

        with monkeypatch.context() as patch:
            patch.setattr(embeddings, "prepare", do_not_embed)
            approve(flow)
            try:
                assert went_in.wait(4), state(flow, batch_id)
                with app.state.database.session() as session:
                    assert session.get(Project, project.id).current_path == project.current_path
                    assert session.get(Product, product.id).display_name == product.display_name
                    assert session.scalar(select(ProductEmbedding).where(ProductEmbedding.product_id == product.id)).text_hash == old_index
                assert flow.call("other", "GET", "/l/" + link).status_code == 200
            finally:
                release.set()
            assert app.state.jobs.wait_idle(8)
        assert state(flow, batch_id)["status"] == "入库异常"
        assert flow.call("admin", "POST", f"/api/operations/{batch_id}/execute", {}).status_code == 403
        assert flow.call("final", "POST", f"/api/operations/{batch_id}/execute", {}).status_code == 200
        assert app.state.jobs.wait_idle(8)
        assert state(flow, batch_id)["status"] == "已入库"
        with app.state.database.session() as session:
            current = session.get(Project, project.id)
            assert current.current_path != project.current_path and current.asset_version == 1
            assert session.get(Product, product.id).display_name == "用于检索的新名称"
            assert session.scalar(select(ProductEmbedding).where(ProductEmbedding.product_id == product.id)).text_hash != old_index
            prior = session.scalar(select(ProductEmbeddingHistory).where(ProductEmbeddingHistory.product_id == product.id))
            assert prior.text_hash == old_index
            fs = app.state.auth.pool.system()
            assert archive.verify(session, current, fs, create_paths(settings).join(Region.FORMAL, current.current_path)) == []
        assert flow.call("other", "GET", "/l/" + link).json()["target_id"] == product.id


def test_whole_project_void_then_restore_from_split_install(settings, tmp_path, monkeypatch):
    app = app_for(settings, tmp_path)
    with TestClient(app) as client:
        flow, project, product, link = initialized(client, ["单品/杯.jpg", "套装/组.jpg"])
        params = {"project": True, "reason": "退档"}
        batch_id = create(flow, project, "void", params)
        submit(flow, batch_id, params)
        with monkeypatch.context() as patch:
            interrupt_after_install(patch)
            approve(flow)
            assert app.state.jobs.wait_idle(8)
        assert state(flow, batch_id)["status"] == "入库异常"
        assert flow.call("other", "GET", "/l/" + link).json()["status"] == "active"
        assert flow.call("final", "POST", f"/api/operations/{batch_id}/execute", {}).status_code == 200
        assert app.state.jobs.wait_idle(8)
        assert state(flow, batch_id)["status"] == "已入库"
        assert flow.call("other", "GET", "/l/" + link).json()["status"] == "voided"
        with app.state.database.session() as session:
            row = session.get(Project, project.id)
            assert row.current_path.startswith("_已作废项目/") and row.restore_path == project.current_path
        params["reason"] = "重新使用"
        batch_id = create(flow, project, "restore", params)
        submit(flow, batch_id, params)
        with monkeypatch.context() as patch:
            interrupt_after_install(patch)
            approve(flow)
            assert app.state.jobs.wait_idle(8)
        assert state(flow, batch_id)["status"] == "入库异常"
        assert flow.call("other", "GET", "/l/" + link).json()["status"] == "voided"
        assert flow.call("final", "POST", f"/api/operations/{batch_id}/execute", {}).status_code == 200
        assert app.state.jobs.wait_idle(8)
        assert state(flow, batch_id)["status"] == "已入库"
        with app.state.database.session() as session:
            row = session.get(Project, project.id)
            assert row.canonical_path == project.current_path and row.status == "active"
            assert session.get(Product, product.id).status == "active"
        assert flow.call("other", "GET", "/l/" + link).json()["status"] == "active"


def test_purge_requires_cooling_and_two_confirmations_and_removes_approved_copies(settings, tmp_path, monkeypatch):
    app = app_for(settings, tmp_path)
    with TestClient(app) as client:
        flow, project, product, link = initialized(client, ["单品/杯.jpg", "01定稿文件/保留.ai"])
        params = {"product_ids": [product.id], "reason": "删除测试"}
        batch_id = create(flow, project, "void", params)
        submit(flow, batch_id, params)
        approve(flow)
        assert app.state.jobs.wait_idle(8)
        assert state(flow, batch_id)["status"] == "已入库"
        actor = "admin"
        draft = create(flow, project, "purge", params, actor)
        rejected = flow.call(actor, "POST", f"/api/batches/{draft}/operation/submit",
                             {"params": {**params, "confirm": product.product_code}})
        assert rejected.status_code == 409
        with app.state.database.session() as session:
            # 仅测试时钟推进，不修改同一批生产资产。
            for row in (session.get(Product, product.id), session.get(CatalogFile, product.file_id)):
                row.voided_at = utcnow() - timedelta(days=30, seconds=1)
        params["confirm"] = product.product_code
        still_cooling = flow.call(actor, "POST", f"/api/batches/{draft}/operation/submit", {"params": params})
        assert still_cooling.status_code == 409  # 作废准备早于发布时，以实际发布时间为准。
        with app.state.database.session() as session:
            completed = session.get(PostopPlan, batch_id)
            completed.progress = {**completed.progress, "published_at": (utcnow() - timedelta(days=30, seconds=1)).isoformat()}
        submit(flow, draft, params, actor)
        approve(flow)
        pending = state(flow, draft)
        assert pending["can_resume"] and pending["confirmation_code"] == product.product_code
        assert len(pending["purge_files"]) == 2
        assert flow.call("other", "GET", "/l/" + link).json()["status"] == "voided"
        assert flow.call("final", "POST", f"/api/operations/{draft}/execute", {"confirm_code": "错误"}).status_code == 422
        delete = LocalFs.delete

        def interrupted(fs, path, recursive=False):
            delete(fs, path, recursive=recursive)
            if "/删除-" in path:
                raise JobFailure("测试删除后、登记完成前中断", retryable=False)

        with monkeypatch.context() as patch:
            patch.setattr(LocalFs, "delete", interrupted)
            assert flow.call("final", "POST", f"/api/operations/{draft}/execute",
                             {"confirm_code": product.product_code}).status_code == 200
            assert app.state.jobs.wait_idle(8)
            assert state(flow, draft)["status"] == "入库异常"
        with app.state.database.session() as session:
            intents = list(session.scalars(select(ExpectedDeletion).where(ExpectedDeletion.batch_id == draft)))
            assert len(intents) == 2 and sum(row.quarantined_at is not None for row in intents) == 1
            assert all(row.completed_at is None for row in intents)
        assert flow.call("final", "POST", f"/api/operations/{draft}/execute",
                         {"confirm_code": product.product_code}).status_code == 200
        assert app.state.jobs.wait_idle(8)
        assert state(flow, draft)["status"] == "已入库"
        resolved = flow.call("other", "GET", "/l/" + link).json()
        assert resolved["status"] == "deleted" and resolved["delete_reason"] == "删除测试"
        with app.state.database.session() as session:
            assert session.get(Product, product.id).status == "deleted"
            records = list(session.scalars(select(ExpectedDeletion).where(ExpectedDeletion.batch_id == draft)))
            assert len(records) == 2 and all(r.completed_at for r in records)
            assert session.scalar(select(HistoryVersion).where(HistoryVersion.file_id == product.file_id)).expected_missing
            current = session.get(Project, project.id)
            fs = app.state.auth.pool.system()
            paths = create_paths(settings)
            assert all(not fs.exists(paths.join(Region.FORMAL, r.path)) for r in records)
            assert fs.exists(paths.join(Region.FORMAL, current.current_path, "01定稿文件/保留.ai"))
            assert archive.verify(session, current, fs, paths.join(Region.FORMAL, current.current_path)) == []


def test_move_preserves_identity_and_reuses_vectors(settings, tmp_path, monkeypatch):
    app = app_for(settings, tmp_path)
    with TestClient(app) as client:
        flow, project, product, link = initialized(client)
        calls: list[str] = []
        embed = FakeAiClient.embed

        def tracked(self, text, batch_id):
            calls.append(text)
            return embed(self, text, batch_id)

        monkeypatch.setattr(FakeAiClient, "embed", tracked)
        params = {"company_id": project.company_id, "won": False, "reason": "核准中标状态错误"}
        batch_id = create(flow, project, "move", params, "admin")
        submit(flow, batch_id, params, "admin")
        with monkeypatch.context() as patch:
            interrupt_after_install(patch)
            approve(flow)
            assert app.state.jobs.wait_idle(8)
        assert state(flow, batch_id)["status"] == "入库异常"
        with app.state.database.session() as session:
            assert session.get(Project, project.id).current_path == project.current_path
        assert flow.call("final", "POST", f"/api/operations/{batch_id}/execute", {}).status_code == 200
        assert app.state.jobs.wait_idle(8)
        assert state(flow, batch_id)["status"] == "已入库"
        with app.state.database.session() as session:
            moved = session.get(Project, project.id)
            assert moved.current_path.startswith("02未中标项目/") and moved.won is False
            assert session.get(Product, product.id).file_id == product.file_id
        assert flow.call("other", "GET", "/l/" + link).json()["target_id"] == product.id
        assert calls == []


def test_candidate_failure_does_not_publish_stale_formal_text(settings, tmp_path, monkeypatch):
    app = app_for(settings, tmp_path)
    with TestClient(app) as client:
        flow, project, product, _ = initialized(client)
        response = flow.call("admin", "POST", "/api/admin/vector-indexes", {"name": "fake-next", "revision": "wp12"})
        assert response.status_code == 202, response.text
        assert app.state.jobs.wait_idle(8)
        embed = FakeAiClient.embed

        def candidate_error(self, text, batch_id):
            if self.config.model_embed == "fake-next":
                raise AiCallError("测试候选索引失败")
            return embed(self, text, batch_id)

        monkeypatch.setattr(FakeAiClient, "embed", candidate_error)
        params = {"products": [{"id": product.id, "name": "已核验的新商品名称"}]}
        batch_id = create(flow, project, "retag", params)
        submit(flow, batch_id, params)
        approve(flow)
        assert app.state.jobs.wait_idle(8)
        assert state(flow, batch_id)["status"] == "已入库"
        with app.state.database.session() as session:
            candidate = session.scalar(select(EmbeddingIndex).where(EmbeddingIndex.status == "candidate"))
            rows = {row.index_generation: row for row in session.scalars(select(ProductEmbedding).where(
                ProductEmbedding.product_id == product.id))}
            formal, alternate = rows[1], rows[candidate.generation]
            assert formal.state == "ready" and alternate.state == "failed"
            assert formal.text_hash == alternate.text_hash
            assert session.get(Product, product.id).display_name == "已核验的新商品名称"


def test_revoked_final_device_pauses_and_only_same_final_can_resume(settings, tmp_path, monkeypatch):
    app = app_for(settings, tmp_path)
    with TestClient(app) as client:
        flow, project, _, _ = initialized(client)
        params = {"project_name": "重命名后"}
        batch_id = create(flow, project, "rename", params)
        submit(flow, batch_id, params)
        reached, continue_job = Event(), Event()
        prepare = ShadowTree.prepare

        def wait_before_writes(tree):
            reached.set()
            assert continue_job.wait(8)
            prepare(tree)

        with monkeypatch.context() as patch:
            patch.setattr(ShadowTree, "prepare", wait_before_writes)
            approve(flow)
            try:
                assert reached.wait(8)
                assert flow.call("final", "POST", "/api/auth/logout").status_code in (200, 204)
            finally:
                continue_job.set()
            assert app.state.jobs.wait_idle(8)
        assert state(flow, batch_id, "one")["status"] == "入库异常"
        with app.state.database.session() as session:
            assert session.get(Project, project.id).current_path == project.current_path
        flow.sign_in("final")
        assert flow.call("first", "POST", f"/api/operations/{batch_id}/execute", {}).status_code == 403
        assert flow.call("final", "POST", f"/api/operations/{batch_id}/execute", {}).status_code == 200
        assert app.state.jobs.wait_idle(8)
        assert state(flow, batch_id)["status"] == "已入库"
