"""WP07 核对页接口：dev 假账号、本地 NAS 与假模型。"""
from __future__ import annotations

from collections.abc import Iterator
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
import pytest
from sqlalchemy import select

from app.config import DevUser, Settings
from app.db.models import Batch, BatchFile, BatchProduct, Oplog, TagField
from app.main import create_app


def jpeg(color: str = "blue") -> bytes:
    stream = BytesIO()
    with Image.new("RGB", (1200, 800), color) as image:
        image.save(stream, "JPEG")
    return stream.getvalue()


@pytest.fixture
def client(settings: Settings, tmp_path: Path) -> Iterator[TestClient]:
    settings.paths.dev_nas_dir = tmp_path / "nas"
    settings.bootstrap_admin = "admin"
    settings.dev_users = [DevUser(username=name, password="fake", permissions={"temp": "write"}) for name in ["one", "two", "admin"]]
    with TestClient(create_app(settings, log_dir=tmp_path / "logs")) as client:
        login(client)
        yield client


def login(client: TestClient, user: str = "one") -> None:
    assert client.post("/api/auth/login", json={"username": user, "password": "fake"}).status_code == 200


def staged(client: TestClient, paths: list[str]) -> int:
    content = jpeg()
    response = client.post("/api/batches", json={"folder_name": "P001_企业_项目", "won": True, "acks": ["P08"],
        "files": [{"rel_path": path, "size": len(content), "mtime": 1} for path in paths]})
    assert response.status_code == 200, response.text
    batch = response.json()
    for file in batch["files"]:
        base = f"/api/batches/{batch['id']}/files/{file['id']}"
        headers = {"X-Upload-Token": batch["upload_token"]}
        assert client.put(base + "/chunks/0", content=content, headers=headers).status_code == 200
        assert client.post(base + "/complete", headers=headers).status_code == 200
    assert client.post(f"/api/batches/{batch['id']}/confirm").status_code == 200
    assert client.app.state.jobs.wait_idle(8)
    return batch["id"]


def review(client: TestClient, batch_id: int) -> dict:
    response = client.get(f"/api/batches/{batch_id}/products")
    assert response.status_code == 200, response.text
    return response.json()


def named(data: dict) -> dict[str, dict]:
    return {product["display_name"]: product for product in data["products"]}


def confirm_relations(client: TestClient, batch_id: int, data: dict) -> None:
    for product in data["products"]:
        if product["kind"] == "set":
            ids = [item["batch_product_id"] for item in product["relation_items"]]
            assert client.put(f"/api/batches/{batch_id}/products/{product['id']}/relations", json={"item_ids": ids}).status_code == 200


CLEAN = ["单品/杯子.jpg", "单品/雨伞.jpg", "单品/茶具.jpg", "套装/礼盒.jpg"]


def test_review_data_lists_products_fields_pending_and_folders(client: TestClient) -> None:
    batch_id = staged(client, [*CLEAN, "单品/无法判断.jpg", "01定稿文件/终稿.ai", "02设计方案/提案/一稿.pdf"])
    data = review(client, batch_id)
    assert data["batch"]["status"] == "待核对" and data["batch"]["editable"] and data["batch"]["recommended_path"] is None
    assert data["batch"]["company_name"] == "企业" and data["batch"]["owner_name"] == "one"
    assert [field["key"] for field in data["fields"]][:2] == ["category", "material"] and len(data["fields"]) == 11
    assert all(field["hint"] for field in data["fields"])
    products = named(data)
    assert products["礼盒"]["kind"] == "set" and products["杯子"]["kind"] == "item"
    tag = products["杯子"]["tags"]["material"]
    assert tag == {"value": "陶瓷、胡桃木", "terms": ["陶瓷", "胡桃木"], "source": "ai", "ai_value": "陶瓷、胡桃木", "flag": None}
    assert data["pending"] == {"yellow": 1, "relations_unconfirmed": 1, "failed": 0, "processing": 0, "can_submit": False}
    folders = {folder["name"]: folder["files"] for folder in data["folders"]}
    assert list(folders)[:5] == ["01定稿文件", "02设计方案", "03拍摄照片及视频", "04授权文件", "05 详情页"]
    assert folders["01定稿文件"] == [{"name": "终稿.ai", "size": len(jpeg())}]
    assert folders["02设计方案"][0]["name"] == "提案/一稿.pdf" and folders["04授权文件"] == []
    assert client.get("/api/batches/999/products").status_code == 404


