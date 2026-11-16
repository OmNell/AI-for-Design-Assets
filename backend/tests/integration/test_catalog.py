"""WP10 项目页、下载、四层查找与永久链接。"""
from __future__ import annotations

from collections.abc import Iterator
import hashlib
import os
from pathlib import Path
import shutil

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.config import DevUser, PathsConfig, Settings
from app.db.models import CatalogFile, InspectionFinding, Link, Oplog, Product, Project, User
from app.main import create_app
from app.services.catalog import content_disposition, parse_range
from app.errors import AppError
from tests.integration.test_audit import login
from tests.integration.test_ingest import FOLDER, PATHS, ROLES, TARGET, approve, to_final
from tests.integration.test_review import jpeg

FORMAL = {"final": "write", "outsider": "none"}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(mode="dev", paths=PathsConfig(data_dir=tmp_path / "data", dev_nas_dir=tmp_path / "nas"), bootstrap_admin="admin")
    names = [*ROLES, "outsider"]
    settings.dev_users = [DevUser(username=name, password="fake", permissions={"temp": "write", "formal": FORMAL.get(name, "read")}) for name in names]
    with TestClient(create_app(settings, log_dir=tmp_path / "logs")) as client:
        for name in names:
            login(client, name)
        with client.app.state.database.session() as session:
            for user in session.scalars(select(User)):
                user.roles = ROLES.get(user.username, ["uploader"])
        login(client, "admin")
        assert client.put("/api/admin/company-map", json={"industries": [{"name": "酒业"}, {"name": "金融"}],
            "companies": [{"name": "企业", "industry_name": "酒业"}, {"name": "招商银行", "industry_name": "金融"}]}).status_code == 200
        approve(client, to_final(client))
        login(client, "one")
        yield client


def file_id(client: TestClient, rel_path: str) -> int:
    with client.app.state.database.session() as session:
        return session.scalar(select(CatalogFile.id).where(CatalogFile.rel_path == rel_path))


def project_row(client: TestClient) -> tuple[int, str]:
    with client.app.state.database.session() as session:
        project = session.scalars(select(Project)).one()
        return project.id, project.current_path


def test_projects_search_filters_and_pagination(client: TestClient) -> None:
    for query in ("P001", "p001", "项目", "企业", "P001_企业"):
        page = client.get("/api/projects", params={"q": query}).json()
        assert page["total"] == 1 and page["items"][0]["project_code"] == "P001", query
    row = client.get("/api/projects").json()["items"][0]
    assert (row["project_name"], row["company_name"], row["industry_name"], row["won"], row["products"], row["files"], row["status"]) == ("项目", "企业", "酒业", True, 5, 7, "active")
    assert row["current_path"] == "01已中标项目/01酒业/01企业/" + FOLDER
    assert client.get("/api/projects", params={"q": "不存在"}).json() == {"items": [], "total": 0, "page": 1, "page_size": 20}
    assert client.get("/api/projects", params={"q": "%"}).json()["total"] == 0
    tree = client.get("/api/catalog/tree").json()[0]["industries"]
    wine, bank = tree[0], tree[1]
    assert client.get("/api/projects", params={"won": "true", "industry": wine["id"], "company": wine["companies"][0]["id"]}).json()["total"] == 1
    assert client.get("/api/projects", params={"won": "false"}).json()["total"] == 0
    assert client.get("/api/projects", params={"industry": bank["id"]}).json()["total"] == 0
    assert client.get("/api/projects", params={"company": bank["companies"][0]["id"]}).json()["total"] == 0
    assert client.get("/api/projects", params={"page": 2}).json()["items"] == []
    assert client.get("/api/projects", params={"page": 0}).status_code == 422
    client.cookies.clear()
    assert client.get("/api/projects").status_code == 401


