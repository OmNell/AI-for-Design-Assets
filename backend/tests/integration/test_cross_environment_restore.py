"""同一模拟 NAS 上演练原工作台、备份恢复和仅凭正式档案恢复。"""
from hashlib import sha256
import json
from pathlib import Path
import struct

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import load_settings
from app.db.models import Batch, ProductEmbedding, Project
from app.main import create_app
from app.services import archive
from app.services.archive import publication
from app.services.auth.service import COOKIE_NAME
from app.services.nas import Region
from app.services.nas.factory import create_paths
from test_ai_pipeline import confirm, uploaded
from test_ingest import progress, start
from test_recovery_admin import BASE, finish, login
from test_review import confirm_fields
from test_two_stage_review import ALL, CATALOG, Flow, configure


def snapshot(client, project_id):
    with client.app.state.database.session() as session:
        catalog = archive.from_catalog(session, session.get(Project, project_id))
        products = [row["id"] for row in catalog["products"]]
        vectors = {row.product_id: (row.model_id, row.model_version, row.index_generation, row.text_hash,
                                     row.dimension, row.state, row.embedding)
                   for row in session.scalars(select(ProductEmbedding).where(ProductEmbedding.product_id.in_(products)))}
    return catalog, vectors


def assert_access(client, catalog, embeddings, monkeypatch):
    project = catalog["project"]
    item = next(row for row in catalog["products"] if row["kind"] == "item")
    file = next(row for row in catalog["files"] if row["id"] == item["file_id"])
    project_url, product_url = f"/api/projects/{project['id']}", f"/api/products/{item['id']}"
    preview_url = f"/api/files/{file['id']}/preview"
    download_url = f"/api/files/{file['id']}/download"

    login(client, "one")
    assert client.get("/api/projects").json()["total"] == 1
    assert client.get(project_url).json()["project"]["project_code"] == project["project_code"]
    product = client.get(product_url).json()
    assert product["product_code"] == item["product_code"] and len(product["tags"]) == 11
    assert [row["id"] for row in product["related"]] == [catalog["relations"][0]["set_product_id"]]
    for link in catalog["links"]:
        resolved = client.get(f"/api/l/{link['link_id']}")
        assert resolved.status_code == 200
        assert resolved.json()["target_id"] == link["target_id"]
    preview = client.get(preview_url)
    assert preview.status_code == 200 and preview.headers["content-type"] == "image/jpeg"
    download = client.get(download_url)
    assert download.status_code == 200
    assert sha256(download.content).hexdigest() == file["fingerprint"]

    quick_query = {"query": item["product_code"], "mode": "quick"}
    quick = client.post("/api/search", json=quick_query).json()
    assert [row["id"] for row in quick["products"]] == [item["id"]] and not quick["usage"]

    # 固定查询端模型结果，保留真实向量读取、相似度计算、排序和权限检查。
    dimension, blob = embeddings[item["id"]][4], embeddings[item["id"]][6]
    query_vector = list(struct.unpack(f"<{dimension}f", blob))
    ai = client.app.state.ai.client()
    with monkeypatch.context() as patch:
        patch.setattr(ai, "embed", lambda *_: query_vector)
        patch.setattr(ai, "search_chat", lambda task, _: json.dumps(
            {"fields": {}, "keywords": [], "exclude": []} if task == "query"
            else {"items": [{"index": 1, "reason": "与已入库向量匹配"}]}))
        semantic = client.post("/api/search", json={"query": "查找已确认的杯类商品", "mode": "semantic"}).json()
    assert semantic["vector_candidates"] > 0 and not semantic["exact"], semantic
    assert item["id"] in {row["id"] for row in semantic["products"]}

    login(client, "other")
    assert client.get("/api/projects").json()["total"] == 0
    for url in (project_url, product_url, preview_url, download_url):
        assert client.get(url).status_code == 403
    assert client.post("/api/search", json=quick_query).json()["products"] == []


def publish_after_cutover(client):
    reviewer_ids = {}
    for reviewer in ("first", "final"):
        login(client, reviewer)
        reviewer_ids[reviewer] = client.get("/api/auth/me").json()["id"]
    login(client, "admin")
    for reviewer, user_id in reviewer_ids.items():
        response = client.put("/api/admin/roles", json={"user_id": user_id, "roles": ["uploader", f"{reviewer}_reviewer"],
                                                       "enabled": True})
        assert response.status_code == 200, response.text
    assert client.put("/api/admin/company-map", json=CATALOG).status_code == 200

    login(client, "one")
    batch = confirm(client, uploaded(client, ["单品/新水杯.jpg"], project_code="P002"))
    assert batch["status"] == "待核对"
    batch_url = f"/api/batches/{batch['id']}"
    for product in client.get(batch_url + "/products").json()["products"]:
        confirm_fields(client, batch_url, product)
    assert client.post(batch_url + "/submit", json={}).status_code == 200

    review_url = f"/api/review/{batch['id']}"
    for reviewer in reviewer_ids:
        login(client, reviewer)
        version = client.get(review_url).json()["version"]
        claim = client.post(review_url + "/claim", headers={"X-Review-Version": version})
        assert claim.status_code == 200, claim.text
        accepted = client.post(review_url + "/pass", json=ALL, headers={"X-Review-Version": claim.json()["version"]})
        assert accepted.status_code == 200, accepted.text
    assert client.app.state.jobs.wait_idle(8)
    assert client.get(review_url + "/ingest").json()["status"] == "已入库"
    with client.app.state.database.session() as session:
        return session.get(Batch, batch["id"]).project_id


