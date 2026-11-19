"""从模拟上传和双审正式入库到检索、索引切换的回归。"""
import json

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.db.models import AiCall, Product, ProductEmbedding, SearchClick, SearchLog, Setting
from app.services.ai.client import AiCallError
from app.services.ingest.vectors import FORMAL_MODEL
from app.services.nas import NasPermissionDenied
from test_ai_pipeline import confirm, uploaded
from test_ingest import app_for, progress, start
from test_review import confirm_fields
from test_two_stage_review import ALL, CATALOG, Flow


@pytest.fixture
def published_search(settings, tmp_path):
    app = app_for(settings, tmp_path)
    with TestClient(app) as client:
        flow = Flow(client, ["单品/陶瓷红杯.jpg", "单品/磨砂雨伞.jpg", "套装/商务礼盒.jpg"])
        start(flow)
        assert app.state.jobs.wait_idle(8) and progress(flow)["status"] == "已入库"
        yield flow


def search(flow, query, actor="one", **extra):
    result = flow.call(actor, "POST", "/api/search", {"query": query, **extra})
    assert result.status_code == 200, result.text
    return result.json()


def test_exact_queries_are_local_and_log_clicks(published_search):
    flow = published_search
    project = search(flow, "P001")
    product = search(flow, "P001-001")
    assert project["exact"] and project["projects"][0]["project_code"] == "P001" and project["usage"] == []
    assert product["exact"] and product["products"][0]["product_code"] == "P001-001" and product["usage"] == []
    assert search(flow, "P999-001")["products"] == []
    assert search(flow, "P001", won=False)["projects"] == []
    row = product["products"][0]
    assert flow.call("one", "GET", "/l/" + row["url"].rsplit("/", 1)[1]).json()["target_id"] == row["id"]
    assert flow.call("one", "GET", f"/api/products/{row['id']}").status_code == 200
    body = {"search_id": product["search_id"], "kind": "product", "id": row["id"]}
    assert flow.call("other", "POST", "/api/search/click", body).status_code == 404
    assert flow.call("one", "POST", "/api/search/click", body).status_code == 204
    with flow.client.app.state.database.session() as session:
        assert session.get(SearchLog, product["search_id"]).ai_usage == []
        assert session.scalar(select(SearchClick)).target_id == row["id"]


def test_query_cache_hint_and_model_revision(published_search):
    flow = published_search
    result = search(flow, "瓷器")
    assert result["conditions"]["fields"]["material"] == ["陶瓷", "瓷器"]
    assert result["keyword_candidates"] >= 1
    assert any(call["kind"] == "search_query" for call in result["usage"])
    cached = search(flow, "瓷器")
    assert all(call["kind"] != "search_query" for call in cached["usage"])
    before = flow.call("admin", "GET", "/api/admin/tag-fields").json()
    fields = [{key: item[key] for key in ("key", "hint", "sort_order")} for item in before["fields"]]
    fields[0]["hint"] += "。搜索说明更新"
    assert flow.call("admin", "PUT", "/api/admin/tag-fields", {"version": before["version"], "fields": fields}).status_code == 200
    refreshed = search(flow, "瓷器")
    assert any(call["kind"] == "search_query" for call in refreshed["usage"])
    flow.client.app.state.settings.ai.model_text_version = "2026-11-19"
    assert any(call["kind"] == "search_query" for call in search(flow, "瓷器")["usage"])


