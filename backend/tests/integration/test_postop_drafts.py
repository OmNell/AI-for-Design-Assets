"""入库后草稿、上传分片、主图替换和审核内容冻结。"""
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image
import pytest
from sqlalchemy import select

from app.db.models import (AccessLog, AiCall, Batch, BatchProduct, CatalogFile, HistoryVersion, Link, Product,
                           ProductRelation, Project, ReviewRound, ReviewSnapshot)
from app.services.nas import Access, Region
from app.services.nas.factory import create_paths
from test_ai_pipeline import describe_all
from test_ingest import app_for, start
from test_postop_execution import interrupt_after_install
from test_review import confirm_fields
from test_two_stage_review import ALL, Flow


@pytest.fixture
def formal(settings, tmp_path):
    app = app_for(settings, tmp_path)
    with TestClient(app) as client:
        flow = Flow(client, ["单品/旧杯.jpg", "套装/旧套装.jpg", "01定稿文件/旧稿.ai", "05 详情页/需要替换.jpg", "05 详情页/无需上传.jpg"])
        start(flow)
        assert app.state.jobs.wait_idle(8)
        with app.state.database.session() as session:
            project = session.scalar(select(Project))
            assert project.status == "active"
            project_id = project.id
        yield flow, project_id


def image_bytes(color: str) -> bytes:
    target = BytesIO()
    with Image.new("RGB", (1200, 800), color) as image:
        image.save(target, "JPEG")
    return target.getvalue()


def upload(flow: Flow, project_id: int, entries: dict[str, bytes]) -> str:
    flow.sign_in("one")
    client = flow.client
    body = {"folder_name": "P001_企业_本地补充", "won": False, "files": [
        {"rel_path": path, "size": len(data), "mtime": 1} for path, data in entries.items()]}
    response = flow.call("one", "POST", f"/api/projects/{project_id}/ops/supplement/precheck", body)
    assert response.status_code == 200, response.text
    assert response.json()["issues"] == [] and response.json()["project_id"] == project_id
    response = flow.call("one", "POST", f"/api/projects/{project_id}/ops/supplement", body)
    assert response.status_code == 200, response.text
    batch = response.json()
    assert batch["kind"] == "supplement" and batch["won"] is True
    base = f"/api/batches/{batch['id']}"
    for file in batch["files"]:
        path = f"{base}/files/{file['id']}"
        headers = {"X-Upload-Token": batch["upload_token"]}
        assert client.put(path + "/chunks/0", content=entries[file["rel_path"]], headers=headers).status_code == 200
        assert client.post(path + "/complete", headers=headers).status_code == 200
    return base


def test_document_supplement_full_sha_and_no_ai(formal):
    flow, project_id = formal
    base = upload(flow, project_id, {
        "01定稿文件/旧稿.ai": b"new version",
        "01定稿文件/新稿.ai": b"new content",
        "单品/旧杯.jpg": image_bytes("blue"),
    })
    op = flow.call("one", "GET", base + "/operation").json()
    kinds = {row["rel_path"]: row["conflict"] for row in op["conflicts"]}
    assert kinds == {"01定稿文件/旧稿.ai": "conflict", "01定稿文件/新稿.ai": "new", "单品/旧杯.jpg": "duplicate"}
    assert flow.call("one", "POST", base + "/confirm").json()["code"] == "conflict_choice_required"
    file_id = next(row["id"] for row in op["conflicts"] if row["conflict"] == "conflict")
    response = flow.call("one", "PUT", base + "/operation", {"params": {"files": [{"id": file_id, "choice": "keep_both"}]}})
    assert response.status_code == 200, response.text
    assert flow.call("one", "POST", base + "/confirm").json()["status"] == "待核对"
    response = flow.call("one", "POST", base + "/submit", {})
    assert response.status_code == 200, response.text
    batch_id = int(base.rsplit("/", 1)[-1])
    assert flow.client.app.state.jobs.wait_idle(8)
    with flow.client.app.state.database.session() as session:
        assert session.scalar(select(AiCall.id).where(AiCall.batch_id == batch_id)) is None
        assert session.scalar(select(BatchProduct.id).where(BatchProduct.batch_id == batch_id)) is None
        snapshot = session.scalar(select(ReviewSnapshot).where(ReviewSnapshot.batch_id == batch_id))
        assert snapshot.content["operation"]["params"]["files"] == [{"id": file_id, "choice": "keep_both"}]
        assert snapshot.content["operation"]["after"]["files"]
        assert all(f["fingerprint_kind"] == "full" and len(f["fingerprint"]) == 64 for f in snapshot.content["files"])


