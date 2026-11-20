"""真实目录行为使用隔离 LocalFs 与 FakeAuthBackend。"""
from pathlib import Path
from time import perf_counter

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select

from app.config import Settings
from app.db.models import CatalogFile, Oplog, Product, Project
from app.main import create_app
from app.services.nas.errors import NasUnavailable
from app.services.nas.fs import NasStream
from app.services.nas.paths import NasPaths

BASE = "/api/nas-browser"
ROOT = "设计验证"
FORMAL = "设计资产"


@pytest.fixture
def browser(tmp_path: Path):
    paths = NasPaths.under(ROOT)
    root = tmp_path / "share" / ROOT
    root.mkdir(parents=True)
    for region in (FORMAL, "工作台临时区", "_工作台备份"):
        (root / region).mkdir()
    config = Settings.model_validate({
        "bootstrap_admin": "admin",
        "paths": {"data_dir": tmp_path / "data", "dev_nas_dir": root.parent},
        "nas": {"work_root": ROOT, "root_path": paths.formal, "temp_path": paths.temp, "backup_path": paths.backup},
        "dev_users": [
            {"username": "reader", "password": "test-password", "permissions": {"formal": "read"}, "nas_browser_read": True},
            {"username": "admin", "password": "test-password", "permissions": {}, "nas_browser_read": True},
            {"username": "denied", "password": "test-password", "permissions": {}},
        ],
    })
    with TestClient(create_app(config, log_dir=tmp_path / "logs")) as client:
        yield client, root


def login(client: TestClient, username: str = "reader") -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": "test-password"})
    assert response.status_code == 200


def test_empty_catalog_browses_one_level_and_downloads_without_import(browser, monkeypatch):
    client, root = browser
    folder = root / "05 详情页" / "二级目录"
    folder.mkdir(parents=True)
    (folder / "稿件.psd").write_bytes(b"design")
    (root / "说明 PDF.pdf").write_bytes(b"pdf-content")
    Image.new("RGB", (2000, 1000)).save(root / "商品图.jpg", exif=b"")
    (root / "工作台临时区" / "秘密.txt").write_text("临时文件")
    (root / "_工作台备份" / "归档.zip").write_bytes(b"backup")
    login(client)

    with client.app.state.database.session() as session:
        assert session.scalar(select(func.count(Project.id))) == 0
    assert client.get("/api/projects").json()["items"] == []
    monkeypatch.setattr(client.app.state.auth.pool, "open", lambda *args: pytest.fail("浏览重新建立了会话"))
    monkeypatch.setattr(client.app.state.auth.pool, "system", lambda: pytest.fail("浏览使用了系统身份"))
    service = client.app.state.ai
    monkeypatch.setattr(service, "client", lambda *args: pytest.fail("NAS 浏览调用 AI"), raising=False)

    response = client.get(BASE + "/list")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["root_name"] == ROOT
    assert {row["name"] for row in result["folders"]} == {FORMAL, "05 详情页"}
    assert {row["name"] for row in result["files"]} == {"说明 PDF.pdf", "商品图.jpg"}
    assert "秘密" not in response.text and "稿件" not in response.text
    selected = client.get(BASE + "/list", params={"path": "05 详情页"}).json()
    assert selected["folders"][0]["path"] == "05 详情页/二级目录"
    file_path = "05 详情页/二级目录/稿件.psd"
    meta = client.get(BASE + "/info", params={"path": file_path}).json()
    assert (meta["kind"], meta["extension"], meta["size"], meta["catalog_status"]) == ("file", "psd", 6, "unmanaged")
    assert client.get(BASE + "/preview", params={"path": file_path}).status_code == 415
    assert client.get(BASE + "/download", params={"path": file_path}).content == b"design"
    preview = client.get(BASE + "/preview", params={"path": "商品图.jpg", "size": 300})
    assert preview.status_code == 200, preview.text
    from io import BytesIO
    with Image.open(BytesIO(preview.content)) as image:
        assert image.size == (300, 150) and not image.getexif()
    assert client.post(BASE + "/download", params={"path": file_path}).status_code == 405
    assert (folder / "稿件.psd").read_bytes() == b"design"

    with client.app.state.database.session() as session:
        assert all(session.scalar(select(func.count(model.id))) == 0 for model in (Project, Product, CatalogFile))
        audit = session.scalars(select(Oplog).where(Oplog.action == "NAS 文件下载")).all()
        assert [row.result for row in audit] == ["开始", "完成"]


def test_empty_folder_and_current_directory_filter(browser):
    client, root = browser
    login(client)
    empty = client.get(BASE + "/list", params={"path": FORMAL}).json()
    assert empty["total"] == 0 and empty["folders"] == empty["files"] == []
    for name, size in (("商品小.png", 4), ("商品大.JPG", 9), ("材料.pdf", 20)):
        (root / name).write_bytes(b"x" * size)
    result = client.get(BASE + "/list", params={"q": "商品", "kind": "image", "sort": "size"}).json()
    assert [item["name"] for item in result["files"]] == ["商品大.JPG", "商品小.png"]