def test_project_page_counts_match_nas(client: TestClient, tmp_path: Path) -> None:
    project_id, _ = project_row(client)
    detail = client.get(f"/api/projects/{project_id}").json()
    root = tmp_path / "nas" / TARGET
    assert detail["project"]["products"] == len(list((root / "单品").iterdir())) + len(list((root / "套装").iterdir())) == 5
    assert {item["name"]: item["count"] for item in detail["folders"]} == {"01定稿文件": 1, "02设计方案": 0, "03拍摄照片及视频": 0, "04授权文件": 0, "05 详情页": 1, "套装": 2, "单品": 3}
    for folder in detail["folders"]:
        directory = root / folder["name"]
        assert folder["count"] == (len([path for path in directory.rglob("*") if path.is_file()]) if directory.exists() else 0)
    products = {item["product_code"]: item for item in detail["products_list"]}
    assert list(products) == ["P001-001", "P001-002", "P001-003", "P001-004", "P001-005"]
    box = products["P001-004"]
    assert box["kind"] == "set" and sorted(box["related"]) == sorted(products[code]["id"] for code in ("P001-001", "P001-002", "P001-003"))
    assert box["id"] in products["P001-001"]["related"] and all(item["link_id"] and item["status"] == "active" for item in products.values())
    assert detail["link_id"] and detail["history"] == [] and detail["folder_name"] == FOLDER and detail["code_source"] == "company"
    actions = [row["action"] for row in detail["logs"]]
    assert "入库完成" in actions and "终审通过" in actions and "登记卡片柜" in actions and detail["logs"][0]["user"] == "final"
    files = client.get(f"/api/projects/{project_id}/files", params={"category": "05 详情页"}).json()
    assert [(row["name"], row["rel_path"], row["size"], row["category"]) for row in files] == [("主视觉.jpg", "05 详情页/主视觉.jpg", len(jpeg()), "05 详情页")]
    assert files[0]["link_id"] and files[0]["mtime"] > 0
    assert len(client.get(f"/api/projects/{project_id}/files").json()) == 7
    assert client.get(f"/api/projects/{project_id}/files", params={"category": "别的"}).status_code == 422
    assert client.get("/api/projects/999").status_code == 404 and client.get("/api/projects/999/files").status_code == 404


def test_product_detail_tags_relations_and_links(client: TestClient) -> None:
    project_id, _ = project_row(client)
    products = {item["product_code"]: item for item in client.get(f"/api/projects/{project_id}").json()["products_list"]}
    bottle = client.get(f"/api/products/{products['P001-003']['id']}").json()
    assert (bottle["display_name"], bottle["kind"], bottle["image_rel_path"], bottle["status"]) == ("酒瓶", "item", "单品/酒瓶.jpg", "active")
    assert len(bottle["tags"]) == 11 and bottle["tags"][0]["key"] == "category" and bottle["tags"][0]["label"] == "品类"
    material = next(tag for tag in bottle["tags"] if tag["key"] == "material")
    assert (material["value"], material["source"], material["terms"]) == ("玻璃", "manual", ["玻璃"])
    assert [item["product_code"] for item in bottle["related"]] == ["P001-004", "P001-005"] and bottle["free_text"]
    assert bottle["project"]["id"] == project_id and bottle["product_link"] == products["P001-003"]["link_id"] and bottle["project_link"]
    box = client.get(f"/api/products/{products['P001-004']['id']}").json()
    assert [item["product_code"] for item in box["related"]] == ["P001-001", "P001-002", "P001-003"]
    assert client.get("/api/products/999").status_code == 404


def test_download_full_file_with_chinese_name_and_log(client: TestClient) -> None:
    target = file_id(client, "05 详情页/主视觉.jpg")
    response = client.get(f"/api/files/{target}/download")
    assert response.status_code == 200 and response.content == jpeg()
    assert response.headers["content-length"] == str(len(jpeg())) and response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-disposition"] == "attachment; filename=\"___.jpg\"; filename*=UTF-8''%E4%B8%BB%E8%A7%86%E8%A7%89.jpg"
    assert response.headers["etag"] == f'"{hashlib.sha256(jpeg()).hexdigest()}"' and "last-modified" in response.headers
    with client.app.state.database.session() as session:
        entry = session.scalars(select(Oplog).where(Oplog.action == "下载文件")).one()
        assert entry.object_id == str(target) and entry.after["rel_path"] == "05 详情页/主视觉.jpg" and entry.after["range"] is None
    assert client.get(f"/api/files/{target}/check").json() == {"id": target, "name": "主视觉.jpg", "size": len(jpeg())}
    assert client.get("/api/files/999/download").status_code == 404
    client.cookies.clear()
    assert client.get(f"/api/files/{target}/download").status_code == 401