@pytest.mark.parametrize("reviewer", ["first", "final"])
def test_material_replacement_preview_summary_and_reject(formal, reviewer, monkeypatch):
    flow, project_id = formal
    app = flow.client.app
    label = "05 详情页/需要替换.jpg"
    unchanged = "05 详情页/无需上传.jpg"
    with app.state.database.session() as session:
        original = session.scalar(select(CatalogFile).where(CatalogFile.rel_path == label))
        unchanged_id = session.scalar(select(CatalogFile.id).where(CatalogFile.rel_path == unchanged))
    same_content = flow.call("one", "GET", f"/api/files/{unchanged_id}/download").content
    content = image_bytes("red")
    batch_base = upload(flow, project_id, {label: content, unchanged: same_content,
                        "01定稿文件/旧稿.ai": "另存一份".encode(), "01定稿文件/新增.ai": "新增".encode()})
    bid = int(batch_base.rsplit("/", 1)[-1])
    draft = flow.call("one", "GET", batch_base + "/operation").json()
    material = next(file for file in draft["conflicts"] if file["rel_path"] == label)
    copy = next(file for file in draft["conflicts"] if file["rel_path"] == "01定稿文件/旧稿.ai")
    url = f"/api/review/{bid}/supplement-files/{material['id']}/preview"
    assert flow.call("other", "GET", url).status_code == 403
    assert flow.call("one", "GET", url).status_code == 200
    response = flow.call("one", "PUT", batch_base + "/operation", {"params": {
        "files": [{"id": material["id"], "choice": "replace"}, {"id": copy["id"], "choice": "keep_both"}]}})
    assert response.status_code == 200, response.text
    assert flow.call("one", "POST", batch_base + "/confirm").json()["status"] == "待核对"
    response = flow.call("one", "POST", batch_base + "/submit", {})
    assert response.status_code == 200, response.text

    flow.base = f"/api/review/{bid}"
    first = flow.call("first", "GET", flow.base).json()
    snapshot = first["review"]["batch"]["review_snapshot_hash"]
    summary = first["supplement"]
    assert {k: summary[k] for k in ("added", "replaced", "skipped", "kept_both")} == {
        "added": 2, "replaced": 1, "skipped": 1, "kept_both": 1}
    pair, = summary["materials"]
    assert pair["original"]["id"] == original.id
    assert pair["original"]["fingerprint"] == original.fingerprint
    assert pair["uploaded"]["id"] == material["id"]
    assert pair["uploaded"]["fingerprint"] == material["fingerprint"]
    if reviewer == "final":
        assert flow.audit("first", "claim").status_code == 200
        assert flow.audit("first", "pass", ALL).status_code == 200
        assert flow.call("final", "GET", flow.base).json()["supplement"] == summary

    approved_url = url + f"?version={snapshot}"
    assert flow.call("other", "GET", approved_url).status_code == 403
    assert flow.call(reviewer, "GET", url + "?version=obsolete").json()["code"] == "stale_review"
    assert flow.call(reviewer, "GET", approved_url.replace(f"/{material['id']}/", f"/{copy['id']}/")).status_code == 404
    preview = flow.call(reviewer, "GET", approved_url)
    assert preview.headers["content-type"] == "image/jpeg"
    with Image.open(BytesIO(preview.content)) as image:
        assert image.getpixel((0, 0))[0] >= 250
    assert flow.call(reviewer, "GET", f"/api/files/{original.id}/preview").status_code == 200
    with app.state.database.session() as session:
        assert session.scalar(select(AccessLog.id).where(AccessLog.kind == "supplement_preview",
            AccessLog.target_id == material["id"], AccessLog.user_id == flow.users[reviewer], AccessLog.outcome == "allowed"))
        batch = session.get(Batch, bid)
        temp_path = batch.temp_path
    config = app.state.auth.config
    path = config.paths.dev_nas_dir / create_paths(config).join(Region.TEMP, temp_path, label)
    with monkeypatch.context() as patch:
        sessions = [fs for fs in app.state.auth.pool._sessions.values() if fs.permissions is not None]
        for fs in sessions:
            patch.setitem(fs.permissions, Region.TEMP, Access.NONE)
        assert flow.call(reviewer, "GET", approved_url).status_code == 403
    path.write_bytes(bytes([content[0] ^ 1]) + content[1:])
    assert flow.call(reviewer, "GET", approved_url).json()["code"] == "content_changed"
    path.write_bytes(content)

    assert flow.audit(reviewer, "reject", {"comment": "请补清楚资料图片"}).json()["code"] == "not_claimed"
    assert flow.audit(reviewer, "claim").status_code == 200
    assert flow.audit(reviewer, "reject", {"comment": " "}).status_code == 422
    rejected = flow.audit(reviewer, "reject", {"comment": "请补清楚资料图片"})
    assert rejected.status_code == 200 and rejected.json()["review"]["batch"]["status"] == "已驳回"
    resubmit = flow.call("one", "POST", batch_base + "/submit", {}).json()
    assert resubmit["batch"]["status"] == "待初审" and resubmit["batch"]["round"] == 2
    with app.state.database.session() as session:
        decision = session.scalar(select(ReviewRound).where(ReviewRound.batch_id == bid, ReviewRound.decision == "reject"))
        assert (decision.stage, decision.comment) == (reviewer, "请补清楚资料图片")


