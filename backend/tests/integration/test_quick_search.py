"""快速匹配的正式数据、SQLite 查询计划及 AI 隔离验收。"""
from __future__ import annotations

import httpx
import pytest
from sqlalchemy import event, func, select

from app.db.models import AiCall, CompanyIndustryMap, Product, ProductTag, Project, SearchLog
from app.services.ai.client import AiClient
from app.services.ingest import vectors
from app.services.nas import NasPermissionDenied
from test_hybrid_search import published_search, search


@pytest.fixture
def assets(published_search):
    flow = published_search
    with flow.client.app.state.database.session() as session:
        project = session.scalar(select(Project))
        company = session.get(CompanyIndustryMap, project.company_id)
        company.aliases = ["酒坊别名"]
        products = {row.display_name: row for row in session.scalars(select(Product))}
        first = products["陶瓷红杯"]
        first.free_text = "适合长途旅行，带隔热护套。"
        changes = {
            "category": ("咖啡器具", ["咖啡器具"]),
            "material": ("瓷土、钢", ["白瓷", "AISI304"]),
            "size": ("250 mL", ["250", "mL"]),
        }
        for tag in session.scalars(select(ProductTag).where(ProductTag.product_id == first.id)):
            if tag.field_key in changes:
                tag.value, tag.terms = changes[tag.field_key]
                tag.confirmation_status = "confirmed_value"
            elif tag.field_key == "structure":
                tag.ai_value = "待核实暗扣"
                tag.value = "待核实暗扣"
                tag.terms = ["待核实暗扣"]
                tag.confirmation_status = "pending"
        second = products["磨砂雨伞"]
        second.display_name = "陶瓷红杯防护套"
        second.free_text = "适用于杯具展示。"
        codes = (project.project_code, first.product_code, second.product_code, project.company_name,
                 str(project.industry_id), str(project.company_id), project.project_name)
    return flow, codes


def quick(flow, term, **extra):
    result = search(flow, term, mode="quick", **extra)
    assert result["mode"] == "quick"
    assert not result["degraded"] and result["warnings"] == []
    assert result["usage"] == [] and result["vector_candidates"] == 0 and result["index_generation"] is None
    assert all(row["similarity"] is None for row in result["products"])
    return result


def test_quick_indexes_and_actual_match_fields(assets):
    flow, (project_code, code, second, name, industry, company, project_name) = assets
    assert quick(flow, code.lower())["products"][0]["product_code"] == code
    assert quick(flow, project_code)["projects"][0]["project_code"] == project_code
    assert quick(flow, project_name)["projects"][0]["reason"] == f"名称匹配：{project_name}"
    assert quick(flow, name)["projects"][0]["company_name"] == name
    assert quick(flow, "酒坊别名")["projects"][0]["reason"] == "企业匹配：酒坊别名"
    assert quick(flow, "酒坊别名 随便词")["projects"][0]["reason"].startswith("部分关键词匹配（1/2）")
    assert quick(flow, "酒业")["projects"][0]["reason"] == "行业匹配：酒业"
    assert quick(flow, "已中标")["projects"][0]["won"]
    items = quick(flow, "陶瓷红杯")["products"]
    assert [row["product_code"] for row in items[:2]] == [code, second]
    assert items[0]["reason"] == "名称匹配：陶瓷红杯"
    checks = {"咖啡器具": "品类匹配", "白瓷": "材质匹配", "ＡＩＳＩ３０４": "材质匹配",
              "250": "尺寸匹配", "隔热护套": "自由描述匹配"}
    for token, reason in checks.items():
        item = quick(flow, token)["products"][0]
        assert item["product_code"] == code and item["reason"].startswith(reason), item
    assert quick(flow, "套装")["products"][0]["kind"] == "set"
    for term in ("待核实暗扣", "陶瓷红杯陶瓷茶具", "、。"):
        plain = quick(flow, term)
        assert not plain["products"] and not plain["projects"], plain
    with flow.client.app.state.database.engine.connect() as con:
        for table, key in (("products", "product_code"), ("projects", "project_code")):
            plan = con.exec_driver_sql(f"EXPLAIN QUERY PLAN SELECT id FROM {table} WHERE {key} COLLATE NOCASE = ?", ("p001",))
            assert any(f"ix_{table}_search_code" in row[-1] for row in plan)


def test_coverage_filters_and_stable_sort(assets):
    flow, (project_code, code, second, _, industry, company, _) = assets
    args = {"won": True, "industry": industry, "company": company}
    result = quick(flow, "陶瓷、白瓷", **args)
    assert result["conditions"]["keywords"] == ["陶瓷", "白瓷"]
    assert [row["product_code"] for row in result["products"][:2]] == [code, second]
    assert "材质匹配：白瓷" in result["products"][0]["reason"]
    assert result["products"][1]["reason"].startswith("部分关键词匹配（1/2）")
    for _ in range(2):
        assert [row["id"] for row in quick(flow, "陶瓷、白瓷", **args)["products"]] == [
            row["id"] for row in result["products"]]
    for mode in ("quick", "semantic"):
        assert search(flow, code, mode=mode, **args)["products"]
        for overrides in ({"won": False}, {"industry": "99999"}, {"company": "99999"}):
            options = {**args, **overrides}
            assert not search(flow, code, mode=mode, **options)["products"]
            assert not search(flow, project_code, mode=mode, **options)["projects"]
    assert not quick(flow, "白瓷", remove=["keyword:白瓷"])["products"]


