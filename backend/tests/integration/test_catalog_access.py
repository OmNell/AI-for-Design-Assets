"""通过 WP09 正常发布的项目验证 WP10 访问链路。"""
import hashlib
from io import BytesIO
import socket
from threading import Thread
import time
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import httpx
from PIL import Image
import pytest
from sqlalchemy import select
import uvicorn

from app.db.models import AccessLog, Batch, CatalogFile, InspectionFinding, Link, Product, ProductTag, Project
from app.services.auth.service import COOKIE_NAME
from app.services.nas import Region
from app.services.nas.factory import create_paths
from test_ai_pipeline import confirm, uploaded
from test_ingest import app_for, progress, start
from test_two_stage_review import Flow


@pytest.fixture
def published(settings, tmp_path):
    app = app_for(settings, tmp_path)
    app.state.static_index = tmp_path / "index.html"
    app.state.static_index.write_text("<html><title>设计资产工作台</title></html>", encoding="utf-8")
    next(user for user in settings.dev_users if user.username == "other").permissions["formal"] = "none"
    with TestClient(app) as client:
        flow = Flow(client, ["单品/杯.jpg", "套装/A.jpg", "套装/B.jpg", "01定稿文件/中文设计.ai", "05 详情页/辅图.png"])
        start(flow)
        assert app.state.jobs.wait_idle(8) and progress(flow)["status"] == "已入库"
        with app.state.database.session() as session:
            project = session.get(Project, session.get(Batch, flow.bid).project_id)
            files = {row.rel_path: row for row in session.scalars(select(CatalogFile))}
            products = {row.kind: row for row in session.scalars(select(Product))}
            links = {(row.kind, row.target_id): row.link_id for row in session.scalars(select(Link))}
        root = settings.paths.dev_nas_dir / create_paths(settings).root(Region.FORMAL) / project.current_path
        yield flow, project, root, files, products, links


def test_catalog_search_filters_and_detail(published):
    flow, project, _, files, products, _ = published
    for query in ("q=P001", "q=项目", "q=酒企", "q=企业", "company=酒企", "won=true&industry=酒业", "sort=code"):
        response = flow.call("one", "GET", "/api/projects?" + query)
        assert response.status_code == 200, response.text
        item = response.json()["items"][0]
        assert item["id"] == project.id and item["products"] == 3 and item["files"] == 5
        assert item["link_url"].startswith("http://assets.example.local:8080/l/")
    assert flow.call("one", "GET", "/api/projects?won=false").json()["total"] == 0
    assert flow.call("one", "GET", "/api/projects?page=2&page_size=1").json()["items"] == []
    assert flow.call("one", "GET", "/api/projects?sort=invalid").status_code == 422
    assert flow.call("other", "GET", "/api/projects").json()["items"] == []
    base = f"/api/projects/{project.id}"
    detail = flow.call("one", "GET", base).json()
    assert {f["name"]:f["count"] for f in detail["folders"]} == {
        "01定稿文件": 1, "02设计方案": 0, "03拍摄照片及视频": 0, "04授权文件": 0, "05 详情页": 1}
    listing = flow.call("one", "GET", base + "/files?category=01定稿文件").json()
    assert [row["name"] for row in listing] == ["中文设计.ai"]
    assert flow.call("one", "GET", base + "/files?category=不存在").status_code == 422
    item = flow.call("one", "GET", f"/api/products/{products['item'].id}").json()
    assert len(item["tags"]) == 11 and len(item["related"]) == 2
    assert next(tag for tag in item["tags"] if tag["key"] == "festival")["confirmation_status"] == "confirmed_unknown"
    assert all(row["related"] == [item["id"]] for row in detail["products_list"] if row["kind"] == "set")
    assert flow.call("other", "GET", base).status_code == 403
    for state in ("ingesting", "ingest_error", "voided", "deleted"):
        with flow.client.app.state.database.session() as session:
            session.get(Project, project.id).status = state
        assert flow.call("one", "GET", "/api/projects").json()["total"] == 0
        assert flow.call("one", "GET", base).status_code == 404
        assert flow.call("one", "GET", f"/api/products/{products['item'].id}").status_code == 404