def test_manual_edit_keeps_ai_value_persists_and_logs_field_changes(client: TestClient) -> None:
    batch_id = staged(client, CLEAN)
    cup = named(review(client, batch_id))["杯子"]
    response = client.patch(f"/api/batches/{batch_id}/products/{cup['id']}", json={"tags": {"material": "  不锈钢，硅胶 / 竹 "}, "free_text": " 人工描述 "})
    assert response.status_code == 200, response.text
    tag = response.json()["product"]["tags"]["material"]
    assert tag == {"value": "不锈钢，硅胶 / 竹", "terms": ["不锈钢", "硅胶", "竹"], "source": "manual", "ai_value": "陶瓷、胡桃木", "flag": None}
    saved = named(review(client, batch_id))["杯子"]
    assert saved["tags"]["material"]["value"] == "不锈钢，硅胶 / 竹" and saved["tags"]["material"]["ai_value"] == "陶瓷、胡桃木"
    assert saved["free_text"] == "人工描述" and saved["free_text_source"] == "manual" and saved["ai_free_text"].startswith("具有清晰外观")
    with client.app.state.database.session() as session:
        entry = session.scalars(select(Oplog).where(Oplog.action == "核对修改商品").order_by(Oplog.id.desc())).first()
        assert entry.before["tags"]["material"] == {"value": "陶瓷、胡桃木", "source": "ai", "flag": None}
        assert entry.after["tags"]["material"] == {"value": "不锈钢，硅胶 / 竹", "source": "manual", "flag": None}
        assert entry.before["free_text"].startswith("具有清晰外观") and entry.after["free_text"] == "人工描述"
        count = len(list(session.scalars(select(Oplog).where(Oplog.action == "核对修改商品"))))
    # 未变化的提交不产生修改，也不新增日志。
    again = client.patch(f"/api/batches/{batch_id}/products/{cup['id']}", json={"tags": {"material": "不锈钢，硅胶 / 竹", "color": "中国红、哑光黑"}})
    assert again.json()["product"]["tags"]["color"]["source"] == "ai"
    with client.app.state.database.session() as session:
        assert len(list(session.scalars(select(Oplog).where(Oplog.action == "核对修改商品")))) == count


def test_values_are_free_text_with_only_length_limit_and_enabled_fields(client: TestClient) -> None:
    batch_id = staged(client, CLEAN)
    cup = named(review(client, batch_id))["杯子"]
    url = f"/api/batches/{batch_id}/products/{cup['id']}"
    many = "、".join(f"值{index}" for index in range(100))
    assert len(client.patch(url, json={"tags": {"style": many}}).json()["product"]["tags"]["style"]["terms"]) == 100
    assert client.patch(url, json={"tags": {"style": "字" * 500}}).status_code == 200
    long = client.patch(url, json={"tags": {"style": "字" * 501}})
    assert long.status_code == 422 and long.json()["code"] == "value_too_long"
    assert client.patch(url, json={"tags": {"nope": "x"}}).json()["code"] == "unknown_field"
    assert client.patch(url, json={"tags": {"style": ""}}).json()["product"]["tags"]["style"]["value"] == ""
    assert client.patch(url, json={"unknown": 1}).status_code == 422
    assert client.patch(f"/api/batches/{batch_id}/products/99999", json={}).status_code == 404


def test_confirm_ai_value_clears_flag_and_edit_also_clears(client: TestClient) -> None:
    batch_id = staged(client, [*CLEAN, "单品/无法判断.jpg", "单品/无法判断乙.jpg", "单品/一图多品.jpg"])
    data = review(client, batch_id)
    assert data["pending"]["yellow"] == 3
    first, second, multi = named(data)["无法判断"], named(data)["无法判断乙"], named(data)["一图多品"]
    assert first["tags"]["category"]["flag"] == "uncertain" and multi["flags"] == ["multi_object"]
    confirmed = client.patch(f"/api/batches/{batch_id}/products/{first['id']}", json={"confirm_fields": ["category"]}).json()
    assert confirmed["product"]["tags"]["category"] == {"value": "无法判断", "terms": ["无法判断"], "source": "ai_confirmed", "ai_value": "无法判断", "flag": None}
    assert confirmed["pending"]["yellow"] == 2
    edited = client.patch(f"/api/batches/{batch_id}/products/{second['id']}", json={"tags": {"category": "保温杯"}}).json()
    assert edited["product"]["tags"]["category"]["flag"] is None and edited["pending"]["yellow"] == 1
    # 已人工修改的字段不能再「确认 AI 值」。
    assert client.patch(f"/api/batches/{batch_id}/products/{second['id']}", json={"confirm_fields": ["category"]}).status_code == 409
    resolved = client.patch(f"/api/batches/{batch_id}/products/{multi['id']}", json={"resolve_flags": ["multi_object"]}).json()
    assert resolved["product"]["flags"] == [] and resolved["pending"]["yellow"] == 0


