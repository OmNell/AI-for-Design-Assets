from threading import Event

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import CatalogFile, Link, Product, ProductTag
from app.main import create_app
from test_maintenance import maintained

BASE = "/api/admin/recovery"


def login(client, username):
    response = client.post("/api/auth/login", json={"username": username, "password": "fake"})
    assert response.status_code == 200, response.text


def finish(client, action, body=None):
    response = client.post(f"{BASE}/{action}", json=body)
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    assert client.app.state.jobs.wait_idle(8)
    report = client.get(f"{BASE}/reports/{identifier}")
    assert report.status_code == 200, client.get(BASE).json()
    return identifier, report.json()


def test_fresh_workbench_recovers_published_catalog_through_admin_api(maintained, tmp_path):
    flow, project, settings, config = maintained
    with flow.client.app.state.database.session() as original:
        product = original.scalar(select(Product).where(Product.project_id == project.id))
        file = original.get(CatalogFile, product.file_id)
        link = original.scalar(select(Link.link_id).where(Link.kind == "product", Link.target_id == product.id))
        tags = {row.field_key: (row.value, row.confirmation_status) for row in original.scalars(
            select(ProductTag).where(ProductTag.product_id == product.id))}
    destination = settings.model_copy(deep=True)
    destination.paths.data_dir = tmp_path / "fresh-archive-workbench"
    destination.recovery.read_only = True
    with TestClient(create_app(destination, config_file=config)) as client:
        assert client.get(BASE).status_code == 401
        login(client, "one")
        assert client.get(BASE).status_code == 403
        assert client.get(BASE + "/reports/1").status_code == 403
        assert client.post(BASE + "/scan").status_code == 403
        login(client, "admin")
        state = client.get(BASE).json()
        assert state["catalog_state"] == "empty_unconfirmed" and not state["can_backup"]
        _, check = finish(client, "detect")
        assert check["nas_state"] == "sampled"
        assert client.get(BASE).json()["catalog_state"] == "needs_scan"
        identifier, plan = finish(client, "scan")
        assert [(row["project_id"], row["kind"]) for row in plan["items"]] == [(project.id, "missing")]
        assert client.post(BASE + "/apply", json={"plan_id": identifier}).status_code == 422
        assert client.post(BASE + "/apply", json={"plan_id": 9999, "confirmed": True}).status_code == 409
        _, result = finish(client, "apply", {"plan_id": identifier, "confirmed": True})
        assert result["recovered"] == [project.id] and not result["blocked"]
        assert client.get(BASE).json()["projects"] == 1
        _, repeated = finish(client, "apply", {"plan_id": identifier, "confirmed": True})
        assert repeated["skipped"] == [project.id] and not repeated["recovered"]
        login(client, "one")
        assert client.get(f"/api/projects/{project.id}").status_code == 200
        assert client.get(f"/api/products/{product.id}").status_code == 200
        assert client.get(f"/l/{link}").json()["target_id"] == product.id
        assert client.get(f"/api/files/{file.id}/preview").status_code == 200
        download = client.get(f"/api/files/{file.id}/download")
        assert download.status_code == 200 and download.headers["etag"] == f'"{file.fingerprint}"'
        query = client.post("/api/search", json={"mode": "quick", "query": product.product_code}).json()
        assert [row["id"] for row in query["products"]] == [product.id]
        assert not query["usage"]
        with client.app.state.database.session() as restored:
            assert restored.get(CatalogFile, file.id).fingerprint == file.fingerprint
            assert {row.field_key: (row.value, row.confirmation_status) for row in restored.scalars(
                select(ProductTag).where(ProductTag.product_id == product.id))} == tags


def test_second_recovery_operation_returns_conflict_while_scan_running(maintained, monkeypatch):
    from app.services.recovery import admin

    flow, _, _, _ = maintained
    entered, release = Event(), Event()
    original = admin.plan_incremental

    def blocked(*args):
        entered.set()
        release.wait(4)
        return original(*args)

    monkeypatch.setattr(admin, "plan_incremental", blocked)
    assert flow.call("admin", "POST", BASE + "/scan").status_code == 202
    try:
        assert entered.wait(2)
        assert flow.call("admin", "POST", BASE + "/detect").status_code == 409
    finally:
        release.set()
    assert flow.client.app.state.jobs.wait_idle(8)