def test_review_tag_suggestions_merge_authorized_published_values_with_draft(published):
    flow, project, _, _, products, _ = published
    db = flow.client.app.state.database
    with db.session() as session:
        rows = session.scalars(select(ProductTag).join(Product).where(
            Product.project_id == project.id, ProductTag.field_key == "material").order_by(Product.id)).all()
        rows[0].terms = ["历史橡木", "历史橡木", "历史柚木", "ABS历史塑料"]
        rows[1].terms = ["历史橡木", *(f"历史材质{i:02}" for i in range(25))]
        rows[2].terms, rows[2].confirmation_status = ["历史未确认"], "pending"

    url = "/api/tag-values?field=material"
    oak = [{"term": "历史橡木", "count": 2}]
    assert flow.call("one", "GET", url + "&q=橡木").json() == oak
    assert flow.call("one", "GET", url + "&q=abs").json() == [{"term": "ABS历史塑料", "count": 1}]
    ranked = flow.call("one", "GET", url + "&q=历史").json()
    assert len(ranked) == 20 and ranked[:1] == oak
    assert all(row["term"] != "历史未确认" for row in ranked)
    assert flow.call("one", "GET", url + "&q=未确认").json() == []
    assert flow.call("other", "GET", url + "&q=历史").json() == []

    flow.sign_in("one")
    draft = confirm(flow.client, uploaded(flow.client, ["单品/新杯.jpg"], project_code="P002"))
    product = draft["products"][0]
    response = flow.call("one", "PATCH", f"/api/batches/{draft['id']}/products/{product['id']}",
                         {"tags": {"material": "历史橡木、历史橡木、历史草稿用词"}})
    assert response.status_code == 200, response.text
    draft_url = url + f"&batch_id={draft['id']}"
    assert flow.call("one", "GET", draft_url + "&q=橡木").json() == [{"term": "历史橡木", "count": 3}]
    assert flow.call("one", "GET", draft_url + "&q=草稿").json() == [{"term": "历史草稿用词", "count": 1}]
    assert flow.call("other", "GET", draft_url + "&q=草稿").status_code == 403

    with db.session() as session:
        session.get(Product, products["item"].id).status = "voided"
    assert flow.call("one", "GET", url + "&q=柚木").json() == []
    with db.session() as session:
        session.get(Project, project.id).status = "voided"
    assert flow.call("one", "GET", url + "&q=历史").json() == []
    assert flow.call("one", "GET", draft_url + "&q=橡木").json() == [{"term": "历史橡木", "count": 1}]


def test_stream_ranges_headers_pause_and_transfer_log(published):
    flow, _, root, files, _, _ = published
    path = "01定稿文件/中文设计.ai"
    fid = files[path].id
    body = (root / path).read_bytes()
    cookie = {COOKIE_NAME: flow.cookies["one"]}
    check = flow.call("one", "GET", f"/api/files/{fid}/check").json()
    url = check["url"]
    transfer = parse_qs(urlsplit(url).query)["transfer"][0]
    client = flow.client
    headers = {"Cookie": f"{COOKIE_NAME}={cookie[COOKIE_NAME]}"}
    full = client.get(url, headers=headers)
    assert full.content == body and int(full.headers["Content-Length"]) == len(body)
    assert "filename*=UTF-8''%E4%B8%AD" in full.headers["Content-Disposition"]
    assert full.headers["accept-ranges"] == "bytes"
    for spec, expected in [("bytes=1-3", body[1:4]), ("bytes=6-", body[6:]), ("bytes=-8", body[-8:])]:
        part = client.get(url, headers={**headers, "Range": spec, "If-Range": full.headers["etag"]})
        assert part.status_code == 206 and part.content == expected
        assert int(part.headers["content-length"]) == len(expected) and part.headers["content-range"].startswith("bytes ")
    result = client.get(url, headers={**headers, "Range": f"bytes={len(body)}-"})
    assert result.status_code == 416 and result.headers["content-range"] == f"bytes */{len(body)}"
    assert client.get(url, headers={**headers, "Range": "bytes=0-1,3-4"}).status_code == 416
    mismatch = client.get(url, headers={**headers, "Range": "bytes=0-9", "If-Range": '"old"'})
    assert mismatch.status_code == 200 and mismatch.content == body
    assert client.head(url, headers=headers).content == b""
    assert flow.call("one", "GET", f"/api/files/{fid}/downloads/{transfer}").json()["status"] == "sent"
    assert flow.call("final", "GET", f"/api/files/{fid}/downloads/{transfer}").json()["status"] == "pending"
    with client.app.state.database.session() as session:
        rows = list(session.scalars(select(AccessLog).where(AccessLog.kind == "download", AccessLog.outcome == "allowed")))
        assert len(rows) == 1