def test_range_requests_resume_and_if_range(client: TestClient) -> None:
    target = file_id(client, "01定稿文件/终稿.ai")
    data, size = jpeg(), len(jpeg())
    url = f"/api/files/{target}/download"
    first = client.get(url, headers={"Range": "bytes=0-99"})
    assert first.status_code == 206 and first.content == data[:100] and first.headers["content-range"] == f"bytes 0-99/{size}" and first.headers["content-length"] == "100"
    rest = client.get(url, headers={"Range": "bytes=100-", "If-Range": first.headers["etag"]})
    assert rest.status_code == 206 and first.content + rest.content == data and rest.headers["content-range"] == f"bytes 100-{size - 1}/{size}"
    assert client.get(url, headers={"Range": "bytes=-10"}).content == data[-10:]
    assert client.get(url, headers={"Range": f"bytes=5-{size + 999}"}).content == data[5:]
    assert client.get(url, headers={"Range": "bytes=7-7"}).content == data[7:8]
    # 文件变了（If-Range 不匹配）或 Range 不认识：发整个文件。
    for headers in ({"Range": "bytes=100-", "If-Range": '"other-version"'}, {"Range": "items=0-1"}, {"Range": "bytes=0-1,5-6"}):
        whole = client.get(url, headers=headers)
        assert whole.status_code == 200 and whole.content == data and "content-range" not in whole.headers
    assert client.get(url, headers={"Range": "bytes=100-", "If-Range": first.headers["last-modified"]}).status_code == 206
    bad = client.get(url, headers={"Range": f"bytes={size}-"})
    assert bad.status_code == 416 and bad.headers["content-range"] == f"bytes */{size}" and bad.json()["code"] == "range_not_satisfiable"
    assert client.get(url, headers={"Range": "bytes=9-3"}).status_code == 416
    with client.app.state.database.session() as session:
        assert [0, 99] in [entry.after["range"] for entry in session.scalars(select(Oplog).where(Oplog.action == "下载文件"))]


def test_range_parser_and_content_disposition() -> None:
    assert parse_range(None, 10) is None and parse_range("bytes=-", 10) is None
    assert parse_range("bytes=0-", 10) == (0, 9) and parse_range("bytes=-3", 10) == (7, 9) and parse_range("bytes=-99", 10) == (0, 9)
    for header, size in (("bytes=10-", 10), ("bytes=-0", 10), ("bytes=0-", 0)):
        with pytest.raises(AppError):
            parse_range(header, size)
    assert content_disposition('a b"；.ai') == "attachment; filename=\"a_b__.ai\"; filename*=UTF-8''a%20b%22%EF%BC%9B.ai"


def test_download_follows_nas_permissions(client: TestClient) -> None:
    target = file_id(client, "05 详情页/主视觉.jpg")
    login(client, "outsider")
    for path in (f"/api/files/{target}/download", f"/api/files/{target}/check"):
        denied = client.get(path)
        assert denied.status_code == 403 and denied.json() == {"code": "nas_permission_denied", "message": "NAS 无权限", "detail": None}
    assert client.get(f"/api/files/{target}/download", headers={"Range": "bytes=0-1"}).status_code == 403
    # 卡片柜信息不受 NAS 权限限制，仍可查看；没有记成功的下载日志。
    assert client.get("/api/projects", params={"q": "P001"}).json()["total"] == 1
    with client.app.state.database.session() as session:
        assert not list(session.scalars(select(Oplog).where(Oplog.action == "下载文件")))


def test_lookup_layer_two_whole_project_folder_moved(client: TestClient, tmp_path: Path) -> None:
    target = file_id(client, "01定稿文件/终稿.ai")
    nas = tmp_path / "nas" / "设计资产"
    destination = nas / "02未中标项目" / "02金融" / "01招商银行"
    destination.mkdir(parents=True)
    shutil.move(str(tmp_path / "nas" / TARGET), str(destination / FOLDER))
    response = client.get(f"/api/files/{target}/download")
    assert response.status_code == 200 and response.content == jpeg()
    project_id, path = project_row(client)
    assert path == "02未中标项目/02金融/01招商银行/" + FOLDER
    with client.app.state.database.session() as session:
        entry = session.scalars(select(Oplog).where(Oplog.action == "更新项目路径")).one()
        assert entry.before == {"current_path": "01已中标项目/01酒业/01企业/" + FOLDER, "won": True}
        assert entry.after["current_path"] == path and entry.after["won"] is False
        assert not list(session.scalars(select(InspectionFinding)))
    # 之后走第一层，不再重复更新。
    assert client.get(f"/api/files/{file_id(client, '单品/酒瓶.jpg')}/download").status_code == 200
    with client.app.state.database.session() as session:
        assert len(list(session.scalars(select(Oplog).where(Oplog.action == "更新项目路径")))) == 1
    assert client.get(f"/api/projects/{project_id}").json()["project"]["current_path"] == path


