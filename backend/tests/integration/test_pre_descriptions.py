"""通过实际 API 验证描述门禁、图文证据和历史兼容。"""
import hashlib
import json
import threading

from sqlalchemy import select

from app.db.models import AiCall, Batch, BatchFile, BatchProduct, Job, Oplog, ReviewSnapshot
from app.services.ai.client import FakeAiClient
from test_ai_pipeline import client, confirm, describe_all, login, save_description, uploaded
from test_review import confirm_fields


def get(client, base):
    result = client.get(base + "/pre-descriptions")
    assert result.status_code == 200, result.text
    return result.json()


def test_staged_creates_one_record_per_main_and_direct_confirm_requires_both(client):
    batch = uploaded(client, ["单品/磁带.jpg", "套装/礼盒.jpg", "02设计方案/普通资料.jpg"])
    base = f"/api/batches/{batch['id']}"
    description = get(client, base)
    assert description["status"] == "已暂存"
    item, bundle = description["products"]
    assert [r["batch_file_id"] for r in (item, bundle)] == [f["id"] for f in batch["files"][:2]]
    assert (item["kind"], bundle["kind"]) == ("item", "set")
    assert item["state"] == bundle["state"] == "missing"
    direct = client.post(base + "/confirm")
    assert direct.status_code == 409 and direct.json()["code"] == "pre_descriptions_required"
    assert {row["product_id"] for row in direct.json()["detail"]} == {item["id"], bundle["id"]}
    for value in ("", "   ", "，!。", "x", "字" * 181):
        assert save_description(client, base, item, value).status_code == 422
    assert save_description(client, base, item, "磁带造型的 Bluetooth 音箱。").status_code == 200
    assert [row["product_id"] for row in client.post(base + "/confirm").json()["detail"]] == [bundle["id"]]
    with client.app.state.database.session() as session:
        assert session.scalar(select(Job).where(Job.kind == "ai_tag_batch")) is None
    assert save_description(client, base, bundle, "含音箱与收纳盒的礼盒").status_code == 200
    current = get(client, base)["products"]
    assert [(p["description"], p["version"], p["state"]) for p in current] == [
        ("磁带造型的 Bluetooth 音箱。", 1, "ready"), ("含音箱与收纳盒的礼盒", 1, "ready"),
    ]
    assert save_description(client, base, item, "旧窗口写入").json()["code"] == "pre_description_changed"


def test_description_version_binds_visual_evidence_and_review_snapshot(client, monkeypatch):
    cases = []
    original = FakeAiClient.generate
    def capture(self, image, fields, **kwargs):
        cases.append((hashlib.sha256(image).hexdigest(), kwargs["pre_description"], kwargs.get("items")))
        return original(self, image, fields, **kwargs)
    monkeypatch.setattr(FakeAiClient, "generate", capture)
    batch = uploaded(client, ["单品/磁带.jpg", "套装/礼盒.jpg"])
    base = f"/api/batches/{batch['id']}"
    item, bundle = get(client, base)["products"]
    assert save_description(client, base, item, "磁带造型音箱，描述冲突").status_code == 200
    assert save_description(client, base, bundle, "音箱和收纳盒套装").status_code == 200
    assert client.post(base + "/confirm").status_code == 200
    assert client.app.state.jobs.wait_idle()
    assert [row[1] for row in cases] == ["磁带造型音箱，描述冲突", "音箱和收纳盒套装"]
    assert cases[0][2] is None and cases[1][2][0]["index"] == 1
    for product, case in zip((item, bundle), cases, strict=True):
        cache = client.app.state.auth.config.paths.data_dir / "thumbs" / "batch" / str(batch["id"]) / str(product["batch_file_id"]) / "1024.jpg"
        assert case[0] == hashlib.sha256(cache.read_bytes()).hexdigest()

    item, bundle = client.get(base + "/products").json()["products"]
    assert item["flags"] == ["description_conflict"]
    assert item["pre_description"] == "磁带造型音箱，描述冲突"
    assert json.loads(item["ai_raw_result"])["description_conflict"] is True
    assert item["free_text"] != item["pre_description"]
    assert len(item["tags"]) == len(bundle["tags"]) == 11
    assert bundle["relation_items"][0]["batch_product_id"] == item["id"]
    confirm_fields(client, base, item)
    confirm_fields(client, base, bundle)
    assert client.put(f"{base}/products/{bundle['id']}/relations",
                      json={"item_ids": [item["id"]], "confirm_kind": "confirmed_members"}).status_code == 200
    assert client.post(base + "/submit", json={}).json()["detail"]["yellow"] == 1
    assert client.patch(f"{base}/products/{item['id']}", json={"resolve_flags": ["description_conflict"]}).status_code == 200
    current = get(client, base)["products"][0]
    assert save_description(client, base, current, "这是磁带外形的蓝牙音箱。").status_code == 200
    view = client.get(base + "/products").json()
    assert not view["products"][0]["visual_ai_succeeded"]
    assert view["pending"]["fields_unconfirmed"] == 11
    assert client.post(base + "/submit", json={}).status_code == 409
    assert client.patch(f"{base}/products/{item['id']}",
                        json={"confirmations": {"category": "confirmed_value"}}).json()["code"] == "visual_evidence_required"
    assert client.post(f"{base}/products/{item['id']}/retry-ai").status_code == 200
    assert client.app.state.jobs.wait_idle()
    refreshed = client.get(base + "/products").json()["products"][0]
    assert refreshed["visual_ai_succeeded"] and not refreshed["flags"]
    assert cases[-1][1] == "这是磁带外形的蓝牙音箱。"
    confirm_fields(client, base, refreshed)
    assert client.post(base + "/submit", json={}).status_code == 200
    assert save_description(client, base, get(client, base)["products"][0], "审核中不允许修改").status_code == 409
    with client.app.state.database.session() as session:
        frozen = session.scalar(select(ReviewSnapshot).where(ReviewSnapshot.batch_id == batch["id"]))
        proof = frozen.content["evidence"][str(item["id"])]
        assert proof["pre_description_version"] == proof["ai_description_version"] == 2
        assert proof["pre_description_fingerprint"] == proof["ai_fingerprint"]
        assert proof["pre_description"] == "这是磁带外形的蓝牙音箱。" and proof["pre_description_by"]
        assert frozen.content["review"]["products"][0]["pre_description"] == proof["pre_description"]
        edits = list(session.scalars(select(Oplog).where(Oplog.action == "保存商品主图预描述")))
        assert any(row.before["description"] == "磁带造型音箱，描述冲突" for row in edits)