def test_replacement_requires_visual_and_explicit_relation_review(formal, monkeypatch):
    flow, project_id = formal
    base = upload(flow, project_id, {"单品/另一文件名.jpg": image_bytes("red")})
    assert flow.call("one", "POST", base + "/confirm").json()["code"] == "pre_descriptions_required"
    assert flow.call("one", "GET", base + "/pre-descriptions").json()["products"][0]["description"] is None
    describe_all(flow.client, base)
    begun = flow.call("one", "POST", base + "/confirm")
    assert begun.status_code == 200, begun.text
    assert flow.client.app.state.jobs.wait_idle(8)
    op = flow.call("one", "GET", base + "/operation").json()
    old_product = next(row for row in op["before"]["products"] if row["kind"] == "item")
    old_set = next(row for row in op["before"]["products"] if row["kind"] == "set")
    p = flow.call("one", "GET", base + "/products").json()["products"][0]
    assert p["visual_ai_succeeded"]
    confirm_fields(flow.client, base, p)
    params = {"products": [{"id": p["id"], "existing_product_id": old_product["id"]}]}
    response = flow.call("one", "PUT", base + "/operation", {"params": params})
    assert response.status_code == 200, response.text
    assert response.json()["required_relation_reviews"] == [old_set["id"]]
    detail = flow.call("one", "GET", base + "/products").json()
    assert detail["pending"]["fields_unconfirmed"] == 11
    assert detail["products"][0]["tags"]["category"]["previous_value"] is not None
    confirm_fields(flow.client, base, detail["products"][0])
    assert flow.call("one", "POST", base + "/submit", {}).json()["code"] == "relation_recheck_required"
    relations = {"relation_reviews": {str(old_set["id"]): [old_product["id"]]}}
    response = flow.call("one", "PUT", base + "/operation", {"params": relations})
    assert response.status_code == 200, response.text
    response = flow.call("one", "POST", base + "/submit", {})
    assert response.status_code == 200, response.text
    batch_id = int(base.rsplit("/", 1)[-1])
    with flow.client.app.state.database.session() as session:
        snapshot = session.scalar(select(ReviewSnapshot).where(ReviewSnapshot.batch_id == batch_id))
        assert snapshot.content["evidence"][str(p["id"])]["existing_product_id"] == old_product["id"]
        product_diff = snapshot.content["operation"]["after"]["products"][old_product["product_code"]]
        assert product_diff["before"]["rel_path"] == "单品/旧杯.jpg"
        assert product_diff["after"]["rel_path"] == "单品/另一文件名.jpg"
        assert session.get(CatalogFile, old_product["file_id"]).status == "active"
        product_link = session.scalar(select(Link.link_id).where(Link.kind == "product", Link.target_id == old_product["id"]))
        historical_link = session.scalar(select(Link.link_id).where(Link.kind == "file", Link.target_id == old_product["file_id"]))
    assert flow.call("other", "GET", "/l/" + product_link).json()["image_file_id"] == old_product["file_id"]
    flow.base = f"/api/review/{batch_id}"
    assert flow.call("one", "GET", flow.base).json()["can_claim"]
    assert flow.audit("first", "claim").status_code == 200
    read = flow.call("first", "GET", flow.base).json()
    assert read["supplement"]["replaced"] == 1 and read["supplement"]["added"] == 0
    first = read["operation"]
    assert first["params"]["products"] == params["products"]
    assert next(row["file_id"] for row in first["before"]["products"] if row["id"] == old_product["id"]) == old_product["file_id"]
    assert flow.audit("first", "pass", ALL).status_code == 200
    assert flow.audit("first", "claim").status_code == 403
    assert flow.audit("final", "claim").status_code == 200
    assert flow.call("final", "GET", flow.base).json()["operation"] == first
    with monkeypatch.context() as patch:
        interrupt_after_install(patch)
        passed = flow.audit("final", "pass", ALL)
        assert passed.status_code == 200, passed.text
        assert flow.client.app.state.jobs.wait_idle(8)
    assert flow.call("final", "GET", f"/api/operations/{batch_id}/execution").json()["status"] == "入库异常"
    assert flow.call("other", "GET", "/l/" + product_link).json()["image_file_id"] == old_product["file_id"]
    assert flow.call("final", "POST", f"/api/operations/{batch_id}/execute", {}).status_code == 200
    assert flow.client.app.state.jobs.wait_idle(8)
    execution = flow.call("final", "GET", f"/api/operations/{batch_id}/execution").json()
    assert execution["status"] == "已入库", execution
    current = flow.call("other", "GET", "/l/" + product_link).json()
    old_image = flow.call("other", "GET", "/l/" + historical_link).json()
    assert current["target_id"] == old_product["id"] and current["status"] == "active"
    assert current["image_file_id"] != old_product["file_id"]
    assert old_image["target_id"] == old_product["file_id"] and old_image["status"] == "replaced"
    assert flow.call("other", "GET", f"/api/files/{current['image_file_id']}/download").content == image_bytes("red")
    assert flow.call("other", "GET", f"/api/files/{old_image['target_id']}/download").content == image_bytes("blue")
    with flow.client.app.state.database.session() as session:
        current_product = session.get(Product, old_product["id"])
        version = session.scalar(select(HistoryVersion).where(HistoryVersion.file_id == old_product["file_id"]))
        relation = session.scalar(select(ProductRelation).where(
            ProductRelation.set_product_id == old_set["id"], ProductRelation.item_product_id == old_product["id"]))
        assert current_product.product_code == old_product["product_code"]
        assert version.new_rel_path.startswith("_历史版本/") and not version.expected_missing
        assert relation.status == "active" and relation.confirmed_by == flow.users["one"]