def test_bulk_sets_same_text_on_three_items(client: TestClient) -> None:
    batch_id = staged(client, CLEAN)
    products = named(review(client, batch_id))
    ids = [products[name]["id"] for name in ("杯子", "雨伞", "茶具")]
    response = client.post(f"/api/batches/{batch_id}/products/bulk", json={"product_ids": ids, "tags": {"material": "304 不锈钢", "craft": "烫金"}})
    assert response.status_code == 200, response.text
    after = named(response.json())
    for name in ("杯子", "雨伞", "茶具"):
        assert after[name]["tags"]["material"]["value"] == "304 不锈钢" and after[name]["tags"]["material"]["source"] == "manual"
        assert after[name]["tags"]["material"]["ai_value"] == "陶瓷、胡桃木"
    assert after["礼盒"]["tags"]["material"]["source"] == "ai"
    with client.app.state.database.session() as session:
        assert len(list(session.scalars(select(Oplog).where(Oplog.action == "批量修改商品标签")))) == 3
    assert client.post(f"/api/batches/{batch_id}/products/bulk", json={"product_ids": [ids[0], 99999], "tags": {"material": "x"}}).status_code == 404
    assert client.post(f"/api/batches/{batch_id}/products/bulk", json={"product_ids": [], "tags": {"material": "x"}}).status_code == 422
    assert named(review(client, batch_id))["杯子"]["tags"]["material"]["value"] == "304 不锈钢"


def test_tag_values_counts_terms_in_current_batch(client: TestClient) -> None:
    batch_id = staged(client, CLEAN)
    cup = named(review(client, batch_id))["杯子"]
    client.patch(f"/api/batches/{batch_id}/products/{cup['id']}", json={"tags": {"material": "陶瓷、竹"}})
    values = client.get("/api/tag-values", params={"field": "material", "q": "", "batch_id": batch_id}).json()
    assert values[0] == {"term": "陶瓷", "count": 4} and {"term": "竹", "count": 1} in values
    assert client.get("/api/tag-values", params={"field": "material", "q": "胡", "batch_id": batch_id}).json() == [{"term": "胡桃木", "count": 3}]
    assert client.get("/api/tag-values", params={"field": "material"}).json() == []
    assert client.get("/api/tag-values").status_code == 422
    login(client, "two")
    assert client.get("/api/tag-values", params={"field": "material", "batch_id": batch_id}).status_code == 403


def test_relations_confirm_records_sources_and_validates_items(client: TestClient) -> None:
    batch_id = staged(client, [*CLEAN, "套装/空关系.jpg"])
    products = named(review(client, batch_id))
    box, empty = products["礼盒"], products["空关系"]
    assert empty["flags"] == ["empty_relation"] and len(box["relation_items"]) == 3
    url = f"/api/batches/{batch_id}/products/{empty['id']}/relations"
    result = client.put(url, json={"item_ids": [products["杯子"]["id"]]}).json()
    assert result["product"]["relation_items"] == [{"batch_product_id": products["杯子"]["id"], "source": "manual"}]
    assert result["product"]["relation_confirmed"] and result["product"]["flags"] == [] and result["pending"]["relations_unconfirmed"] == 1
    kept = client.put(f"/api/batches/{batch_id}/products/{box['id']}/relations", json={"item_ids": [products["雨伞"]["id"]]}).json()
    assert kept["product"]["relation_items"] == [{"batch_product_id": products["雨伞"]["id"], "source": "ai"}]
    assert client.put(url, json={"item_ids": [box["id"]]}).status_code == 422
    assert client.put(f"/api/batches/{batch_id}/products/{products['杯子']['id']}/relations", json={"item_ids": []}).status_code == 422
    # 空清单也可以由人确认。
    assert client.put(url, json={"item_ids": []}).json()["product"]["relation_confirmed"]