def test_hybrid_rank_filters_permissions_and_invalid_ids(published_search, monkeypatch):
    flow = published_search
    app = flow.client.app
    login = app.state.auth.context(flow.cookies["one"], "测试")
    fs = app.state.auth.pool.get(login.session_id)
    raw = fs._open_raw

    def restricted(path, mode):
        if path.endswith("商务礼盒.jpg"):
            raise NasPermissionDenied("模拟文件的单独读取限制")
        return raw(path, mode)

    monkeypatch.setattr(fs, "_open_raw", restricted)
    client = app.state.ai.client()
    calls = []

    def artificial(task, payload):
        if task == "query":
            return json.dumps({"fields": {"material": ["陶瓷"]}, "keywords": [], "exclude": []})
        calls.extend(payload["candidates"])
        return '{"items":[{"index":1,"reason":"匹配"},{"index":1,"reason":"重复"},{"index": ninety,"reason":"错误"}]}'.replace("ninety", "90")

    with app.state.database.session() as session:
        match = session.scalar(select(ProductEmbedding).join(Product).where(Product.display_name == "陶瓷红杯"))
        import struct
        selected = list(struct.unpack(f"<{match.dimension}f", match.embedding))
    monkeypatch.setattr(client, "embed", lambda *_: selected)
    monkeypatch.setattr(client, "search_chat", artificial)
    found = search(flow, "陶瓷")
    assert found["keyword_candidates"] >= 1 and found["vector_candidates"] >= 1
    assert found["degraded"] and found["permission_filtered"]
    assert len(found["candidate_codes"]) == len(set(found["candidate_codes"]))
    assert all(row["name"] != "商务礼盒" for row in calls)
    assert all(set(row) == {"index", "name", "tags", "description"} for row in calls)
    assert all("商务礼盒" not in row["related"] for row in found["products"])
    assert found["products"][0]["matches"]


def test_rank_query_vector_failures_preserve_local_results(published_search, monkeypatch):
    flow = published_search
    client = flow.client.app.state.ai.client()

    def broken(task, payload):
        if task == "query":
            return '{"fields":{"unknown":["非法字段"]}}'
        raise AiCallError("模拟重排失败")

    monkeypatch.setattr(client, "search_chat", broken)
    monkeypatch.setattr(client, "embed", lambda *_: [1.0, 0.0])
    result = search(flow, "陶瓷")
    assert result["products"] and result["degraded"] and result["vector_candidates"] == 0
    assert len(result["warnings"]) == 3
    assert search(flow, "陶瓷", remove=["keyword:陶瓷"])["products"] == []
    with flow.client.app.state.database.session() as session:
        assert session.scalar(select(Setting).where(Setting.key == FORMAL_MODEL)).value["generation"] == 1


def publish_followup(flow):
    client = flow.client
    flow.sign_in("one")
    result = confirm(client, uploaded(client, ["单品/新陶瓷.jpg"], project_code="P002"))
    flow.bid = result["id"]
    flow.batch, flow.base = f"/api/batches/{flow.bid}", f"/api/review/{flow.bid}"
    item = client.get(flow.batch + "/products").json()["products"][0]
    confirm_fields(client, flow.batch, item)
    assert flow.call("one", "POST", flow.batch + "/submit", {}).status_code == 200
    for reviewer in ("first", "final"):
        assert flow.audit(reviewer, "claim").status_code == 200
        assert flow.audit(reviewer, "pass", ALL).status_code == 200
    assert client.app.state.jobs.wait_idle(8) and progress(flow)["status"] == "已入库"


def test_blue_green_switch_rollback_and_unavailable_candidate(published_search, monkeypatch):
    flow = published_search
    app = flow.client.app
    base = "/api/admin/vector-indexes"
    assert flow.call("one", "GET", base).status_code == 403
    assert flow.call("admin", "GET", base).json()["indexes"][0]["generation"] == 1
    make_client = app.state.ai.client

    def inject(config=None):
        client = make_client(config)
        if config and config.model_embed == "unavailable":
            def broken(*args):
                raise AiCallError("测试：候选不可用")
            monkeypatch.setattr(client, "embed", broken)
        return client

    monkeypatch.setattr(app.state.ai, "client", inject)
    assert flow.call("admin", "POST", base, {"name": "unavailable"}).status_code == 202
    assert app.state.jobs.wait_idle(8)
    assert flow.call("admin", "POST", base + "/2/switch", {}).status_code == 409
    assert search(flow, "陶瓷")["index_generation"] == 1
    # 候选失败时，新项目仍可使用正式模型完成。
    publish_followup(flow)
    status = flow.call("admin", "GET", base).json()["indexes"]
    assert next(row for row in status if row["generation"] == 2)["failures"] == 4
    assert flow.call("admin", "POST", base + "/2/cancel", {}).status_code == 200
    assert flow.call("admin", "POST", base, {"name": "next-embed", "revision": "r2"}).status_code == 202
    assert app.state.jobs.wait_idle(8)
    assert flow.call("admin", "POST", base + "/3/switch", {}).status_code == 200
    served = search(flow, "陶瓷")
    assert served["index_generation"] == 3 and any(call["model"] == "next-embed" for call in served["usage"])
    assert flow.call("admin", "POST", base + "/1/rollback", {}).status_code == 200
    assert search(flow, "陶瓷")["index_generation"] == 1
    with app.state.database.session() as session:
        assert {row.index_generation for row in session.scalars(select(ProductEmbedding))} == {1, 2, 3}