def test_rename_preview_mutex_and_frozen_owner(formal):
    flow, project_id = formal
    project_base = f"/api/projects/{project_id}/ops"
    params = {"project_name": "审核后名称"}
    response = flow.call("one", "POST", project_base + "/rename", {"params": params})
    assert response.status_code == 200, response.text
    batch_id = response.json()["id"]
    base = f"/api/batches/{batch_id}"
    assert flow.call("other", "POST", project_base + "/rename", {"params": params}).json()["code"] == "project_busy"
    assert flow.call("other", "PUT", base + "/operation", {"params": params}).status_code == 403
    assert flow.call("one", "POST", base + "/submit", {}).json()["code"] == "invalid_operation"
    preview = flow.call("one", "GET", base + "/operation/preview")
    assert preview.status_code == 200, preview.text
    assert preview.json()["after"]["project"]["project"]["after"]["project_name"] == "审核后名称"
    submitted = flow.call("one", "POST", base + "/operation/submit", {"params": params})
    assert submitted.status_code == 200, submitted.text
    assert flow.call("one", "PUT", base + "/operation", {"params": {"project_name": "绕过冻结"}}).status_code == 409
    with flow.client.app.state.database.session() as session:
        snapshot = session.scalar(select(ReviewSnapshot).where(ReviewSnapshot.batch_id == batch_id))
        assert snapshot.content["operation"]["params"]["project_name"] == "审核后名称"
        assert session.get(Project, project_id).project_name == "项目"
