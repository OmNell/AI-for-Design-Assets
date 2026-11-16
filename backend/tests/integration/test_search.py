"""WP11 检索：两种向量实现各跑一遍同一套用例。"""
from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import func, select

from app.config import DevUser, PathsConfig, Settings
from app.db.models import AiCall, Job, Oplog, Product, ProductEmbedding, ProductTag, Project, QueryCache, SearchLog, TagField, User
from app.main import create_app
from app.services.ai.client import AiClient
from app.services.search import parse_conditions, parse_ranking, product_text
from tests.integration.test_audit import login
from tests.integration.test_ingest import ROLES, approve, to_final

TAGS = {
    "P001-001": {"category": "手提袋", "festival": "通用", "material": "帆布", "audience": "员工", "craft": "细瓷贴花"},
    "P001-002": {"category": "礼盒", "festival": "中秋", "material": "纸", "color": "中国红"},
    "P001-003": {"category": "摆件", "festival": "新年", "audience": "上级", "size": "掌心", "material": "陶瓷"},
    "P001-004": {"category": "茶具套装", "festival": "春节", "material": "陶瓷", "audience": "长辈"},
    "P001-005": {"category": "保温杯", "festival": "新年", "material": "塑料", "audience": "儿童"},
}
FREE = {"P001-001": "结实耐用的帆布袋", "P001-002": "带提手的月饼包装，内衬细瓷托", "P001-003": "桌面上的福字小物", "P001-004": "一壶四杯", "P001-005": "卡通图案"}