def test_hash_change_and_active_job_cannot_reuse_old_description(client, monkeypatch):
    batch = uploaded(client, ["单品/杯.jpg"])
    assert client.app.state.jobs.wait_idle()
    base = f"/api/batches/{batch['id']}"
    describe_all(client, base)
    first = get(client, base)["products"][0]
    with client.app.state.database.session() as session:
        file = session.get(BatchFile, first["batch_file_id"])
        original_digest = file.fingerprint
        file.fingerprint = "f" * 64
    assert get(client, base)["products"][0]["state"] == "reconfirm"
    assert client.post(base + "/confirm").json()["code"] == "pre_descriptions_required"
    assert save_description(client, base, first, first["description"]).json()["code"] == "pre_description_changed"
    with client.app.state.database.session() as session:
        session.get(BatchFile, first["batch_file_id"]).fingerprint = original_digest

    entered, release = threading.Event(), threading.Event()
    original = FakeAiClient.generate
    def pause(self, *args, **kwargs):
        entered.set()
        assert release.wait(4)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(FakeAiClient, "generate", pause)
    assert client.post(base + "/confirm").status_code == 200
    try:
        assert entered.wait(3)
        in_flight = get(client, base)["products"][0]
        assert in_flight["ai_in_progress"]
        assert save_description(client, base, in_flight, "运行中写入").json()["code"] == "ai_in_progress"
        # 模拟开始后、模型返回前更换版本，旧任务不能发布标签。
        with client.app.state.database.session() as session:
            product = session.get(BatchProduct, first["id"])
            product.pre_description_version += 1
            product.pre_description = "模拟另一版本已保存"
    finally:
        release.set()
    assert client.app.state.jobs.wait_idle()
    assert get(client, base)["status"] == "AI 处理失败"
    with client.app.state.database.session() as session:
        product = session.get(BatchProduct, first["id"])
        assert product.ai_success_at is None and not product.tags


def test_failed_retry_uses_current_description_and_requires_owner_to_edit(client, monkeypatch):
    captures = []
    original = FakeAiClient.generate
    def spy(self, *args, **kwargs):
        captures.append(kwargs["pre_description"])
        return original(self, *args, **kwargs)
    monkeypatch.setattr(FakeAiClient, "generate", spy)
    batch = uploaded(client, ["单品/调用失败.jpg"])
    base = f"/api/batches/{batch['id']}"
    result = confirm(client, batch)
    assert result["status"] == "AI 处理失败"
    product = get(client, base)["products"][0]
    login(client, "two")
    assert client.get(base + "/pre-descriptions").status_code == 403
    assert save_description(client, base, product, "越权描述").status_code == 403
    login(client, "admin")
    assert save_description(client, base, product, "管理员也不能代填").status_code == 403
    login(client)
    foreign = {**product, "id": product["id"] + 500}
    assert save_description(client, base, foreign, "其他主图").status_code == 404
    assert client.post(base + "/retry-ai").status_code == 200
    assert client.app.state.jobs.wait_idle()
    assert captures == ["这是一只水杯。", "这是一只水杯。"]


def test_legacy_completed_evidence_preserved_and_queued_job_does_not_guess_description(client, monkeypatch):
    complete = confirm(client, uploaded(client, ["单品/完成.jpg"], project_code="OLD1"))
    waiting = uploaded(client, ["单品/等候.jpg"], project_code="OLD2")
    assert client.app.state.jobs.wait_idle()
    base = f"/api/batches/{waiting['id']}"
    # 模拟迁移前留下的已成功证据和旧版队列 payload。
    app = client.app.state
    with app.auth.lock, app.database.session() as session:
        old = session.get(BatchProduct, complete["products"][0]["id"])
        old.pre_description = old.pre_description_by = old.pre_description_at = old.pre_description_fingerprint = None
        old.pre_description_version, old.ai_description_version, old.ai_raw_result = 0, None, None
        queued = session.get(Batch, waiting["id"])
        queued.status = "AI 处理中"
        queued.payload = {**queued.payload, "ai_run": "legacy-run", "ai_initialized": False}
        app.jobs.enqueue("ai_tag_batch", {"batch_id": queued.id, "run_id": "legacy-run"}, session=session)
    assert app.jobs.wait_idle()
    assert client.get(f"/api/batches/{complete['id']}").json()["products"][0]["visual_ai_succeeded"]
    assert get(client, base)["status"] == "AI 处理失败"
    assert get(client, base)["products"][0]["state"] == "missing"
    assert client.post(base + "/retry-ai").json()["code"] == "pre_descriptions_required"
    with app.database.session() as session:
        assert session.scalar(select(AiCall.id).where(AiCall.batch_id == waiting["id"])) is None