def test_thumbnail_decode_failure_returns_placeholder_only_after_access_check(published, monkeypatch):
    from app.services.catalog import media

    flow, _, _, _, products, _ = published
    product = products["item"]
    def unreadable(*args):
        raise media.ImageDecodeError("模拟图片无法解码")
    monkeypatch.setattr(media.MediaService, "_official_thumb", lambda *args: None)
    monkeypatch.setattr(media, "render_variants", unreadable)
    url = f"/api/thumbs/{product.id}/300"
    assert flow.call("other", "GET", url).status_code == 403
    response = flow.call("one", "GET", url)
    assert response.status_code == 200
    assert response.headers["X-Asset-Thumbnail"] == "placeholder"
    with Image.open(BytesIO(response.content)) as image:
        assert image.format == "JPEG" and image.size == (300, 300)
    assert flow.call("one", "GET", f"/api/files/{product.file_id}/preview").json()["code"] == "image_invalid"


def test_preview_authorizes_even_when_warm_and_logs_once(published):
    flow, _, _, files, products, _ = published
    fid = files["单品/杯.jpg"].id
    product = products["item"]
    thumb = f"/api/thumbs/{product.id}/300?transfer=preview_test_token"
    assert flow.call("one", "GET", thumb).content.startswith(b"\xff\xd8")
    assert flow.call("other", "GET", thumb).status_code == 403
    assert flow.call("other", "GET", f"/api/files/{fid}/download").status_code == 403
    preview = f"/api/files/{fid}/preview?transfer=preview_test_token"
    assert flow.call("one", "GET", preview).status_code == 200
    assert flow.call("one", "GET", preview + "&original=true").headers["content-type"] == "image/jpeg"
    with flow.client.app.state.database.session() as session:
        assert len(list(session.scalars(select(AccessLog).where(AccessLog.kind == "preview",
            AccessLog.target_id == fid, AccessLog.outcome == "allowed")))) == 1


def test_permanent_link_compatibility_history_and_mixed_access(published):
    flow, project, _, files, products, links = published
    file = files["01定稿文件/中文设计.ai"]
    for kind, obj in (("project", project), ("product", products["item"]), ("file", file)):
        short = links[kind, obj.id]
        original = flow.call("one", "GET", f"/l/{short}")
        assert original.status_code == 200 and original.json()["target_id"] == obj.id
        assert flow.call("one", "GET", f"/api/l/{short}").json() == original.json()
        browser = flow.client.get(f"/l/{short}", headers={"Accept": "text/html", "Cookie": ""})
        assert browser.status_code == 200 and "设计资产工作台" in browser.text
    file_link = "/api/l/" + links["file", file.id]
    db = flow.client.app.state.database
    with db.session() as session:
        session.get(CatalogFile, file.id).status = "voided"
    assert flow.call("one", "GET", file_link).json()["status"] == "voided"
    assert flow.call("other", "GET", file_link).status_code == 403
    assert flow.call("one", "GET", f"/api/files/{file.id}/download").headers["x-asset-state"] == "voided"
    with db.session() as session:
        session.get(CatalogFile, file.id).status = "deleted"
    assert flow.call("one", "GET", file_link).json()["status"] == "deleted"
    assert flow.call("one", "GET", f"/api/files/{file.id}/download").status_code == 410
    good = files["单品/杯.jpg"].id
    results = flow.call("one", "POST", "/api/files/download-many", {"file_ids":[file.id, good, 9321]}).json()
    assert [row["code"] for row in results] == ["file_deleted", None, "file_not_found"]
    assert flow.call("one", "GET", results[1]["url"]).status_code == 200
    denied = flow.call("other", "POST", "/api/files/download-many", {"file_ids":[good]}).json()
    assert denied[0]["url"] is None and denied[0]["code"] == "nas_permission_denied"