def test_quick_has_no_ai_or_vector_dependency(assets, monkeypatch):
    flow, (_, code, *_) = assets
    app = flow.client.app
    service = app.state.search
    attempted = []
    def forbidden(*args, **kwargs):
        attempted.append("AI")
        raise AssertionError("快速匹配触发了 AI 或向量组件")
    assert not app.state.settings.ai.api_key_enc.get_secret_value()
    for owner, attrs in ((service, ("_understand", "_vectors", "_rank", "_semantic_snapshot")),
                         (app.state.ai, ("client",)), (app.state.ai.settings, ("_effective",)),
                         (AiClient, ("search_chat", "embed")), (vectors, ("formal_model", "dimension_for", "text_hash")),
                         (service.store, ("top", "backend")), (httpx.Client, ("stream",))):
        for name in attrs:
            monkeypatch.setattr(owner, name, forbidden)
    monkeypatch.setattr("app.services.search.inputs_for", forbidden)
    with app.state.database.session() as session:
        before = session.scalar(select(func.count()).select_from(AiCall))
    statements = []
    def record(connection, cursor, statement, params, context, many):
        statements.append(statement.lower())
    event.listen(app.state.database.engine, "before_cursor_execute", record)
    try:
        quick(flow, code)
        result = quick(flow, "咖啡器具")
    finally:
        event.remove(app.state.database.engine, "before_cursor_execute", record)
    assert result["products"] and not attempted
    assert not any(name in statement for statement in statements
        for name in ("product_embeddings", "embedding_indexes", "query_cache", "ai_calls"))
    with app.state.database.session() as session:
        assert session.scalar(select(func.count()).select_from(AiCall)) == before
        record = session.get(SearchLog, result["search_id"])
        assert (record.mode, record.ai_usage, record.index_generation) == ("quick", [], None)
    report = flow.call("admin", "GET", "/api/admin/search-logs").json()
    assert report["items"][0]["mode"] == "quick"
    row = result["products"][0]
    for kind, identifier in (("product", row["id"]), ("project", row["project_id"])):
        body = {"search_id": result["search_id"], "kind": kind, "id": identifier}
        assert flow.call("one", "POST", "/api/search/click", body).status_code == 204


def test_quick_respects_file_acl_and_published_state(assets, monkeypatch):
    flow, (project_code, code, second, *_) = assets
    app = flow.client.app
    login = app.state.auth.context(flow.cookies["one"], "测试")
    fs = app.state.auth.pool.get(login.session_id)
    raw = fs._open_raw
    def restricted(path, mode):
        if path.endswith("陶瓷红杯.jpg"):
            raise NasPermissionDenied("模拟主图无读取权限")
        return raw(path, mode)
    monkeypatch.setattr(fs, "_open_raw", restricted)
    result = quick(flow, "陶瓷红杯")
    assert result["permission_filtered"]
    assert [item["product_code"] for item in result["products"]] == [second]
    with app.state.database.session() as session:
        session.scalar(select(Product).where(Product.product_code == second)).status = "voided"
    assert not quick(flow, second)["products"]
    with app.state.database.session() as session:
        session.scalar(select(Project).where(Project.project_code == project_code)).status = "ingesting"
    assert not quick(flow, project_code)["projects"]
    assert not quick(flow, "陶瓷红杯")["products"]


def test_login_invalid_mode_and_empty_catalog(published_search):
    flow = published_search
    no_cookie = flow.client.__class__(flow.client.app)
    try:
        assert no_cookie.post("/api/search", json={"query": "茶杯", "mode": "quick"}).status_code == 401
    finally:
        no_cookie.close()
    assert flow.call("one", "POST", "/api/search", {"query": "茶杯", "mode": "automatic"}).status_code == 422
    with flow.client.app.state.database.session() as session:
        session.scalar(select(Project)).status = "voided"
    for mode in ("quick", "semantic"):
        found = search(flow, "没有的商品", mode=mode)
        assert found["mode"] == mode and found["products"] == [] and found["projects"] == []
    legacy = search(flow, "没有的商品")
    assert legacy["mode"] == "semantic"


def test_migration_preserves_original_searches_and_clicks(tmp_path):
    from alembic import command
    from alembic.config import Config
    from app.db.database import BACKEND_DIR, Database

    db = Database(tmp_path)
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    with db.engine.begin() as con:
        con.exec_driver_sql("BEGIN IMMEDIATE")
        config.attributes["connection"] = con
        command.upgrade(config, "0021_pre_descriptions")
        con.exec_driver_sql("""
            INSERT INTO users (id, created_at, updated_at, username, roles, enabled)
            VALUES (2, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, '测试', '[]', 1)
        """)
        con.exec_driver_sql("""
            INSERT INTO search_logs (id, ts, user_id, query, parsed, result_count, duration_ms, index_generation, ai_usage, warnings, targets)
            VALUES (7, CURRENT_TIMESTAMP, 2, '历史茶杯', '{}', 1, 30, 8, '[{"kind":"search_rank"}]', '[]', '{"product":[9]}')
        """)
        con.exec_driver_sql("""
            INSERT INTO search_clicks (id, search_id, user_id, ts, target_kind, target_id)
            VALUES (10, 7, 2, CURRENT_TIMESTAMP, 'product', 9)
        """)
    db.migrate()
    with db.engine.connect() as con:
        assert con.exec_driver_sql("SELECT id, query, index_generation, mode, json_extract(ai_usage, '$[0].kind') FROM search_logs").one() == (
            7, "历史茶杯", 8, "semantic", "search_rank")
        assert con.exec_driver_sql("SELECT id, search_id, target_id FROM search_clicks").one() == (10, 7, 9)
        assert not con.exec_driver_sql("PRAGMA foreign_key_check").all()
    db.close()