def test_submit_blocked_by_unresolved_flag(client: TestClient) -> None:
    batch_id = staged(client, ["单品/杯子.jpg", "单品/无法判断.jpg"])
    data = review(client, batch_id)
    assert data["pending"] == {"yellow": 1, "relations_unconfirmed": 0, "failed": 0, "processing": 0, "can_submit": False}
    blocked = client.post(f"/api/batches/{batch_id}/submit", json={"note": ""})
    assert blocked.status_code == 409 and blocked.json()["code"] == "submit_blocked" and blocked.json()["detail"]["yellow"] == 1
    client.patch(f"/api/batches/{batch_id}/products/{named(data)['无法判断']['id']}", json={"confirm_fields": ["category"]})
    assert client.post(f"/api/batches/{batch_id}/submit", json={"note": ""}).status_code == 200


def test_submit_blocked_by_image_level_flag(client: TestClient) -> None:
    batch_id = staged(client, ["单品/杯子.jpg", "单品/一图多品.jpg"])
    data = review(client, batch_id)
    assert client.post(f"/api/batches/{batch_id}/submit", json={}).json()["detail"]["yellow"] == 1
    client.patch(f"/api/batches/{batch_id}/products/{named(data)['一图多品']['id']}", json={"resolve_flags": ["multi_object"]})
    assert client.post(f"/api/batches/{batch_id}/submit", json={}).status_code == 200


def test_submit_blocked_by_unconfirmed_relations(client: TestClient) -> None:
    batch_id = staged(client, CLEAN)
    data = review(client, batch_id)
    blocked = client.post(f"/api/batches/{batch_id}/submit", json={})
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == {"yellow": 0, "relations_unconfirmed": 1, "failed": 0, "processing": 0, "can_submit": False}
    confirm_relations(client, batch_id, data)
    assert client.post(f"/api/batches/{batch_id}/submit", json={}).status_code == 200


def test_submit_blocked_by_failed_image_until_manual_fill(client: TestClient) -> None:
    batch_id = staged(client, ["单品/杯子.jpg", "单品/调用失败.jpg"])
    data = review(client, batch_id)
    failed = named(data)["调用失败"]
    assert failed["ai_status"] == "failed" and failed["ai_error"] and not failed["manually_filled"]
    blocked = client.post(f"/api/batches/{batch_id}/submit", json={})
    assert blocked.status_code == 409 and blocked.json()["detail"]["failed"] == 1
    # 只改自由描述或填空白不算手工填写标签。
    assert client.patch(f"/api/batches/{batch_id}/products/{failed['id']}", json={"tags": {"category": "  "}, "free_text": "描述"}).json()["pending"]["failed"] == 1
    filled = client.patch(f"/api/batches/{batch_id}/products/{failed['id']}", json={"tags": {"category": "保温杯"}}).json()
    assert filled["product"]["manually_filled"] and filled["product"]["ai_status"] == "failed" and filled["pending"]["can_submit"]
    assert client.post(f"/api/batches/{batch_id}/submit", json={}).status_code == 200


def test_submit_blocked_while_tagging_in_progress(client: TestClient) -> None:
    batch_id = staged(client, ["单品/杯子.jpg"])
    with client.app.state.database.session() as session:
        session.scalar(select(BatchProduct).where(BatchProduct.batch_id == batch_id)).ai_status = "pending"
    blocked = client.post(f"/api/batches/{batch_id}/submit", json={})
    assert blocked.status_code == 409 and blocked.json()["detail"]["processing"] == 1


def test_submit_does_not_require_filled_fields_and_saves_note(client: TestClient) -> None:
    batch_id = staged(client, CLEAN)
    data = review(client, batch_id)
    cup = named(data)["杯子"]
    cleared = {field["key"]: "" for field in data["fields"]}
    assert client.patch(f"/api/batches/{batch_id}/products/{cup['id']}", json={"tags": cleared, "free_text": ""}).status_code == 200
    confirm_relations(client, batch_id, data)
    assert client.post(f"/api/batches/{batch_id}/submit", json={"note": "字" * 2001}).status_code == 422
    response = client.post(f"/api/batches/{batch_id}/submit", json={"note": " 位置建议放到酒业 "})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["batch"]["status"] == "待初审" and body["batch"]["note_to_reviewer"] == "位置建议放到酒业" and not body["batch"]["editable"]
    with client.app.state.database.session() as session:
        batch = session.get(Batch, batch_id)
        assert batch.status == "待初审" and batch.round == 1
        entry = session.scalars(select(Oplog).where(Oplog.action == "提交审核")).one()
        assert entry.before["status"] == "待核对" and entry.after["status"] == "待初审"
    # 提交后所有写接口一律拒绝。
    assert client.patch(f"/api/batches/{batch_id}/products/{cup['id']}", json={"tags": {"material": "x"}}).status_code == 409
    assert client.post(f"/api/batches/{batch_id}/submit", json={}).status_code == 409
    assert client.delete(f"/api/batches/{batch_id}/products/{cup['id']}").status_code == 409
    assert client.get(f"/api/batches/{batch_id}/products").status_code == 200