def test_lookup_layer_two_project_folder_renamed(client: TestClient, tmp_path: Path) -> None:
    target = file_id(client, "01定稿文件/终稿.ai")
    project = tmp_path / "nas" / TARGET
    project.rename(project.with_name("P001_企业_改了名字"))
    assert client.get(f"/api/files/{target}/download").content == jpeg()
    with client.app.state.database.session() as session:
        row = session.scalars(select(Project)).one()
        assert (row.current_path, row.folder_name) == ("01已中标项目/01酒业/01企业/P001_企业_改了名字", "P001_企业_改了名字")


def test_lookup_layer_three_file_moved_inside_project(client: TestClient, tmp_path: Path) -> None:
    target = file_id(client, "01定稿文件/终稿.ai")
    root = tmp_path / "nas" / TARGET
    (root / "02设计方案" / "旧稿").mkdir(parents=True)
    (root / "01定稿文件" / "终稿.ai").rename(root / "02设计方案" / "旧稿" / "终稿.ai")
    assert client.get(f"/api/files/{target}/download").content == jpeg()
    with client.app.state.database.session() as session:
        row = session.get(CatalogFile, target)
        assert (row.rel_path, row.category) == ("02设计方案/旧稿/终稿.ai", "02设计方案")
        entry = session.scalars(select(Oplog).where(Oplog.action == "更新文件路径")).one()
        assert entry.before == {"rel_path": "01定稿文件/终稿.ai"} and entry.after == {"rel_path": "02设计方案/旧稿/终稿.ai", "by": "文件名、大小与修改时间"}


def test_lookup_layer_four_renamed_file_confirmed_by_fingerprint(client: TestClient, tmp_path: Path) -> None:
    target = file_id(client, "01定稿文件/终稿.ai")
    root = tmp_path / "nas" / TARGET
    content = b"unique design source " * 1000
    source = root / "01定稿文件" / "终稿.ai"
    source.write_bytes(content)
    with client.app.state.database.session() as session:
        row = session.get(CatalogFile, target)
        row.size, row.fingerprint = len(content), hashlib.sha256(content).hexdigest()
    # 改名并改了修改时间；旁边放一个大小相同、内容不同的干扰文件。
    renamed = root / "03拍摄照片及视频"
    renamed.mkdir()
    source.rename(renamed / "终稿_v2.ai")
    os.utime(renamed / "终稿_v2.ai", (1_600_000_000, 1_600_000_000))
    (renamed / "干扰.ai").write_bytes(b"x" * len(content))
    assert client.get(f"/api/files/{target}/download").content == content
    with client.app.state.database.session() as session:
        assert session.get(CatalogFile, target).rel_path == "03拍摄照片及视频/终稿_v2.ai"
        assert session.scalars(select(Oplog).where(Oplog.action == "更新文件路径")).one().after["by"] == "指纹"


def test_lookup_fails_everywhere_reports_missing_and_records_finding_once(client: TestClient, tmp_path: Path) -> None:
    target = file_id(client, "01定稿文件/终稿.ai")
    (tmp_path / "nas" / TARGET / "01定稿文件" / "终稿.ai").unlink()
    for _ in range(2):
        missing = client.get(f"/api/files/{target}/download")
        assert missing.status_code == 404 and missing.json()["code"] == "file_missing" and missing.json()["message"] == "文件已不存在"
    assert client.get(f"/api/files/{target}/check").status_code == 404
    with client.app.state.database.session() as session:
        finding = session.scalars(select(InspectionFinding)).one()
        assert (finding.kind, finding.file_id, finding.run_id, finding.auto_fixed) == ("missing", target, None, False)
        assert finding.path == "01已中标项目/01酒业/01企业/" + FOLDER + "/01定稿文件/终稿.ai"
        assert session.get(CatalogFile, target).rel_path == "01定稿文件/终稿.ai"
    # 其他文件不受影响；整个项目文件夹都没了也只是报不存在。
    assert client.get(f"/api/files/{file_id(client, '单品/酒瓶.jpg')}/download").status_code == 200
    shutil.rmtree(tmp_path / "nas" / TARGET)
    assert client.get(f"/api/files/{file_id(client, '单品/酒瓶.jpg')}/download").status_code == 404