def test_four_level_recovery_and_content_drift(published):
    flow, project, root, files, _, _ = published
    original = files["01定稿文件/中文设计.ai"]
    original_path = root / original.rel_path
    original_bytes = original_path.read_bytes()
    branch = root.parents[3] / "02未中标项目" / "02另一行业" / "01另一企业"
    branch.mkdir(parents=True)
    moved = branch / root.name
    root.rename(moved)
    url = f"/api/files/{original.id}/download"
    assert flow.call("one", "GET", url).content == original_bytes
    db = flow.client.app.state.database
    with db.session() as session:
        row = session.get(Project, project.id)
        assert row.current_path.startswith("02未中标项目")
        assert row.won is True and row.company_name == project.company_name and row.industry_id == project.industry_id

    source = moved / original.rel_path
    next_path = moved / "05 详情页" / "改名.ai"
    source.rename(next_path)
    copy = moved / "05 详情页" / "同内容副本.ai"
    copy.write_bytes(original_bytes)
    assert flow.call("one", "GET", url).status_code == 404
    copy.unlink()
    assert flow.call("one", "GET", url).content == original_bytes
    altered = b"X" + original_bytes[1:]
    next_path.write_bytes(altered)
    denied = flow.call("one", "GET", url)
    assert denied.status_code == 409 and denied.json()["code"] == "content_changed"
    warned = flow.call("one", "GET", url + "?allow_changed=true")
    assert warned.content == altered and warned.headers["x-asset-warning"] == "content_changed"
    with db.session() as session:
        assert session.get(CatalogFile, original.id).fingerprint == original.fingerprint
        assert session.get(CatalogFile, original.id).category == "01定稿文件"
        assert {finding.kind for finding in session.scalars(select(InspectionFinding))} == {"moved", "missing", "modified"}
    assert flow.call("one", "GET", "/api/admin/asset-findings").status_code == 403
    assert len(flow.call("admin", "GET", "/api/admin/asset-findings").json()) == 4


def test_two_gib_over_http_streamed_with_sha_and_cleanup(published):
    flow, project, root, _, _, _ = published
    path = root / "01定稿文件" / "大文件.bin"
    size = 2 * 1024 ** 3
    with path.open("wb") as stream:
        stream.truncate(size)
    expected = hashlib.sha256()
    zeros = bytes(1024 * 1024)
    for _ in range(2048):
        expected.update(zeros)
    with flow.client.app.state.database.session() as session:
        file = CatalogFile(project_id=project.id, rel_path="01定稿文件/大文件.bin", size=size,
                           mtime=int(path.stat().st_mtime * 1000), fingerprint=expected.hexdigest(), category="01定稿文件")
        session.add(file)
        session.flush()
        fid = file.id
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(flow.client.app, lifespan="off", log_level="warning", access_log=False))
    thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        actual = hashlib.sha256()
        bytes_read = 0
        with httpx.stream("GET", f"http://127.0.0.1:{port}/api/files/{fid}/download", timeout=30,
                          cookies={COOKIE_NAME: flow.cookies["one"]}) as response:
            assert response.status_code == 200 and int(response.headers["content-length"]) == size
            for part in response.iter_bytes(1024 * 1024):
                actual.update(part)
                bytes_read += len(part)
        assert bytes_read == size and actual.digest() == expected.digest()
        transfer = "interrupt_large_file"
        with httpx.stream("GET", f"http://127.0.0.1:{port}/api/files/{fid}/download?transfer={transfer}",
                          timeout=30, cookies={COOKIE_NAME: flow.cookies["one"]}) as response:
            assert response.status_code == 200
            assert next(response.iter_bytes(1024 * 1024))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = flow.call("one", "GET", f"/api/files/{fid}/downloads/{transfer}").json()["status"]
            if state == "interrupted":
                break
            time.sleep(0.05)
        assert state == "interrupted"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
    assert not flow.client.app.state.auth.pool._pins


def test_download_many_preserves_partial_file_acl_failures(published, monkeypatch):
    from app.services.nas import NasPermissionDenied

    flow, _, _, files, _, _ = published
    user = flow.client.app.state.auth.context(flow.cookies["one"], "测试")
    fs = flow.client.app.state.auth.pool.get(user.session_id)
    original = fs._open_raw
    opened = fs.permissions
    assert opened[Region.FORMAL] == "read"

    def per_file_acl(path, mode):
        if path.endswith("/中文设计.ai"):
            raise NasPermissionDenied("测试中的文件访问控制")
        return original(path, mode)

    monkeypatch.setattr(fs, "_open_raw", per_file_acl)
    rejected = files["01定稿文件/中文设计.ai"].id
    allowed = files["单品/杯.jpg"].id
    result = flow.call("one", "POST", "/api/files/download-many", {"file_ids": [rejected, allowed]})
    assert result.status_code == 200
    first, second = result.json()
    assert first["code"] == "nas_permission_denied" and first["url"] is None
    assert flow.call("one", "GET", second["url"]).status_code == 200
    assert flow.call("one", "GET", f"/api/files/{rejected}/download").status_code == 403