def test_rejected_batch_is_editable_and_resubmit_increases_round(client: TestClient) -> None:
    batch_id = staged(client, ["单品/杯子.jpg"])
    with client.app.state.database.session() as session:
        session.get(Batch, batch_id).status = "已驳回"
    data = review(client, batch_id)
    assert data["batch"]["editable"]
    assert client.patch(f"/api/batches/{batch_id}/products/{data['products'][0]['id']}", json={"tags": {"material": "竹"}}).status_code == 200
    assert client.post(f"/api/batches/{batch_id}/submit", json={}).json()["batch"]["round"] == 2


def test_only_owner_and_admin_can_write_and_admin_changes_owner(client: TestClient) -> None:
    batch_id = staged(client, CLEAN)
    data = review(client, batch_id)
    cup, box = named(data)["杯子"], named(data)["礼盒"]
    login(client, "two")
    base = f"/api/batches/{batch_id}"
    assert client.get(base + "/products").status_code == 403
    attempts = [
        client.patch(f"{base}/products/{cup['id']}", json={"tags": {"material": "x"}}),
        client.post(base + "/products/bulk", json={"product_ids": [cup["id"]], "tags": {"material": "x"}}),
        client.post(f"{base}/products/{cup['id']}/retry-ai"),
        client.put(f"{base}/products/{box['id']}/relations", json={"item_ids": []}),
        client.post(base + "/products", data={"kind": "item"}, files={"file": ("新.jpg", jpeg(), "image/jpeg")}),
        client.delete(f"{base}/products/{cup['id']}"),
        client.post(base + "/submit", json={}),
        client.put(f"/api/admin/batches/{batch_id}/owner", json={"user_id": 1}),
    ]
    assert [response.status_code for response in attempts] == [403] * 8
    two_id = client.get("/api/auth/me").json()["id"]
    login(client, "admin")
    assert client.patch(f"{base}/products/{cup['id']}", json={"tags": {"material": "管理员改"}}).status_code == 200
    assert client.put("/api/admin/batches/999/owner", json={"user_id": two_id}).status_code == 404
    assert client.put(f"/api/admin/batches/{batch_id}/owner", json={"user_id": 999}).status_code == 404
    changed = client.put(f"/api/admin/batches/{batch_id}/owner", json={"user_id": two_id})
    assert changed.status_code == 200 and changed.json() == {"id": batch_id, "owner_id": two_id, "owner_name": "two"}
    with client.app.state.database.session() as session:
        entry = session.scalars(select(Oplog).where(Oplog.action == "更换批次责任人")).one()
        assert entry.before == {"owner_id": 1} and entry.after == {"owner_id": two_id}
    login(client, "two")
    assert client.patch(f"{base}/products/{cup['id']}", json={"tags": {"material": "新责任人改"}}).status_code == 200
    assert batch_id in [batch["id"] for batch in client.get("/api/batches").json()]
    login(client, "one")
    # 原上传人不再是责任人：仍可查看，但不能修改。
    assert client.get(base + "/products").json()["batch"]["editable"] is False
    assert client.patch(f"{base}/products/{cup['id']}", json={"tags": {"material": "x"}}).status_code == 403