def object_ids(catalog):
    return {catalog["project"]["id"], *(row["id"] for row in catalog["products"] + catalog["files"])}


def test_same_nas_recovers_from_backup_then_from_published_archives(settings, tmp_path, monkeypatch):
    configure(settings, tmp_path)
    settings.paths.data_dir = tmp_path / "a" / "data"
    for user in settings.dev_users:
        user.permissions["formal"] = "write" if user.username == "final" else "none" if user.username == "other" else "read"
    source_config = tmp_path / "source.yaml"
    source_config.write_text("mode: dev\n", encoding="utf-8")
    nas_paths = create_paths(settings)

    with TestClient(create_app(settings, config_file=source_config)) as source:
        flow = Flow(source, ["单品/栗色杯.jpg", "套装/栗色杯礼装.jpg", "01定稿文件/定稿文件.ai"])
        start(flow)
        assert source.app.state.jobs.wait_idle(8)
        assert progress(flow)["status"] == "已入库"
        with source.app.state.database.session() as session:
            project_id = session.get(Batch, flow.bid).project_id
        expected, embeddings = snapshot(source, project_id)
        assert len(expected["files"]) == 3 and len(expected["products"]) == len(embeddings) == 2
        assert len(expected["tags"]) == 22 and len(expected["relations"]) == 1 and len(expected["links"]) == 6
        assert_access(source, expected, embeddings, monkeypatch)

        assert flow.call("admin", "POST", "/api/admin/maintenance/backup/run").status_code == 202
        assert source.app.state.jobs.wait_idle(8)
        backup, = flow.call("admin", "GET", "/api/admin/backups").json()
        source_token = flow.cookies["one"]

    blank = settings.model_copy(deep=True)
    blank.paths.data_dir = tmp_path / "b" / "initial"
    blank.recovery.read_only = True
    with TestClient(create_app(blank, config_file=source_config)) as destination:
        login(destination, "admin")
        assert destination.get(BASE).json()["catalog_state"] == "empty_unconfirmed"
        _, detection = finish(destination, "detect")
        assert destination.get(BASE).json()["catalog_state"] == "needs_scan"
        candidate, = detection["backups"]
        assert candidate["name"] == backup["name"] and candidate["validation"] == "manifest_checked"
        _, staged = finish(destination, "stage", {"backup_name": backup["name"], "confirmed": True})
        assert staged["source_inventory"] == staged["inventory"] and staged["sessions_revoked"] > 0
        assert [row["kind"] for row in staged["difference"]["items"]] == ["same"]

    staged_config = Path(staged["stage_dir"]) / "config.yaml"
    recovered = load_settings(staged_config)
    assert recovered.paths.data_dir != settings.paths.data_dir and recovered.paths.dev_nas_dir == settings.paths.dev_nas_dir
    with TestClient(create_app(recovered, config_file=staged_config)) as destination:
        old = destination.get("/api/auth/me", headers={"Cookie": f"{COOKIE_NAME}={source_token}"})
        assert old.status_code == 401
        catalog, vectors = snapshot(destination, project_id)
        assert publication.digest(catalog) == publication.digest(expected)
        assert vectors == embeddings
        assert_access(destination, expected, vectors, monkeypatch)

    # 仅移动本次演练生成的备份确保证明同一 NAS 没有可选备份。
    backup_file = settings.paths.dev_nas_dir / nas_paths.join(Region.BACKUP, backup["name"])
    backup_file.rename(tmp_path / backup["name"])
    rebuilt = settings.model_copy(deep=True)
    rebuilt.paths.data_dir = tmp_path / "c" / "data"
    rebuilt.recovery.read_only = True
    with TestClient(create_app(rebuilt, config_file=source_config)) as destination:
        login(destination, "admin")
        assert destination.get(BASE).json()["catalog_state"] == "empty_unconfirmed"
        _, detection = finish(destination, "detect")
        assert detection["backup_state"] == "checked" and detection["backups"] == []
        plan_id, plan = finish(destination, "scan")
        assert [(row["project_id"], row["kind"]) for row in plan["items"]] == [(project_id, "missing")]
        _, restored = finish(destination, "apply", {"plan_id": plan_id, "confirmed": True})
        assert restored["recovered"] == [project_id] and not restored["blocked"]
        catalog, vectors = snapshot(destination, project_id)
        assert publication.digest(catalog) == publication.digest(expected)
        assert vectors == embeddings
        assert_access(destination, expected, vectors, monkeypatch)

    # 停止核验实例后，仅开启 C 的模拟 NAS 写入并由管理员重新授予审核角色。
    rebuilt.recovery.read_only = False
    with TestClient(create_app(rebuilt, config_file=source_config)) as destination:
        new_id = publish_after_cutover(destination)
        new_catalog, new_vectors = snapshot(destination, new_id)
        assert new_catalog["project"]["project_code"] == "P002"
        assert [row["product_code"] for row in new_catalog["products"]] == ["P002-001"]
        assert min(object_ids(new_catalog)) > max(object_ids(expected))
        assert len(new_vectors) == 1 and all(value[5] == "ready" for value in new_vectors.values())
        catalog, vectors = snapshot(destination, project_id)
        assert publication.digest(catalog) == publication.digest(expected)
        assert vectors == embeddings