def test_details_do_not_infer_asset_identity_from_a_name_or_recorded_path(browser):
    client, root = browser
    (root / FORMAL / "已入库目录").mkdir()
    (root / FORMAL / "已入库目录" / "同名.png").touch()
    (root / "同名.png").touch()
    login(client)
    with client.app.state.database.session() as session:
        project = Project(project_code="P1", company_name="企业", won=True, project_name="项目",
                          folder_name="已入库目录", current_path="已入库目录", status="active")
        session.add(project)
        session.flush()
        session.add(CatalogFile(project_id=project.id, rel_path="同名.png", size=0, mtime=0,
                                fingerprint="0" * 64, category="design", status="active"))
    assert client.get(BASE + "/info", params={"path": "同名.png"}).json()["catalog_status"] == "unmanaged"
    assert client.get(BASE + "/info", params={"path": FORMAL + "/已入库目录/同名.png"}).json()["catalog_status"] == "unverified"


def test_denied_admin_and_unauthenticated_logout(browser):
    client, root = browser
    (root / FORMAL / "禁止.jpg").write_bytes(b"private")
    assert client.get(BASE + "/list").status_code == 401
    login(client, "denied")
    assert client.get(BASE + "/list").status_code == 403
    client.post("/api/auth/logout")
    login(client, "admin")
    assert client.get("/api/auth/me").json()["roles"] == ["uploader", "admin"]
    assert client.get(BASE + "/list").json()["folders"] == []
    for command in ("list", "info", "preview", "download"):
        assert client.get(BASE + "/" + command, params={"path": f"{FORMAL}/禁止.jpg"}).status_code == 403
    client.post("/api/auth/logout")
    assert client.get(BASE + "/download", params={"path": "任意.jpg"}).status_code == 401


@pytest.mark.parametrize("path", ["../其他", "/绝对路径", "C:/文件", r"\\server\share", "%2e%2e/其他",
                                    "工作台临时区/秘密.txt", "_工作台备份/归档.zip",
                                    "_工作台备份./归档.zip", "工作台临时区/../内容"])
def test_path_boundary(browser, path):
    client, _ = browser
    login(client)
    for command in ("list", "info", "preview", "download"):
        response = client.get(BASE + "/" + command, params={"path": path})
        assert response.status_code == 403 and "秘密.txt" not in response.text, response.text


def test_symlink_and_unauthorized_other_share(browser, tmp_path):
    client, root = browser
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(b"outside")
    (root / "外部链接").symlink_to(outside, target_is_directory=True)
    (root / "图片链接.png").symlink_to(outside / "secret.png")
    login(client)
    assert "链接" not in client.get(BASE + "/list").text
    assert client.get(BASE + "/info", params={"path": "外部链接/secret.png"}).status_code == 403
    assert client.get(BASE + "/download", params={"path": "图片链接.png"}).status_code == 403
    assert client.get(BASE + "/list", params={"share": "他人共享"}).status_code == 422


def test_not_found_and_unavailable(browser, monkeypatch):
    client, _ = browser
    login(client)
    for command in ("list", "info", "preview", "download"):
        assert client.get(BASE + "/" + command, params={"path": "不存在.jpg"}).status_code == 404
    from app.services.nas.browser import ReadRoot
    def disconnected(self, path, folders_only, cancelled):
        raise NasUnavailable()
    monkeypatch.setattr(ReadRoot, "_list", disconnected)
    response = client.get(BASE + "/list")
    assert response.status_code == 503 and "刷新重试" in response.text


def test_large_download_range_stream_sizes_and_release(browser, monkeypatch):
    client, root = browser
    target = root / "超大文件.psd"
    length = 2 * 1024 ** 3
    with target.open("wb") as stream:
        stream.truncate(length)
        stream.seek(length - 4)
        stream.write(b"tail")
    login(client)
    reads = []
    original = NasStream.read

    def tracked(self, size=-1):
        reads.append(size)
        return original(self, size)

    monkeypatch.setattr(NasStream, "read", tracked)
    endpoint = BASE + "/download"
    params = {"path": target.name}
    head = client.head(endpoint, params=params)
    assert head.status_code == 200 and head.headers["Content-Length"] == str(length)
    assert not reads
    response = client.get(endpoint, params=params, headers={"Range": "bytes=-2097152"})
    assert response.status_code == 206 and response.content.endswith(b"tail")
    assert response.headers["Content-Range"] == f"bytes {length - 2097152}-{length - 1}/{length}"
    assert "filename*=UTF-8''" in response.headers["Content-Disposition"]
    assert len(reads) == 2 and max(reads) == 1024 ** 2
    rejected = client.get(endpoint, params=params, headers={"Range": f"bytes={length}-"})
    assert rejected.status_code == 416
    assert rejected.headers["Content-Range"] == f"bytes */{length}"
    context = client.app.state.auth.context(client.cookies["nas_workbench_session"], "本机")
    assert not client.app.state.auth.pool.get(context.session_id)._streams


def test_large_directory_is_paged_without_opening_children(browser):
    client, root = browser
    for index in range(5000):
        (root / f"资料-{index:04d}.pdf").touch()
    login(client)
    start = perf_counter()
    first = client.get(BASE + "/list").json()
    second = client.get(BASE + "/list", params={"page": 2}).json()
    elapsed = perf_counter() - start
    assert first["total"] == second["total"] == 5001
    assert len(first["folders"] + first["files"]) == len(second["files"]) == 60
    assert first["files"][-1]["name"] < second["files"][0]["name"]
    assert elapsed < 10
    print(f"LocalFs 一级目录 5001 项，两次分页耗时 {elapsed:.3f} 秒")