def test_append_item_is_tagged_and_all_sets_need_recheck(client: TestClient, settings: Settings) -> None:
    batch_id = staged(client, CLEAN)
    data = review(client, batch_id)
    confirm_relations(client, batch_id, data)
    assert review(client, batch_id)["pending"]["can_submit"]
    response = client.post(f"/api/batches/{batch_id}/products", data={"kind": "item", "filename": "定制酒杯.jpg"},
                           files={"file": ("随便.jpg", jpeg("red"), "image/jpeg")})
    assert response.status_code == 200, response.text
    added = named(response.json())["定制酒杯"]
    assert added["kind"] == "item" and added["ai_status"] == "pending" and response.json()["pending"]["processing"] == 1
    assert client.app.state.jobs.wait_idle(8)
    after = review(client, batch_id)
    added, box = named(after)["定制酒杯"], named(after)["礼盒"]
    assert added["ai_status"] == "done" and len(added["tags"]) == 11 and added["thumb_status"] == "done"
    assert not box["relation_confirmed"] and box["flags"] == ["relation_recheck"]
    assert added["id"] not in [item["batch_product_id"] for item in box["relation_items"]] and len(box["relation_items"]) == 3
    assert after["pending"] == {"yellow": 0, "relations_unconfirmed": 1, "failed": 0, "processing": 0, "can_submit": False}
    with client.app.state.database.session() as session:
        batch = session.get(Batch, batch_id)
        file = session.get(BatchFile, added["batch_file_id"])
        assert file.rel_path == "单品/定制酒杯.jpg" and file.upload_status == "done" and file.fingerprint and batch.status == "待核对"
        stored = settings.paths.dev_nas_dir / "工作台临时区" / batch.batch_no / "P001_企业_项目" / "单品" / "定制酒杯.jpg"
        assert stored.read_bytes() == jpeg("red")
    assert client.get(f"/api/batches/{batch_id}/thumbs/{added['batch_file_id']}/300").status_code == 200
    confirm_relations(client, batch_id, after)
    assert review(client, batch_id)["pending"]["can_submit"]


def test_append_validates_name_kind_duplicate_and_size(client: TestClient, settings: Settings) -> None:
    batch_id = staged(client, CLEAN)
    url = f"/api/batches/{batch_id}/products"
    def post(kind: str, name: str, content: bytes = jpeg()) -> int:
        return client.post(url, data={"kind": kind}, files={"file": (name, content, "image/jpeg")}).status_code
    assert post("item", "杯子.JPG") == 409
    assert post("item", "说明.pdf") == 422
    assert post("item", "a:b.jpg") == 422
    assert post("other", "新图.jpg") == 422
    assert post("item", "空.jpg", b"") == 422
    assert client.post(url, data={"kind": "item"}).status_code == 422
    settings.upload.append_max_mb = 1
    assert post("item", "大图.jpg", b"0" * (1024 * 1024 + 70 * 1024)) == 413
    # 追加套装图不要求其他套装复核；坏图进入失败状态，可手填或移除。
    response = client.post(url, data={"kind": "set"}, files={"file": ("坏图.png", b"not an image", "image/png")})
    assert response.status_code == 200
    assert client.app.state.jobs.wait_idle(8)
    after = named(review(client, batch_id))
    assert after["坏图"]["kind"] == "set" and after["坏图"]["ai_status"] == "failed" and after["坏图"]["ai_error"]
    assert after["礼盒"]["flags"] == []


