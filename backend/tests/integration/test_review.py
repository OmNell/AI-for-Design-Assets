"""经上传、缩略图、Fake AI 和设备会话进入核对，直接请求接口验证提交门禁。"""
import hashlib
import json
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from fastapi.testclient import TestClient
from PIL import Image
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.db.models import Batch, BatchFile, BatchProduct, Oplog, ReviewSnapshot
from test_ai_pipeline import client, confirm, login, uploaded


def ready(client: TestClient, paths: list[str]) -> str:
    batch = confirm(client, uploaded(client, paths))
    assert batch["status"] == "待核对"
    return f"/api/batches/{batch['id']}"


def confirm_fields(client: TestClient, url: str, p: dict, skip: str | None = None) -> None:
    choices = {key: "confirmed_value" if tag["value"] else "confirmed_unknown"
               for key, tag in p["tags"].items() if key != skip}
    assert client.patch(f"{url}/products/{p['id']}", json={"confirmations": choices}).status_code == 200


def test_freeze_each_round_confirmation_flags_and_rejection(client: TestClient) -> None:
    base = ready(client, ["单品/无法判断一图多品.jpg", "套装/空关系.jpg", "01定稿文件/稿.ai"])
    page = client.get(base + "/products").json()
    assert page["pending"]["fields_unconfirmed"] == 22
    item, group = page["products"]
    tag_url = f"{base}/products/{item['id']}"
    relations = f"{base}/products/{group['id']}/relations"
    assert client.put(relations, json={"item_ids": []}).status_code == 422
    assert client.put(relations, json={"item_ids": [], "confirm_kind": "confirmed_members"}).status_code == 422
    assert client.put(relations, json={"item_ids": [item["id"] + 100], "confirm_kind": "confirmed_members"}).status_code == 422
    assert client.patch(tag_url, json={"confirmations": {"festival": "confirmed_value"}}).status_code == 422
    assert client.patch(tag_url, json={"tags": {"unknown": "自定义"}}).status_code == 422
    confirm_fields(client, base, group)
    confirm_fields(client, base, item, skip="festival")
    assert client.put(relations, json={"item_ids": [], "confirm_kind": "confirmed_empty"}).json()["product"]["relation_confirm_kind"] == "confirmed_empty"
    assert client.post(base + "/submit", json={}).json()["detail"]["fields_unconfirmed"] == 1
    saved = client.patch(tag_url, json={"confirmations": {"festival": "confirmed_unknown"}}).json()
    assert saved["pending"]["fields_unconfirmed"] == 0
    assert not saved["pending"]["can_submit"] and saved["pending"]["yellow"] == 1
    assert client.post(base + "/submit", json={}).status_code == 409
    assert client.patch(tag_url, json={"resolve_flags": ["multi_object"]}).json()["pending"]["can_submit"]
    again = client.get(base + "/products").json()
    festival = again["products"][0]["tags"]["festival"]
    assert festival["confirmation_status"] == "confirmed_unknown" and festival["value"] == "" and festival["terms"] == []
    locked = client.post(base + "/submit", json={"note": "请核对位置"})
    assert locked.status_code == 200, locked.text
    assert locked.json()["batch"]["status"] == "待初审" and not locked.json()["batch"]["editable"]
    for response in (client.patch(tag_url, json={"free_text": "改写"}), client.post(base+"/submit", json={}),
                     client.put(relations, json={"item_ids": [], "confirm_kind":"confirmed_empty"}),
                     client.delete(tag_url), client.post(tag_url+"/retry-ai"), client.post(base+"/retry-ai"),
                     client.post(base+"/products/bulk", json={"product_ids":[item["id"]], "tags":{"color":"红"}})):
        assert response.status_code == 409, response.text
    db = client.app.state.database
    with db.session() as session:
        first = session.scalar(select(ReviewSnapshot))
        assert first.submitted_by == page["batch"]["owner_id"] and first.round == 1
        digest = hashlib.sha256(json.dumps(first.content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        assert first.digest == digest == locked.json()["batch"]["review_snapshot_hash"]
        assert len(first.content["files"]) == 3 and first.content["confirmation_oplog_ids"]
        assert all(len(f["fingerprint"]) == 64 for f in first.content["files"])
        assert len(first.content["evidence"][str(item["id"])]["tags_confirmation"]) == 11
    for sql in ("UPDATE review_snapshots SET round=88", "DELETE FROM review_snapshots"):
        with pytest.raises(IntegrityError), db.session() as session:
            session.execute(text(sql))
    # WP08 的退回边界由测试夹具提供，业务端没有伪造审核结果的入口。
    with db.session() as session:
        session.get(Batch, page["batch"]["id"]).status = "已驳回"
    assert client.patch(tag_url, json={"tags": {"material": "纸"}, "free_text": "第二轮新描述"}).json()["pending"]["fields_unconfirmed"] == 1
    assert client.post(base+"/submit", json={}).status_code == 409
    assert client.patch(tag_url, json={"confirmations": {"material": "confirmed_value"}}).status_code == 200
    second = client.post(base+"/submit", json={}).json()["batch"]
    assert second["round"] == 2 and second["review_snapshot_hash"] != digest and second["status"] == "待初审"
    with db.session() as session:
        assert [s.round for s in session.scalars(select(ReviewSnapshot).order_by(ReviewSnapshot.id))] == [1, 2]
        assert session.get(ReviewSnapshot, first.id).digest == digest


@pytest.mark.parametrize("fault", ["failed", "missing_proof", "sha", "bytes", "quick", "orphan_image", "disk", "extra"])
def test_incomplete_or_unrecognized_cannot_submit_even_with_manual_values(client: TestClient, fault: str) -> None:
    base = ready(client, ["单品/手工.jpg"])
    page = client.get(base+"/products").json()
    p = page["products"][0]
    body = {"tags": {k: "人工值" for k in p["tags"]}, "confirmations": {k: "confirmed_value" for k in p["tags"]}}
    assert client.patch(f"{base}/products/{p['id']}", json=body).json()["pending"]["can_submit"]
    db = client.app.state.database
    with db.session() as session:
        f = session.get(BatchFile, p["batch_file_id"])
        product = session.get(BatchProduct, p["id"])
        if fault == "failed":
            product.ai_status = "failed"
        elif fault == "missing_proof":
            product.ai_success_at = None
        elif fault == "sha":
            f.fingerprint = "e" * 64
        elif fault == "bytes":
            f.bytes_received -= 1
        elif fault == "quick":
            f.fingerprint_kind = "quick"
        elif fault == "orphan_image":
            session.add(BatchFile(batch_id=f.batch_id, rel_path="单品/无商品.jpg", size=f.size, mtime=1,
                                  bytes_received=f.size, upload_status="done", fingerprint_kind="full", fingerprint=f.fingerprint))
        else:
            path = next(client.app.state.auth.config.paths.dev_nas_dir.rglob("手工.jpg"))
            if fault == "extra":
                (path.parent / "未经上传.jpg").write_bytes(b"extra")
            else:
                data = path.read_bytes()
                path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    response = client.post(base+"/submit", json={})
    assert response.status_code == 409
    assert response.json()["code"] == ("review_file_changed" if fault in {"disk", "extra"} else "submit_blocked")
    with db.session() as session:
        assert session.scalar(select(ReviewSnapshot)) is None


def test_owner_only_admin_transfer_and_session_for_append_remove(client: TestClient) -> None:
    base = ready(client, ["单品/甲.jpg", "套装/乙.jpg"])
    page = client.get(base+"/products").json()
    bid = page["batch"]["id"]
    item, group = page["products"]
    url = base + "/products"
    login(client, "two")
    new_owner = client.get("/api/auth/me").json()["id"]
    assert client.get(url).status_code == 403
    assert client.put(f"/api/admin/batches/{bid}/owner", json={"user_id":new_owner}).status_code == 403
    login(client, "admin")
    assert client.get(url).json()["batch"]["editable"] is False
    assert client.patch(url+f"/{item['id']}", json={"free_text": "越权"}).status_code == 403
    assert client.post(base+"/submit", json={}).status_code == 403
    assert client.put(f"/api/admin/batches/{bid}/owner", json={"user_id":new_owner}).status_code == 200
    login(client)
    assert client.post(base+"/retry-ai").status_code == 403
    login(client, "two")
    member = {"item_ids":[item["id"]], "confirm_kind":"confirmed_members"}
    relations = f"{url}/{group['id']}/relations"
    assert client.put(relations,json=member).status_code == 200
    assert client.post(url+"/bulk", json={"product_ids":[item["id"],group["id"]],"tags":{"material":"胡桃木"}}).status_code == 200
    assert client.get("/api/tag-values",params={"field":"material","q":"胡桃","batch_id":bid}).json() == [{"term":"胡桃木","count":2}]
    assert client.get("/api/tag-values",params={"field":"旧字段","batch_id":bid}).status_code == 422
    assert client.delete(url+f"/{item['id']}").status_code == 200
    assert list(client.app.state.auth.config.paths.dev_nas_dir.rglob("_回收区/单品/甲.jpg"))
    view = client.get(url).json()
    assert view["products"][0]["relation_confirm_kind"] == "pending" and not view["products"][0]["relation_items"]
    data = BytesIO()
    Image.new("RGB", (40,40), "green").save(data, "JPEG")
    def append(name: str):
        return client.post(url, data={"kind":"item"},files={"file":(name,data.getvalue(),"image/jpeg")})
    assert append("../越界.jpg").status_code == 422
    assert append("甲.jpg").status_code == 200
    assert client.app.state.jobs.wait_idle()
    view = client.get(url).json()
    item, group = view["products"]
    assert item["visual_ai_succeeded"] and item["tags"]["material"]["previous_value"] == "胡桃木"
    assert item["tags"]["material"]["confirmation_status"] == "pending"
    assert "relation_recheck" in group["flags"]
    assert append("甲.jpg").status_code == 409
    assert client.patch(url+f"/{item['id']}", json={"tags":{"material":"玻璃"},"confirmations":{"material":"confirmed_value"}}).status_code == 200
    assert client.post(url+f"/{item['id']}/retry-ai").status_code == 200
    assert client.app.state.jobs.wait_idle()
    tag = client.get(url).json()["products"][0]["tags"]["material"]
    assert tag["value"] == "玻璃" and tag["confirmation_status"] == "confirmed_value"


def test_cannot_freeze_a_concurrently_edited_draft(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.review as review
    base = ready(client, ["单品/甲.jpg"])
    p = client.get(base+"/products").json()["products"][0]
    confirm_fields(client, base, p)
    started, release = Event(), Event()
    actual = review.fingerprint
    def pause(*args):
        started.set()
        assert release.wait(4)
        return actual(*args)
    monkeypatch.setattr(review, "fingerprint", pause)
    with ThreadPoolExecutor(max_workers=1) as worker:
        task = worker.submit(client.post, base+"/submit", json={})
        try:
            assert started.wait(4)
            assert client.patch(f"{base}/products/{p['id']}", json={"free_text":"另一设备修改"}).status_code == 200
        finally:
            release.set()
        assert task.result().json()["code"] == "review_changed"
    assert client.get(base+"/products").json()["batch"]["editable"]
    assert client.delete(f"{base}/products/{p['id']}").status_code == 200
    assert client.post(base+"/submit", json={}).json()["detail"]["missing_main_image"]


def test_failed_visual_run_can_save_draft_but_only_verified_retry_allows_confirmation(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.ai.client import FakeAiClient
    base = ready(client, ["单品/正常.jpg", "单品/调用失败.jpg"])
    page = client.get(base+"/products").json()
    failed = next(p for p in page["products"] if p["ai_status"] == "failed")
    url = f"{base}/products/{failed['id']}"
    manual = {k: "人工值" for k in failed["tags"]}
    assert client.patch(url, json={"tags":manual}).status_code == 200
    assert client.patch(url, json={"confirmations":{"category":"confirmed_value"}}).json()["code"] == "visual_evidence_required"
    assert client.post(base+"/submit", json={}).json()["detail"]["failed"] == 1
    generate = FakeAiClient.generate
    def recover(self, *args, **kwargs):
        return generate(self, *args, **{**kwargs, "filename":"恢复.jpg"})
    monkeypatch.setattr(FakeAiClient, "generate", recover)
    assert client.post(url+"/retry-ai").status_code == 200
    assert client.app.state.jobs.wait_idle()
    p = next(p for p in client.get(base+"/products").json()["products"] if p["id"] == failed["id"])
    assert p["visual_ai_succeeded"] and p["ai_status"] == "done" and p["tags"]["category"]["value"] == "人工值"
    assert p["tags"]["category"]["confirmation_status"] == "pending"


def test_nas_moves_rollback_on_database_failure(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    base = ready(client, ["单品/甲.jpg"])
    url = base+"/products"
    image = next(client.app.state.auth.config.paths.dev_nas_dir.rglob("甲.jpg"))
    original = image.read_bytes()
    p = client.get(url).json()["products"][0]
    def fail(*args, **kwargs):
        raise RuntimeError("模拟审计写入失败")
    monkeypatch.setattr(client.app.state.review, "_log", fail)
    with pytest.raises(RuntimeError, match="模拟审计写入失败"):
        client.delete(f"{url}/{p['id']}")
    assert image.read_bytes() == original
    with pytest.raises(RuntimeError, match="模拟审计写入失败"):
        client.post(url, data={"kind":"item"},files={"file":("乙.jpg",original,"image/jpeg")})
    assert not (image.parent / "乙.jpg").exists()
    assert [p["display_name"] for p in client.get(url).json()["products"]] == ["甲"]