@pytest.fixture(params=["sqlite-vec", "numpy"])
def client(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(mode="dev", paths=PathsConfig(data_dir=tmp_path / "data", dev_nas_dir=tmp_path / "nas"), bootstrap_admin="admin")
    settings.search.vector_backend = request.param
    settings.dev_users = [DevUser(username=name, password="fake", permissions={"temp": "write", "formal": "write" if name == "final" else "read"}) for name in ROLES]
    with TestClient(create_app(settings, log_dir=tmp_path / "logs")) as client:
        assert client.app.state.search.store.name == request.param
        for name in ROLES:
            login(client, name)
        with client.app.state.database.session() as session:
            for user in session.scalars(select(User)):
                user.roles = ROLES[user.username]
        login(client, "admin")
        assert client.put("/api/admin/company-map", json={"industries": [{"name": "酒业"}], "companies": [{"name": "企业", "aliases": ["某企业集团"], "industry_name": "酒业"}]}).status_code == 200
        approve(client, to_final(client))
        # 用确定的标签覆盖假模型打的标，再让向量任务按新文本重算。
        with client.app.state.database.session() as session:
            for product in session.scalars(select(Product)):
                product.free_text = FREE[product.product_code]
                for tag in session.scalars(select(ProductTag).where(ProductTag.product_id == product.id)):
                    tag.value = TAGS[product.product_code].get(tag.field_key, "")
                    tag.terms = [tag.value] if tag.value else []
                client.app.state.search.enqueue(session, product.id)
        assert client.app.state.jobs.wait_idle(10)
        login(client, "one")
        yield client


def search(client: TestClient, query: str, **extra: object) -> dict:
    response = client.post("/api/search", json={"query": query, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def codes(result: dict, only_keywords: bool = False) -> list[str]:
    return [item["product_code"] for item in result["results"] if item["by_keywords"] or not only_keywords]


def calls(client: TestClient, kind: str) -> int:
    with client.app.state.database.session() as session:
        return session.scalar(select(func.count()).select_from(AiCall).where(AiCall.kind == kind)) or 0


def test_code_queries_go_first_without_any_model_call(client: TestClient) -> None:
    before = {kind: calls(client, kind) for kind in ("query", "rank", "embed")}
    for text in ("P001-003", "p001-003", " P001-003 "):
        result = search(client, text)
        assert result["kind"] == "code" and codes(result) == ["P001-003"] and result["model_calls"] == 0 and result["conditions"] == []
        assert result["results"][0]["reason"] == "编号精确匹配" and result["results"][0]["display_name"] == "酒瓶"
    project = search(client, "p001")
    assert project["kind"] == "code" and codes(project) == ["P001-001", "P001-002", "P001-003", "P001-004", "P001-005"]
    assert [hit["project_code"] for hit in project["project_hits"]] == ["P001"] and project["model_calls"] == 0
    assert {kind: calls(client, kind) for kind in ("query", "rank", "embed")} == before
    with client.app.state.database.session() as session:
        logs = list(session.scalars(select(SearchLog).order_by(SearchLog.id)))
        assert [(log.kind, log.model_calls, log.degraded, log.result_count) for log in logs] == [("code", 0, False, 1)] * 3 + [("code", 0, False, 5)]
    # 像编号但查不到：按普通文字处理。
    assert search(client, "X999-123")["kind"] == "natural"


def test_natural_query_understands_expands_synonyms_and_merges_two_routes(client: TestClient) -> None:
    result = search(client, "过年送领导的小摆件")
    assert result["kind"] == "natural" and not result["degraded"] and result["model_calls"] == 3 and not result["from_cache"]
    conditions = {(item["field"], item["terms"][0]): item for item in result["conditions"]}
    assert conditions[("festival", "过年")]["terms"] == ["过年", "春节", "新年"] and conditions[("festival", "过年")]["label"] == "节日场景"
    assert conditions[("audience", "领导")]["terms"] == ["领导", "企业客户", "上级"]
    assert conditions[("category", "小摆件")]["terms"] == ["小摆件", "摆件", "桌面摆设"] and ("size", "小") in conditions
    # 标签写的是「新年」「上级」「掌心」，用「过年」「领导」「小」搜到，且排第一。
    top = result["results"][0]
    assert top["product_code"] == "P001-003" and top["by_keywords"] and top["by_vector"] and top["reason"]
    assert set(top["matched"]) == {"节日场景", "受众群体", "品类", "尺寸"}
    assert set(codes(result, only_keywords=True)) == {"P001-003", "P001-004", "P001-005"}
    # 另外两个只由向量一路带出。
    assert set(codes(result)) == set(TAGS) and all(item["by_vector"] for item in result["results"])
    assert top["project"] == {"id": top["project"]["id"], "project_code": "P001", "project_name": "项目", "company_name": "企业", "won": True, "link_id": top["project"]["link_id"]}
    assert top["link_id"] and top["project"]["link_id"] and [item["product_code"] for item in top["related"]] == ["P001-004", "P001-005"]
    assert {tag["label"]: tag["value"] for tag in top["tags"]}["节日场景"] == "新年"
    assert result["total"] == 5 and result["local_ms"] <= result["elapsed_ms"]
    with client.app.state.database.session() as session:
        log = session.scalars(select(SearchLog)).one()
        assert (log.query, log.kind, log.result_count, log.model_calls, log.degraded) == ("过年送领导的小摆件", "natural", 5, 3, False)
        assert log.conditions[0]["type"] == "field" and log.elapsed_ms >= 0


def test_removing_a_condition_changes_results_without_new_understanding(client: TestClient) -> None:
    first = search(client, "过年送领导的小摆件")
    understood = calls(client, "query")
    kept = [item for item in first["conditions"] if item["field"] not in ("festival", "audience")]
    second = search(client, "过年送领导的小摆件", conditions=kept)
    assert calls(client, "query") == understood and second["model_calls"] == 2
    assert [item["field"] for item in second["conditions"]] == ["category", "size"]
    assert set(codes(first, only_keywords=True)) == {"P001-003", "P001-004", "P001-005"}
    assert set(codes(second, only_keywords=True)) == {"P001-003"}
    assert set(second["results"][0]["matched"]) == {"品类", "尺寸"}


def test_understanding_is_cached_per_query_and_field_version(client: TestClient) -> None:
    first = search(client, "中秋 礼盒")
    second = search(client, "  中秋   礼盒 ")
    assert not first["from_cache"] and second["from_cache"] and (first["model_calls"], second["model_calls"]) == (3, 2)
    assert first["conditions"] == second["conditions"] and calls(client, "query") == 1
    with client.app.state.database.session() as session:
        assert len(list(session.scalars(select(QueryCache)))) == 1
        for field in session.scalars(select(TagField)):
            field.version += 1
    # 字段清单变了，旧的理解结果不再使用。
    assert not search(client, "中秋 礼盒")["from_cache"] and calls(client, "query") == 2


def test_voided_products_and_projects_never_appear(client: TestClient) -> None:
    with client.app.state.database.session() as session:
        session.scalar(select(Product).where(Product.product_code == "P001-003")).status = "voided"
    result = search(client, "过年送领导的小摆件")
    assert "P001-003" not in codes(result) and set(codes(result)) == {"P001-001", "P001-002", "P001-004", "P001-005"}
    assert search(client, "P001-003")["results"] == [] and "P001-003" not in codes(search(client, "P001"))
    # 关系里已作废的单品也不再显示。
    box = next(item for item in search(client, "春节")["results"] if item["product_code"] == "P001-004")
    assert "P001-003" not in [item["product_code"] for item in box["related"]]
    with client.app.state.database.session() as session:
        session.scalars(select(Project)).one().status = "voided"
    assert search(client, "过年")["results"] == [] and search(client, "项目")["project_hits"] == []


def test_ranking_failure_degrades_to_keyword_then_similarity_order(client: TestClient) -> None:
    result = search(client, "过年送领导的小摆件 排序失败")
    assert result["degraded"] and result["notes"] == ["排序模型暂不可用，结果按关键词命中和相似度排列"] and result["model_calls"] == 2
    assert result["results"][0]["product_code"] == "P001-003" and all(item["reason"] is None for item in result["results"])
    keyword_flags = [item["by_keywords"] for item in result["results"]]
    assert keyword_flags == sorted(keyword_flags, reverse=True) and len(result["results"]) == 5
    with client.app.state.database.session() as session:
        assert session.scalars(select(SearchLog)).one().degraded is True


def test_understanding_or_embedding_failure_still_returns_results(client: TestClient) -> None:
    result = search(client, "帆布 理解失败")
    assert result["degraded"] and "查询理解暂不可用，已直接按输入的文字匹配" in result["notes"]
    assert [item["terms"][0] for item in result["conditions"]] == ["帆布", "理解失败"] and result["results"][0]["product_code"] == "P001-001"
    vectorless = search(client, "陶瓷 向量失败")
    assert vectorless["degraded"] and "向量检索暂不可用，本次只按关键词匹配" in vectorless["notes"]
    assert not any(item["by_vector"] for item in vectorless["results"]) and {"P001-003", "P001-004"} <= set(codes(vectorless))


def test_model_ranking_drops_out_of_range_and_duplicate_indexes(client: TestClient) -> None:
    result = search(client, "陶瓷 越界序号")
    assert not result["degraded"] and len(codes(result)) == len(set(codes(result))) == 5
    assert parse_ranking('{"ranking":[{"index":3,"reason":"好"},{"index":9},{"index":"1"},{"index":3},{"index":true},{"index":1}]}', 3) == [(3, "好"), (1, "")]
    assert parse_ranking('```json\n{"ranking":[2,1,2,0]}\n```', 2) == [(2, ""), (1, "")]
    with pytest.raises(ValueError):
        parse_ranking("不是 JSON", 3)


def test_exclusion_relaxation_filters_and_company_condition(client: TestClient) -> None:
    excluded = search(client, "新年 不要塑料")
    assert [item["type"] for item in excluded["conditions"]] == ["field", "exclude"] and excluded["conditions"][1]["terms"] == ["塑料"]
    assert "P001-005" not in codes(excluded) and "P001-003" in codes(excluded, only_keywords=True)
    # 「陶瓷」按材质字段命中两个，描述里含「瓷」的再加一个，仍少于阈值 → 放宽：
    # 不再区分字段，同义说法「瓷」出现在手提袋的工艺字段里也算。
    relaxed = search(client, "陶瓷")
    assert relaxed["relaxed"] and set(codes(relaxed, only_keywords=True)) == {"P001-001", "P001-002", "P001-003", "P001-004"}
    by_code = {item["product_code"]: item for item in relaxed["results"]}
    assert by_code["P001-003"]["matched"] == ["材质"] and by_code["P001-002"]["matched"] == [] and by_code["P001-001"]["matched"] == ["放宽匹配"]
    assert not by_code["P001-005"]["by_keywords"]
    assert search(client, "陶瓷", filters={"won": False})["results"] == []
    tree = client.get("/api/catalog/tree").json()[0]["industries"][0]
    assert len(search(client, "陶瓷", filters={"won": True, "industry": tree["id"], "company": tree["companies"][0]["id"]})["results"]) == 5
    assert search(client, "陶瓷", filters={"company": 999})["results"] == []
    named = search(client, "某企业集团的陶瓷摆件")
    assert named["conditions"][0] == {"type": "company", "field": None, "label": "企业", "terms": ["企业"]}
    assert named["results"] and [hit["project_code"] for hit in named["project_hits"]] == ["P001"]
    assert search(client, "陶瓷", conditions=[{"type": "company", "label": "企业", "terms": ["别家"]}, {"type": "field", "field": "material", "label": "材质", "terms": ["陶瓷"]}])["results"] == []


def test_project_hits_by_name_code_and_company(client: TestClient) -> None:
    for text in ("项目", "企业", "P00"):
        hits = search(client, text)["project_hits"]
        assert [(hit["project_code"], hit["company_name"], hit["won"]) for hit in hits] == [("P001", "企业", True)] and hits[0]["link_id"], text
    assert search(client, "完全无关的内容")["project_hits"] == []
    assert client.post("/api/search", json={"query": "  "}).status_code == 422
    assert client.post("/api/search", json={"query": "x" * 201}).status_code == 422
    client.cookies.clear()
    assert client.post("/api/search", json={"query": "陶瓷"}).status_code == 401


def test_click_is_recorded_on_own_search_log(client: TestClient) -> None:
    result = search(client, "陶瓷")
    product_id = result["results"][0]["id"]
    assert client.post("/api/search/click", json={"log_id": result["log_id"], "product_id": product_id}).status_code == 200
    assert client.post("/api/search/click", json={"log_id": result["log_id"], "product_id": 99999}).status_code == 404
    with client.app.state.database.session() as session:
        assert session.get(SearchLog, result["log_id"]).clicks == [product_id]
    login(client, "first")
    assert client.post("/api/search/click", json={"log_id": result["log_id"], "product_id": product_id}).status_code == 404


def test_embed_job_skips_unchanged_text_and_admin_can_rebuild(client: TestClient) -> None:
    embedded = calls(client, "embed")
    with client.app.state.database.session() as session:
        rows = list(session.scalars(select(ProductEmbedding)))
        assert len(rows) == 5 and {row.model for row in rows} == {"fake-embed"} and {row.dim for row in rows} == {64}
        product = session.scalar(select(Product).where(Product.product_code == "P001-001"))
        client.app.state.search.enqueue(session, product.id)
    assert client.app.state.jobs.wait_idle(10) and calls(client, "embed") == embedded
    # 描述改了，文本哈希变化才重新计算。
    with client.app.state.database.session() as session:
        product = session.scalar(select(Product).where(Product.product_code == "P001-001"))
        product.free_text = "换了一段描述"
        client.app.state.search.enqueue(session, product.id)
    assert client.app.state.jobs.wait_idle(10) and calls(client, "embed") == embedded + 1
    assert client.get("/api/admin/search-index").status_code == 403 and client.post("/api/admin/search-index/rebuild").status_code == 403
    login(client, "admin")
    status = client.get("/api/admin/search-index").json()
    assert status == {"backend": client.app.state.search.store.name, "model": "fake-embed", "products": 5, "vectors": 5, "dim": 64, "queued": 0}
    rebuilt = client.post("/api/admin/search-index/rebuild").json()
    assert rebuilt["vectors"] == 0 or rebuilt["queued"] <= 5
    assert client.app.state.jobs.wait_idle(10)
    assert client.get("/api/admin/search-index").json()["vectors"] == 5 and calls(client, "embed") == embedded + 6
    with client.app.state.database.session() as session:
        assert session.scalars(select(Oplog).where(Oplog.action == "重建全部向量")).one().after["queued"] == 5
        assert not list(session.scalars(select(Job).where(Job.kind == "embed", Job.status == "failed")))
    assert search(client, "帆布袋")["results"][0]["product_code"] == "P001-001"


def test_nothing_sent_to_models_contains_paths_codes_or_company_names(client: TestClient) -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(json.dumps(body, ensure_ascii=False))
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [1.0, 0.5, 0.25, 0.0]} for _ in body["input"]], "usage": {"total_tokens": 1}})
        prompt = body["messages"][0]["content"]
        answer = ({"ranking": [{"index": 2, "reason": "更贴合"}, {"index": 1, "reason": "也可以"}]} if "候选" in prompt
                  else {"fields": {"material": [{"keyword": "陶瓷", "synonyms": ["瓷器", "瓷"]}], "unknown_field": [{"keyword": "x"}]}, "exclude": [], "other": ["摆件"]})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer, ensure_ascii=False)}}], "usage": {"total_tokens": 1}})

    app = client.app
    config = app.state.ai_settings.effective().model_copy(update={"model_text": "text-model", "model_embed": "embed-model", "retries": 0})
    config.api_key_enc = type(config.api_key_enc)(app.state.cipher.encrypt("test-key"))
    real = AiClient(config, app.state.database, app.state.cipher, transport=httpx.MockTransport(handler))
    app.state.search.client_factory = lambda: real
    with app.state.database.session() as session:
        # 换成一个不会与普通词语混淆的企业名，再把它塞进商品名称和描述里。
        from app.db.models import CompanyIndustryMap
        company = session.scalars(select(CompanyIndustryMap)).one()
        company.company_name, company.aliases = "泸州老窖", ["老窖集团"]
        session.scalars(select(Project)).one().company_name = "泸州老窖"
        bottle = session.scalar(select(Product).where(Product.product_code == "P001-003"))
        bottle.display_name, bottle.free_text = "泸州老窖定制酒瓶", "老窖集团 P001 年会纪念 p001-003"
    login(client, "admin")
    assert client.post("/api/admin/search-index/rebuild").status_code == 200
    assert app.state.jobs.wait_idle(10)
    login(client, "one")
    result = search(client, "老窖集团 P001 的陶瓷摆件")
    assert not result["degraded"] and result["model_calls"] == 3 and len(result["results"]) == 5
    assert result["conditions"][0] == {"type": "company", "field": None, "label": "企业", "terms": ["泸州老窖"]}
    assert result["results"][0]["reason"] == "更贴合" and [item["field"] for item in result["conditions"] if item["type"] == "field"] == ["material"]
    # 5 次商品向量、1 次查询理解、1 次查询向量、1 次排序。
    assert len(sent) == 5 + 3
    for payload in sent:
        for secret in ("泸州老窖", "老窖集团", "P001", "p001", "设计资产", "工作台临时区", "01已中标项目", "01酒业", "192.168", "product_code", "link_id"):
            assert secret not in payload, (secret, payload[:300])
    ranking = next(payload for payload in sent if "候选" in payload)
    assert "1. 名称：" in ranking and "定制酒瓶" in ranking and "年会纪念" in ranking
    real.close()


def test_helpers_build_text_and_tolerate_model_output() -> None:
    assert product_text("酒瓶", [("品类", "摆件"), ("材质", " "), ("节日场景", "新年")], " 福字 ") == "名称：酒瓶\n品类：摆件\n节日场景：新年\n描述：福字"
    labels = {"festival": "节日场景", "size": "尺寸"}
    parsed = parse_conditions('```json\n{"fields":{"festival":[{"keyword":"过年","synonyms":["春节","新年","春节",1]},"元旦"],"nope":[{"keyword":"x"}],"size":"小"},"exclude":["塑料",""],"other":"不是列表"}\n```', labels)
    assert [(item.type, item.field, item.terms) for item in parsed] == [("field", "festival", ["过年", "春节", "新年"]), ("field", "festival", ["元旦"]), ("exclude", None, ["塑料"])]
    with pytest.raises(ValueError):
        parse_conditions("[]", labels)