def test_search_exam_demo(settings, tmp_path, monkeypatch):
    from pathlib import Path

    import yaml

    from app.services.ai.client import FakeAiClient
    from scripts.run_exam import evaluate

    known = {
        "陶瓷红杯.jpg": {"material": "陶瓷", "color": "中国红", "style": "极简", "festival": "新年", "usage_scene": "日常居家"},
        "磨砂雨伞.jpg": {"material": "尼龙", "color": "蓝色", "usage_scene": "户外"},
        "商务礼盒.jpg": {"material": "纸板", "color": "牛皮色", "audience": "商务客户"},
    }
    original = FakeAiClient.generate

    def fake(self, image, fields, items=None, filename="", **kwargs):
        result = json.loads(original(self, image, fields, items, filename, **kwargs))
        result["tags"] = {field["key"]: known[filename].get(field["key"], "") for field in fields}
        result["free_text"] = ""
        return json.dumps(result, ensure_ascii=False)

    monkeypatch.setattr(FakeAiClient, "generate", fake)
    with TestClient(app_for(settings, tmp_path)) as client:
        flow = Flow(client, ["单品/磨砂雨伞.jpg", "单品/陶瓷红杯.jpg", "套装/商务礼盒.jpg"])
        start(flow)
        assert client.app.state.jobs.wait_idle(8) and progress(flow)["status"] == "已入库"
        cases = yaml.safe_load((Path(__file__).resolve().parents[1] / "search_exam.yaml").read_text())["cases"]
        results = evaluate(cases, lambda query: search(flow, query))
    print(json.dumps(results, ensure_ascii=False))
    assert results["cases"] == 10 and results["errors"] == 0
    assert results["candidate_recall"] == 1 and results["top10_hit_rate"] == 1 and results["empty_correct"] == 1


def test_search_scrubs_credentials_and_hides_inactive_assets(published_search, monkeypatch):
    flow = published_search
    app = flow.client.app
    with app.state.database.session() as session:
        session.scalar(select(Product).where(Product.display_name == "商务礼盒")).status = "voided"
    client = app.state.ai.client()
    original = client.search_chat
    captured = []

    def check_payload(task, data):
        captured.append(json.dumps(data, ensure_ascii=False))
        return original(task, data)

    monkeypatch.setattr(client, "search_chat", check_payload)
    question = "瓷器 酒企 P001 https://nas.intra/secret password=localsecret"
    result = search(flow, question)
    assert captured and all(all(secret not in raw for secret in ("酒企", "P001", "nas.intra", "localsecret", "商务礼盒"))
                            for raw in captured)
    assert all(row["name"] != "商务礼盒" for row in result["products"])
    assert search(flow, "P001-003")["products"] == []
    with app.state.database.session() as session:
        row = session.get(SearchLog, result["search_id"])
        assert "localsecret" not in row.query and "酒企" not in row.query


def test_index_switch_fails_on_inconsistent_row_and_can_retry(published_search):
    flow = published_search
    app = flow.client.app
    base = "/api/admin/vector-indexes"
    assert flow.call("admin", "POST", base, {"name": "next-embed"}).status_code == 202
    assert app.state.jobs.wait_idle(8)
    with app.state.database.session() as session:
        row = session.scalars(select(ProductEmbedding).where(ProductEmbedding.index_generation == 2)).first()
        row.text_hash = "0" * 64
    assert flow.call("admin", "POST", base + "/2/switch", {}).status_code == 409
    assert search(flow, "陶瓷")["index_generation"] == 1
    assert flow.call("admin", "POST", base + "/2/retry", {}).status_code == 200
    assert app.state.jobs.wait_idle(8)
    assert flow.call("admin", "POST", base + "/2/switch", {}).status_code == 200
    assert search(flow, "陶瓷")["index_generation"] == 2