def test_permanent_links_redirect_and_inactive_targets(client: TestClient) -> None:
    with client.app.state.database.session() as session:
        links = {link.kind: link for link in session.scalars(select(Link).order_by(Link.id.desc()))}
        ids = {kind: (link.link_id, link.target_id) for kind, link in links.items()}
    expected = {"product": "/products/{}", "file": "/api/files/{}/download", "project": "/projects/{}"}
    for kind, (link_id, target_id) in ids.items():
        redirect = client.get(f"/api/l/{link_id}", follow_redirects=False)
        assert redirect.status_code == 307 and redirect.headers["location"] == expected[kind].format(target_id)
        assert client.get(f"/api/l/{link_id}", params={"format": "json"}).json() == {"link_id": link_id, "kind": kind, "target_id": target_id,
            "status": "active", "url": expected[kind].format(target_id), "message": None}
    followed = client.get(f"/api/l/{ids['file'][0]}")
    assert followed.status_code == 200 and followed.headers["content-disposition"].startswith("attachment")
    assert client.get("/api/l/nope").status_code == 404
    with client.app.state.database.session() as session:
        session.get(Product, ids["product"][1]).status = "voided"
        session.get(Project, ids["project"][1]).status = "deleted"
    voided = client.get(f"/api/l/{ids['product'][0]}", follow_redirects=False)
    assert voided.status_code == 410 and voided.json()["status"] == "voided" and voided.json()["message"] == "该商品已作废" and voided.json()["url"] is None
    assert client.get(f"/api/l/{ids['project'][0]}", params={"format": "json"}).json()["message"] == "该项目已删除"
    with client.app.state.database.session() as session:
        session.get(CatalogFile, ids["file"][1]).status = "deleted"
    assert client.get(f"/api/files/{ids['file'][1]}/download").status_code == 410
    client.cookies.clear()
    assert client.get(f"/api/l/{ids['project'][0]}").status_code == 401


def test_download_many_returns_one_url_per_file(client: TestClient) -> None:
    project_id, _ = project_row(client)
    files = client.get(f"/api/projects/{project_id}/files").json()[:5]
    ids = [row["id"] for row in files]
    items = client.post("/api/files/download-many", json={"file_ids": [*ids, ids[0]]}).json()
    assert [(item["file_id"], item["name"], item["url"]) for item in items] == [(row["id"], row["name"], f"/api/files/{row['id']}/download") for row in files]
    for item in items:
        assert client.get(item["url"]).status_code == 200
    assert client.post("/api/files/download-many", json={"file_ids": []}).status_code == 422
    assert client.post("/api/files/download-many", json={"file_ids": [ids[0], 99999]}).status_code == 404


def test_product_thumbnail_cache_regeneration_and_placeholder(client: TestClient, tmp_path: Path) -> None:
    with client.app.state.database.session() as session:
        product = session.scalar(select(Product).where(Product.product_code == "P001-003"))
        product_id = product.id
    cached = client.get(f"/api/thumbs/{product_id}/300")
    assert cached.status_code == 200 and cached.headers["content-type"] == "image/jpeg" and cached.headers["x-thumbnail-placeholder"] == "0"
    # 本地缓存丢失：用当前用户会话从 NAS 重新生成三档。
    shutil.rmtree(tmp_path / "data" / "thumbs" / "product" / str(product_id))
    again = client.get(f"/api/thumbs/{product_id}/1024")
    assert again.headers["x-thumbnail-placeholder"] == "0" and again.content[:2] == b"\xff\xd8"
    assert sorted(path.name for path in (tmp_path / "data" / "thumbs" / "product" / str(product_id)).iterdir()) == ["1024.jpg", "1600.jpg", "300.jpg"]
    # 缓存没有、当前用户又读不了 NAS：返回占位图而不是报错。
    shutil.rmtree(tmp_path / "data" / "thumbs" / "product" / str(product_id))
    login(client, "outsider")
    fallback = client.get(f"/api/thumbs/{product_id}/300")
    assert fallback.status_code == 200 and fallback.headers["x-thumbnail-placeholder"] == "1" and fallback.content[:2] == b"\xff\xd8"
    assert not (tmp_path / "data" / "thumbs" / "product" / str(product_id)).exists()
    assert client.get(f"/api/thumbs/{product_id}/123").status_code == 422 and client.get("/api/thumbs/999/300").status_code == 404