def test_remove_item_leaves_relations_and_moves_file_to_recycle(client: TestClient, settings: Settings) -> None:
    batch_id = staged(client, CLEAN)
    data = review(client, batch_id)
    confirm_relations(client, batch_id, data)
    cup = named(data)["杯子"]
    response = client.delete(f"/api/batches/{batch_id}/products/{cup['id']}")
    assert response.status_code == 200, response.text
    after = named(response.json())
    assert "杯子" not in after
    assert cup["id"] not in [item["batch_product_id"] for item in after["礼盒"]["relation_items"]] and len(after["礼盒"]["relation_items"]) == 2
    assert after["礼盒"]["relation_confirmed"]
    with client.app.state.database.session() as session:
        batch = session.get(Batch, batch_id)
        assert session.get(BatchFile, cup["batch_file_id"]).removed is True
        root = settings.paths.dev_nas_dir / "工作台临时区" / batch.batch_no
        entry = session.scalars(select(Oplog).where(Oplog.action == "移除主图")).one()
        assert entry.after["recycle_path"] == "_回收区/单品/杯子.jpg" and entry.before["in_sets"] == [after["礼盒"]["id"]]
    assert (root / "_回收区" / "单品" / "杯子.jpg").is_file() and not (root / "P001_企业_项目" / "单品" / "杯子.jpg").exists()
    assert client.delete(f"/api/batches/{batch_id}/products/{cup['id']}").status_code == 404
    assert client.patch(f"/api/batches/{batch_id}/products/{cup['id']}", json={}).status_code == 404
    assert client.get(f"/api/batches/{batch_id}/thumbs/{cup['batch_file_id']}/300").status_code == 404
    assert "杯子" not in [product["display_name"] for product in client.get(f"/api/batches/{batch_id}").json()["products"]]
    assert client.put(f"/api/batches/{batch_id}/products/{after['礼盒']['id']}/relations", json={"item_ids": [cup["id"]]}).status_code == 422
    # 同名重新追加后再次移除：回收区内不覆盖上一份。
    assert client.post(f"/api/batches/{batch_id}/products", data={"kind": "item"}, files={"file": ("杯子.jpg", jpeg("red"), "image/jpeg")}).status_code == 200
    assert client.app.state.jobs.wait_idle(8)
    again = named(review(client, batch_id))["杯子"]
    assert again["id"] == cup["id"] and again["ai_status"] == "done" and again["tags"]["material"]["source"] == "ai"
    assert client.delete(f"/api/batches/{batch_id}/products/{again['id']}").status_code == 200
    assert (root / "_回收区" / "单品" / "杯子_2.jpg").read_bytes() == jpeg("red")
    assert (root / "_回收区" / "单品" / "杯子.jpg").read_bytes() == jpeg()


def test_single_retry_overwrites_only_untouched_fields(client: TestClient) -> None:
    batch_id = staged(client, [*CLEAN, "单品/无法判断.jpg"])
    data = review(client, batch_id)
    target, box = named(data)["无法判断"], named(data)["礼盒"]
    url = f"/api/batches/{batch_id}/products/{target['id']}"
    client.patch(url, json={"tags": {"material": "人工材质"}, "confirm_fields": ["category"], "free_text": "人工描述"})
    with client.app.state.database.session() as session:
        product = session.get(BatchProduct, target["id"])
        product.tags = {**product.tags, "color": {**product.tags["color"], "value": "旧值", "ai_value": "旧值", "terms": ["旧值"]}}
        session.add(TagField(key="custom", label="新增字段", hint="说明", sort_order=99, enabled=True, version=1))
    confirm_relations(client, batch_id, data)
    started = client.post(url + "/retry-ai")
    assert started.status_code == 200 and started.json()["product"]["ai_status"] == "pending"
    assert client.app.state.jobs.wait_idle(8)
    after = named(review(client, batch_id))
    tags = after["无法判断"]["tags"]
    assert tags["material"]["value"] == "人工材质" and tags["material"]["source"] == "manual"
    assert tags["category"]["source"] == "ai_confirmed" and tags["category"]["flag"] is None
    assert tags["color"]["value"] == "中国红、哑光黑" and tags["custom"]["value"] == "演示新增字段"
    assert after["无法判断"]["free_text"] == "人工描述" and after["无法判断"]["ai_status"] == "done"
    # 已确认关系的套装重新打标，不改变人工确认结果。
    assert client.post(f"/api/batches/{batch_id}/products/{box['id']}/retry-ai").status_code == 200
    assert client.app.state.jobs.wait_idle(8)
    box_after = named(review(client, batch_id))["礼盒"]
    assert box_after["relation_confirmed"] and box_after["relation_items"] == box["relation_items"] and box_after["ai_status"] == "done"
    with client.app.state.database.session() as session:
        assert session.get(Batch, batch_id).status == "待核对" and session.get(Batch, batch_id).payload["ai_single"] == {}


def test_single_retry_failure_stays_failed_and_pending_conflicts(client: TestClient) -> None:
    batch_id = staged(client, ["单品/杯子.jpg", "单品/调用失败.jpg"])
    failed = named(review(client, batch_id))["调用失败"]
    url = f"/api/batches/{batch_id}/products/{failed['id']}/retry-ai"
    assert client.post(url).status_code == 200
    assert client.app.state.jobs.wait_idle(8)
    again = named(review(client, batch_id))["调用失败"]
    assert again["ai_status"] == "failed" and again["ai_error"] == "假模型调用失败"
    with client.app.state.database.session() as session:
        session.get(BatchProduct, failed["id"]).ai_status = "pending"
    assert client.post(url).status_code == 409
    assert client.post(f"/api/batches/{batch_id}/products/99999/retry-ai").status_code == 404
