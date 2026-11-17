"""WP12 入库后操作：每种操作走完两级审核再检查结果，并在多个中断点验证可以继续。"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from app.config import DevUser, PathsConfig, Settings
from app.db.models import (Batch, BatchFile, CatalogFile, HistoryVersion, Link, Oplog, Product, ProductEmbedding, ProductRelation, ProductTag,
                           Project, User)
from app.main import create_app
from app.services import archive
from app.services.upload.supplement import versioned_name
from tests.integration.test_audit import ALL, act, login
from tests.integration.test_ingest import FOLDER, PATHS, ROLES, approve, to_final
from tests.integration.test_review import jpeg

OPS_PATHS = [*PATHS, "02设计方案/提案.pdf"]
HOME = "01已中标项目/01酒业/01企业"
TODAY = archive.local_date(datetime.now(timezone.utc)).replace("-", "")
# 不中断 / 第 1 个文件操作后 / 文件操作全部完成后 / 卡片柜更新后
FAULTS = [None, ("ops", 1), ("step", 2), ("step", 3)]
NO_FILE_FAULTS = [None, ("step", 2), ("step", 3)]


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(mode="dev", paths=PathsConfig(data_dir=tmp_path / "data", dev_nas_dir=tmp_path / "nas"), bootstrap_admin="admin")
    settings.dev_users = [DevUser(username=name, password="fake", permissions={"temp": "write", "formal": "write" if name == "final" else "read"}) for name in ROLES]
    with TestClient(create_app(settings, log_dir=tmp_path / "logs")) as client:
        for name in ROLES:
            login(client, name)
        with client.app.state.database.session() as session:
            for user in session.scalars(select(User)):
                user.roles = ROLES[user.username]
        login(client, "admin")
        assert client.put("/api/admin/company-map", json={"industries": [{"name": "酒业"}, {"name": "金融"}],
            "companies": [{"name": "企业", "industry_name": "酒业"}, {"name": "招商银行", "industry_name": "金融"}]}).status_code == 200
        approve(client, to_final(client, OPS_PATHS))
        login(client, "one")
        yield client


# ---- 帮手 ----
def project_of(client: TestClient) -> tuple[int, str, str]:
    with client.app.state.database.session() as session:
        project = session.scalars(select(Project)).one()
        return project.id, project.current_path, project.status


def ids(client: TestClient) -> dict[str, int]:
    with client.app.state.database.session() as session:
        return {row.product_code: row.id for row in session.scalars(select(Product))}


def file_ids(client: TestClient) -> dict[str, int]:
    with client.app.state.database.session() as session:
        return {row.rel_path: row.id for row in session.scalars(select(CatalogFile))}


def batch_state(client: TestClient, batch_id: int) -> tuple[str, int, str | None]:
    with client.app.state.database.session() as session:
        batch = session.get(Batch, batch_id)
        return batch.status, batch.ingest_step, batch.ingest_error


def arm(client: TestClient, fault: tuple[str, int] | None) -> None:
    if fault and fault[0] == "ops":
        client.app.state.postops.fault_after_ops = fault[1]
    elif fault:
        client.app.state.ingest.fault_after = fault[1]


def settle(client: TestClient, batch_id: int, fault: tuple[str, int] | None) -> None:
    """等任务结束；注入了故障时先确认批次停在「入库异常」，再由终审点继续。"""
    assert client.app.state.jobs.wait_idle(15)
    if fault:
        status, step, error = batch_state(client, batch_id)
        assert status == "入库异常" and "中断" in error, (status, step, error)
        assert step == (1 if fault[0] == "ops" else fault[1])
        login(client, "final")
        assert client.post(f"/api/review/{batch_id}/resume-ingest").status_code == 200
        assert client.app.state.jobs.wait_idle(15)
    assert batch_state(client, batch_id) == ("已入库", 5, None), batch_state(client, batch_id)
    login(client, "one")


def review_and_run(client: TestClient, batch_id: int, fault: tuple[str, int] | None = None) -> None:
    assert act(client, "first", batch_id, "claim").status_code == 200
    assert act(client, "first", batch_id, "pass", {"checklist": ALL}).status_code == 200
    assert act(client, "final", batch_id, "claim").status_code == 200
    arm(client, fault)
    done = act(client, "final", batch_id, "pass", {"checklist": ALL})
    assert done.status_code == 200, done.text
    settle(client, batch_id, fault)


def start(client: TestClient, user: str, path: str, body: dict | None = None) -> int:
    login(client, user)
    project_id = project_of(client)[0]
    response = client.post(f"/api/projects/{project_id}/ops/{path}", json=body or {})
    assert response.status_code == 200, response.text
    login(client, "one")
    return response.json()["id"]


def verify_clean(client: TestClient) -> None:
    with client.app.state.database.session() as session:
        project = session.scalars(select(Project)).one()
        fs = client.app.state.auth.pool.get(session.scalar(select(User.id).where(User.username == "final")))
        assert archive.verify(session, fs, client.app.state.postops.paths, project) == []


def actions(client: TestClient) -> list[str]:
    with client.app.state.database.session() as session:
        return [row.action for row in session.scalars(select(Oplog).order_by(Oplog.id))]


def root(tmp_path: Path, path: str | None = None) -> Path:
    return tmp_path / "nas" / "设计资产" / (path or f"{HOME}/{FOLDER}")


def archive_of(tmp_path: Path, rel: str, path: str | None = None) -> dict:
    return json.loads((root(tmp_path, path) / rel).read_text("utf-8"))


def upload_supplement(client: TestClient, files: dict[str, bytes], folder: str = FOLDER, acks: list[str] | None = None) -> dict:
    response = client.post("/api/batches", json={"folder_name": folder, "won": True, "acks": acks or [],
        "files": [{"rel_path": path, "size": len(data), "mtime": 5} for path, data in files.items()]})
    assert response.status_code == 200, response.text
    batch = response.json()
    for file in batch["files"]:
        base = f"/api/batches/{batch['id']}/files/{file['id']}"
        headers = {"X-Upload-Token": batch["upload_token"]}
        assert client.put(base + "/chunks/0", content=files[file["rel_path"]], headers=headers).status_code == 200
        staged = client.post(base + "/complete", headers=headers)
        assert staged.status_code == 200
    return staged.json()


# ---- 补充与替换 ----
@pytest.mark.parametrize("fault", FAULTS)
def test_supplement_merges_skips_replaces_and_keeps_both(client: TestClient, tmp_path: Path, fault: tuple[str, int] | None) -> None:
    old, red, green = jpeg(), jpeg("red"), jpeg("green")
    before = ids(client)
    with client.app.state.database.session() as session:
        bottle_link = session.scalar(select(Link.link_id).where(Link.kind == "product", Link.target_id == before["P001-003"]))
        old_bottle_file = session.get(Product, before["P001-003"]).file_id
    staged = upload_supplement(client, {"02设计方案/新方案.pdf": b"new plan", "01定稿文件/终稿.ai": old, "05 详情页/主视觉.jpg": red,
                                        "02设计方案/提案.pdf": b"second proposal", "单品/新单品.jpg": green, "单品/酒瓶.jpg": red})
    batch_id = staged["id"]
    assert (staged["kind"], staged["status"], staged["won"]) == ("supplement", "已暂存", True)
    assert staged["conflict_counts"] == {"new": 2, "duplicate": 1, "conflict": 3, "unresolved": 3}
    by_path = {file["rel_path"]: file for file in staged["files"]}
    assert (by_path["01定稿文件/终稿.ai"]["conflict"], by_path["单品/新单品.jpg"]["conflict"], by_path["单品/酒瓶.jpg"]["conflict"]) == ("duplicate", "new", "conflict")
    # 冲突文件没选完不能进入下一步。
    blocked = client.post(f"/api/batches/{batch_id}/confirm")
    assert blocked.status_code == 409 and blocked.json()["code"] == "conflicts_unresolved" and len(blocked.json()["detail"]["files"]) == 3
    assert client.put(f"/api/batches/{batch_id}/conflicts", json={"choices": {str(by_path["01定稿文件/终稿.ai"]["id"]): "replace"}}).status_code == 404
    chosen = client.put(f"/api/batches/{batch_id}/conflicts", json={"choices": {
        str(by_path["05 详情页/主视觉.jpg"]["id"]): "replace", str(by_path["单品/酒瓶.jpg"]["id"]): "replace", str(by_path["02设计方案/提案.pdf"]["id"]): "keep_both"}})
    assert chosen.status_code == 200 and chosen.json()["conflict_counts"]["unresolved"] == 0
    assert "02设计方案/提案_v2.pdf" in [file["rel_path"] for file in chosen.json()["files"]]
    # 改主意：另存 → 替换 → 再另存，文件名来回切换不丢。
    proposal = str(by_path["02设计方案/提案.pdf"]["id"])
    assert "02设计方案/提案.pdf" in [f["rel_path"] for f in client.put(f"/api/batches/{batch_id}/conflicts", json={"choices": {proposal: "replace"}}).json()["files"]]
    assert "02设计方案/提案_v2.pdf" in [f["rel_path"] for f in client.put(f"/api/batches/{batch_id}/conflicts", json={"choices": {proposal: "keep_both"}}).json()["files"]]
    assert client.post(f"/api/batches/{batch_id}/confirm").status_code == 200
    assert client.app.state.jobs.wait_idle(15)
    page = client.get(f"/api/batches/{batch_id}/products").json()
    assert page["batch"]["status"] == "待核对" and page["batch"]["kind"] == "supplement"
    # AI 只处理新增的主图和被替换的主图。
    products = {item["display_name"]: item for item in page["products"]}
    assert set(products) == {"新单品", "酒瓶"} and all(item["ai_status"] == "done" for item in products.values())
    assert (products["酒瓶"]["existing_code"], products["新单品"]["existing_code"]) == ("P001-003", None)
    summary = page["supplement"]
    assert (summary["new"], summary["replaced"], summary["kept_both"], summary["skipped"], summary["unresolved"]) == (2, 2, 1, 1, 0)
    assert summary["skipped_files"] == ["01定稿文件/终稿.ai"] and "原有套装" in summary["notices"][0]
    compare = {item["rel_path"]: item for item in summary["compare"]}
    assert compare["单品/酒瓶.jpg"]["is_image"] and compare["单品/酒瓶.jpg"]["old_product_id"] == before["P001-003"] and compare["单品/酒瓶.jpg"]["choice"] == "replace"
    assert compare["02设计方案/提案.pdf"]["choice"] == "keep_both" and not compare["02设计方案/提案.pdf"]["is_image"]
    assert compare["05 详情页/主视觉.jpg"]["old"]["fingerprint"] == hashlib.sha256(old).hexdigest() and compare["05 详情页/主视觉.jpg"]["new"]["fingerprint"] == hashlib.sha256(red).hexdigest()
    assert "01定稿文件" not in {folder["name"]: folder["files"] for folder in page["folders"] if folder["files"]}
    assert client.post(f"/api/batches/{batch_id}/submit", json={}).status_code == 200
    login(client, "first")
    audit = client.get(f"/api/review/{batch_id}").json()
    assert audit["kind_label"] == "补充" and [(row["label"], row["after"]) for row in audit["preview"]["rows"]] == [
        ("新增", "2 个"), ("替换", "2 个"), ("另存为新文件", "1 个"), ("跳过（与库中相同）", "1 个")]
    assert audit["review"]["supplement"]["replaced"] == 2 and audit["path"]["effective"] == HOME and not audit["can_edit_path"]
    assert client.get("/api/review/queue", params={"stage": "first"}).json()[0]["kind_label"] == "补充"
    review_and_run(client, batch_id, fault)

    base = root(tmp_path)
    history = sorted(path.name for path in (base / "_历史版本").iterdir())
    # 替换的旧文件进入 _历史版本，文件名带日期；另存的带 _v2；重复的被跳过；新文件合并。
    assert history == [f"主视觉_{TODAY}.jpg", f"酒瓶_{TODAY}.jpg"]
    assert (base / "_历史版本" / f"主视觉_{TODAY}.jpg").read_bytes() == old and (base / "05 详情页" / "主视觉.jpg").read_bytes() == red
    assert (base / "_历史版本" / f"酒瓶_{TODAY}.jpg").read_bytes() == old and (base / "单品" / "酒瓶.jpg").read_bytes() == red
    assert (base / "02设计方案" / "提案.pdf").read_bytes() == old and (base / "02设计方案" / "提案_v2.pdf").read_bytes() == b"second proposal"
    assert (base / "02设计方案" / "新方案.pdf").read_bytes() == b"new plan" and (base / "01定稿文件" / "终稿.ai").read_bytes() == old
    assert (base / "单品" / "新单品.jpg").read_bytes() == green
    with client.app.state.database.session() as session:
        batch = session.get(Batch, batch_id)
        assert not (tmp_path / "nas" / "工作台临时区" / batch.batch_no / FOLDER).exists()
        after = {row.product_code: row for row in session.scalars(select(Product))}
        # 新单品获得下一个流水编号；被替换主图的商品编号、商品记录和链接都不变，指向新图。
        assert set(after) - set(before) == {"P001-006"} and after["P001-006"].display_name == "新单品" and after["P001-006"].status == "active"
        bottle = after["P001-003"]
        assert bottle.id == before["P001-003"] and bottle.file_id != old_bottle_file
        new_file = session.get(CatalogFile, bottle.file_id)
        assert (new_file.rel_path, new_file.fingerprint, new_file.status, new_file.first_batch_id) == ("单品/酒瓶.jpg", hashlib.sha256(red).hexdigest(), "active", batch_id)
        assert session.scalar(select(Link.link_id).where(Link.kind == "product", Link.target_id == bottle.id)) == bottle_link
        replaced = session.get(CatalogFile, old_bottle_file)
        assert (replaced.status, replaced.rel_path) == ("replaced", f"_历史版本/酒瓶_{TODAY}.jpg")
        rows = {row.old_rel_path: row for row in session.scalars(select(HistoryVersion))}
        assert set(rows) == {"单品/酒瓶.jpg", "05 详情页/主视觉.jpg"} and all(row.reason == "replaced" and row.batch_id == batch_id for row in rows.values())
        # 被替换商品的旧标签保存为历史。
        snapshot = rows["单品/酒瓶.jpg"].snapshot
        assert {tag["field_key"]: tag["value"] for tag in snapshot["tags"]}["material"] == "玻璃" and rows["单品/酒瓶.jpg"].product_id == bottle.id
        assert rows["05 详情页/主视觉.jpg"].snapshot is None
        assert {tag.field_key: tag.source for tag in session.scalars(select(ProductTag).where(ProductTag.product_id == bottle.id))}["material"] == "ai"
        assert session.scalar(select(func.count()).select_from(ProductRelation)) == 5
        assert session.scalar(select(func.count()).select_from(Link).where(Link.kind == "product")) == 6
        assert session.scalar(select(func.count()).select_from(ProductEmbedding)) == 6
        active = {row.rel_path for row in session.scalars(select(CatalogFile).where(CatalogFile.status == "active"))}
        assert {"02设计方案/新方案.pdf", "02设计方案/提案_v2.pdf", "02设计方案/提案.pdf", "单品/新单品.jpg"} <= active and len(active) == 11
        assert (tmp_path / "data" / "thumbs" / "product" / str(after["P001-006"].id) / "300.jpg").is_file()
    # 商品链接指向新图；旧版本的文件链接仍能下载到旧内容。
    assert client.get(f"/api/files/{new_file.id}/download").content == red
    assert client.get(f"/api/files/{old_bottle_file}/download").content == old
    assert client.get(f"/api/l/{bottle_link}", follow_redirects=False).headers["location"] == f"/products/{before['P001-003']}"
    assert archive_of(tmp_path, "单品档案/酒瓶.json")["主图指纹"] == hashlib.sha256(red).hexdigest()
    assert archive_of(tmp_path, "单品档案/新单品.json")["商品编号"] == "P001-006"
    summary_text = (base / "_项目档案.txt").read_text("utf-8")
    assert f"单品/酒瓶.jpg  →  _历史版本/酒瓶_{TODAY}.jpg" in summary_text and "原因 replaced" in summary_text
    verify_clean(client)
    log = actions(client)
    assert {"补充包逐文件判定", "选择冲突文件处理方式", "文件被替换", "替换主图", "补充新商品", "补充落盘", "补充完成"} <= set(log)
    # 同一天再替换一次：历史版本文件名再加序号。
    again = upload_supplement(client, {"单品/酒瓶.jpg": green})
    assert client.put(f"/api/batches/{again['id']}/conflicts", json={"choices": {str(again["files"][0]["id"]): "replace"}}).status_code == 200
    assert client.post(f"/api/batches/{again['id']}/confirm").status_code == 200 and client.app.state.jobs.wait_idle(15)
    assert client.post(f"/api/batches/{again['id']}/submit", json={}).status_code == 200
    review_and_run(client, again["id"])
    assert sorted(path.name for path in (base / "_历史版本").iterdir()) == [f"主视觉_{TODAY}.jpg", f"酒瓶_{TODAY}.jpg", f"酒瓶_{TODAY}_2.jpg"]
    assert ids(client)["P001-003"] == before["P001-003"] and (base / "单品" / "酒瓶.jpg").read_bytes() == green
    verify_clean(client)


def test_supplement_with_only_material_files_and_name_mismatch_notice(client: TestClient, tmp_path: Path) -> None:
    payload = {"folder_name": "P001_企业_改过的项目名", "files": [{"rel_path": "03拍摄照片及视频/花絮.mp4", "size": 3, "mtime": 1}]}
    check = client.post("/api/batches/precheck", json=payload).json()
    assert check["code_status"] == "exists" and [(i["rule_code"], i["level"]) for i in check["issues"]] == [("P02", "warn")]
    staged = upload_supplement(client, {"03拍摄照片及视频/花絮.mp4": b"mp4"}, folder="P001_企业_改过的项目名", acks=["P02"])
    assert staged["conflict_counts"] == {"new": 1, "duplicate": 0, "conflict": 0, "unresolved": 0}
    # 同一项目同时只允许一个未完成的操作批次。
    busy = client.post(f"/api/projects/{project_of(client)[0]}/ops/retag")
    assert busy.status_code == 409 and busy.json()["code"] == "project_busy" and "补充" in busy.json()["message"]
    assert client.post(f"/api/batches/{staged['id']}/confirm").status_code == 200 and client.app.state.jobs.wait_idle(15)
    page = client.get(f"/api/batches/{staged['id']}/products").json()
    # 没有主图的补充包直接进入核对，不算 AI 失败。
    assert page["batch"]["status"] == "待核对" and page["products"] == [] and page["pending"]["can_submit"]
    assert client.post(f"/api/batches/{staged['id']}/submit", json={}).status_code == 200
    review_and_run(client, staged["id"])
    assert (root(tmp_path) / "03拍摄照片及视频" / "花絮.mp4").read_bytes() == b"mp4"
    assert project_of(client)[1] == f"{HOME}/{FOLDER}"
    verify_clean(client)


def test_supplement_can_be_withdrawn_and_versioned_names_increment(client: TestClient, tmp_path: Path) -> None:
    staged = upload_supplement(client, {"单品/酒瓶.jpg": jpeg("red")})
    assert client.put(f"/api/batches/{staged['id']}/conflicts", json={"choices": {str(staged["files"][0]["id"]): "replace"}}).status_code == 200
    assert client.post(f"/api/batches/{staged['id']}/confirm").status_code == 200 and client.app.state.jobs.wait_idle(15)
    login(client, "first")
    assert client.post(f"/api/ops/{staged['id']}/withdraw").status_code == 403
    login(client, "one")
    done = client.post(f"/api/ops/{staged['id']}/withdraw")
    assert done.status_code == 200 and done.json()["status"] == "已撤回" and not done.json()["can_withdraw"]
    with client.app.state.database.session() as session:
        assert not (tmp_path / "nas" / "工作台临时区" / session.get(Batch, staged["id"]).batch_no).exists()
    assert client.post(f"/api/ops/{staged['id']}/withdraw").status_code == 409
    assert (root(tmp_path) / "单品" / "酒瓶.jpg").read_bytes() == jpeg()
    assert versioned_name("02设计方案/提案.pdf", set()) == "02设计方案/提案_v2.pdf"
    assert versioned_name("02设计方案/提案.pdf", {"02设计方案/提案_v2.pdf", "02设计方案/提案_v3.pdf"}) == "02设计方案/提案_v4.pdf"
    assert versioned_name("说明", {"说明_v2"}) == "说明_v3"


# ---- 改名 ----
@pytest.mark.parametrize("fault", FAULTS)
def test_rename_project_and_product_keeps_codes_and_links(client: TestClient, tmp_path: Path, fault: tuple[str, int] | None) -> None:
    products = ids(client)
    with client.app.state.database.session() as session:
        links = {(row.kind, row.target_id): row.link_id for row in session.scalars(select(Link))}
        bottle_file = session.get(Product, products["P001-003"]).file_id
    batch_id = start(client, "one", "rename", {"project_name": "中秋礼盒新版", "products": [{"id": products["P001-003"], "name": "红酒瓶"}, {"id": products["P001-001"], "name": "手提袋"}]})
    detail = client.get(f"/api/ops/{batch_id}").json()
    assert (detail["kind_label"], detail["status"], detail["can_withdraw"]) == ("改名", "待初审", True)
    assert [(row["label"], row["before"], row["after"]) for row in detail["preview"]["rows"]] == [
        ("项目名", "项目", "中秋礼盒新版"), ("项目文件夹", FOLDER, "P001_企业_中秋礼盒新版"), ("P001-003 展示名", "酒瓶", "红酒瓶")]
    assert detail["preview"]["files"] == ["单品/酒瓶.jpg → 单品/红酒瓶.jpg"]
    review_and_run(client, batch_id, fault)
    new_root = f"{HOME}/P001_企业_中秋礼盒新版"
    assert project_of(client)[1] == new_root and not root(tmp_path).exists()
    base = root(tmp_path, new_root)
    assert (base / "单品" / "红酒瓶.jpg").is_file() and not (base / "单品" / "酒瓶.jpg").exists()
    assert sorted(path.name for path in (base / "单品档案").iterdir()) == ["手提袋.json", "月饼盒.json", "红酒瓶.json"]
    doc = archive_of(tmp_path, "单品档案/红酒瓶.json", new_root)
    assert (doc["商品编号"], doc["展示名"], doc["主图相对路径"], sorted(doc["所属套装"])) == ("P001-003", "红酒瓶", "单品/红酒瓶.jpg", ["P001-004", "P001-005"])
    assert {item["商品编号"] for item in archive_of(tmp_path, "套装档案/中秋礼盒A.json", new_root)["包含单品"]} == {"P001-001", "P001-002", "P001-003"}
    text = (base / "_项目档案.txt").read_text("utf-8")
    assert "项目名称：中秋礼盒新版" in text and "P001-003  单品  单品/红酒瓶.jpg" in text and "酒瓶.jpg" not in text.replace("红酒瓶.jpg", "")
    with client.app.state.database.session() as session:
        # 编号与链接不变。
        assert {row.product_code: row.id for row in session.scalars(select(Product))} == products
        assert {(row.kind, row.target_id): row.link_id for row in session.scalars(select(Link))} == links
        bottle = session.get(Product, products["P001-003"])
        assert (bottle.display_name, bottle.archive_rel_path, bottle.file_id) == ("红酒瓶", "单品档案/红酒瓶.json", bottle_file)
        project = session.scalars(select(Project)).one()
        assert (project.project_name, project.folder_name) == ("中秋礼盒新版", "P001_企业_中秋礼盒新版")
    # 旧链接仍可用。
    assert client.get(f"/api/l/{links[('product', products['P001-003'])]}", params={"format": "json"}).json()["url"] == f"/products/{products['P001-003']}"
    assert client.get(f"/api/l/{links[('file', bottle_file)]}").content == jpeg()
    assert client.get(f"/api/l/{links[('project', project_of(client)[0])]}", follow_redirects=False).headers["location"] == f"/projects/{project_of(client)[0]}"
    assert client.get("/api/projects", params={"q": "中秋礼盒新版"}).json()["total"] == 1
    verify_clean(client)
    assert {"发起改名", "项目改名", "商品改名", "改名完成"} <= set(actions(client))
    with client.app.state.database.session() as session:
        entry = session.scalars(select(Oplog).where(Oplog.action == "商品改名")).one()
        assert entry.before["display_name"] == "酒瓶" and entry.after["rel_path"] == "单品/红酒瓶.jpg"


def test_rename_validation_and_light_single_review(client: TestClient) -> None:
    products = ids(client)
    project_id = project_of(client)[0]
    post = lambda body: client.post(f"/api/projects/{project_id}/ops/rename", json=body)  # noqa: E731
    assert post({}).status_code == 422 and post({"project_name": "项目"}).json()["code"] == "no_changes"
    for name in ("a/b", "带_下划线", "con", " "):
        assert post({"project_name": name}).status_code == 422, name
    for name in ("a:b", "x/y", "单品档案", ""):
        assert post({"products": [{"id": products["P001-003"], "name": name}]}).status_code == 422, name
    # 项目内重名：与已有文件同名、与本次另一个改名同名。
    assert post({"products": [{"id": products["P001-003"], "name": "月饼盒"}]}).json()["code"] == "name_taken"
    assert post({"products": [{"id": products["P001-003"], "name": "同名"}, {"id": products["P001-002"], "name": "同名"}]}).json()["code"] == "name_taken"
    assert post({"products": [{"id": 99999, "name": "x"}]}).status_code == 404
    # 两个商品互换名字不支持（逐个改名会在磁盘上相撞）。
    assert post({"products": [{"id": products["P001-003"], "name": "月饼盒"}, {"id": products["P001-002"], "name": "酒瓶"}]}).json()["code"] == "name_taken"
    assert client.post("/api/projects/999/ops/rename", json={"project_name": "x"}).status_code == 404
    # 配置为轻操作只需终审一级时，改名直接进入待终审。
    client.app.state.auth.config.review.light_ops_single_review = True
    created = post({"project_name": "一级审核"})
    assert created.status_code == 200 and created.json()["status"] == "待终审"
    batch_id = created.json()["id"]
    assert act(client, "final", batch_id, "claim").status_code == 200 and act(client, "final", batch_id, "pass", {"checklist": ALL}).status_code == 200
    settle(client, batch_id, None)
    assert project_of(client)[1].endswith("P001_企业_一级审核")
    verify_clean(client)


# ---- 修改标签与套装关系 ----
@pytest.mark.parametrize("fault", NO_FILE_FAULTS)
def test_retag_updates_catalog_archives_and_search(client: TestClient, tmp_path: Path, fault: tuple[str, int] | None) -> None:
    products = ids(client)
    assert client.app.state.jobs.wait_idle(15)
    assert not [item for item in client.post("/api/search", json={"query": "过年"}).json()["results"] if item["by_keywords"]]
    batch_id = start(client, "one", "retag")
    page = client.get(f"/api/batches/{batch_id}/products").json()
    assert (page["batch"]["kind"], page["batch"]["status"], page["batch"]["editable"]) == ("retag", "待核对", True)
    assert len(page["products"]) == 5 and page["pending"]["can_submit"] and all(folder["files"] == [] for folder in page["folders"])
    rows = {item["existing_code"]: item for item in page["products"]}
    assert rows["P001-003"]["tags"]["material"] == {"value": "玻璃", "terms": ["玻璃"], "source": "manual", "ai_value": "陶瓷、胡桃木", "flag": None}
    assert len(rows["P001-004"]["relation_items"]) == 3 and rows["P001-004"]["relation_confirmed"]
    assert client.get(f"/api/batches/{batch_id}/thumbs/{rows['P001-001']['batch_file_id']}/300").status_code == 200
    # 不动图片：不能追加、移除、重新打标；没有改动不能提交。
    base = f"/api/batches/{batch_id}"
    assert client.post(base + "/products", data={"kind": "item"}, files={"file": ("新.jpg", jpeg(), "image/jpeg")}).status_code == 409
    assert client.delete(f"{base}/products/{rows['P001-001']['id']}").status_code == 409
    assert client.post(f"{base}/products/{rows['P001-001']['id']}/retry-ai").status_code == 409
    assert client.post(base + "/submit", json={}).json()["code"] == "no_changes"
    assert client.patch(f"{base}/products/{rows['P001-001']['id']}", json={"tags": {"festival": "新年"}, "free_text": "新年限定的手提袋"}).status_code == 200
    keep = [rows["P001-003"]["id"], rows["P001-001"]["id"]]
    assert client.put(f"{base}/products/{rows['P001-005']['id']}/relations", json={"item_ids": keep}).status_code == 200
    assert client.post(base + "/submit", json={}).json()["batch"]["status"] == "待初审"
    login(client, "first")
    preview = client.get(f"/api/review/{batch_id}").json()["preview"]
    changes = {row["label"]: (row["before"], row["after"]) for row in preview["rows"]}
    assert changes["P001-001 手提袋 · 节日场景"] == ("通用", "新年") and changes["P001-001 手提袋 · 自由描述"][1] == "新年限定的手提袋"
    assert "P001-005 中秋礼盒B · 包含的单品" in changes and preview["kind_label"] == "修改标签"
    review_and_run(client, batch_id, fault)
    with client.app.state.database.session() as session:
        tag = session.scalar(select(ProductTag).where(ProductTag.product_id == products["P001-001"], ProductTag.field_key == "festival"))
        assert (tag.value, tag.terms, tag.source, tag.ai_value) == ("新年", ["新年"], "manual", "通用")
        assert session.get(Product, products["P001-001"]).free_text == "新年限定的手提袋"
        members = {row.item_product_id: row.source for row in session.scalars(select(ProductRelation).where(ProductRelation.set_product_id == products["P001-005"]))}
        assert set(members) == {products["P001-001"], products["P001-003"]} and members[products["P001-001"]] == "manual" and members[products["P001-003"]] == "ai"
        assert session.scalar(select(func.count()).select_from(Product)) == 5 and session.scalar(select(func.count()).select_from(CatalogFile)) == 8
    doc = archive_of(tmp_path, "单品档案/手提袋.json")
    assert doc["标签"]["节日场景"] == "新年" and doc["标签来源"]["节日场景"] == "人工" and sorted(doc["所属套装"]) == ["P001-004", "P001-005"]
    assert archive_of(tmp_path, "单品档案/月饼盒.json")["所属套装"] == ["P001-004"]
    assert {(item["商品编号"], item["来源"]) for item in archive_of(tmp_path, "套装档案/中秋礼盒B.json")["包含单品"]} == {("P001-001", "人工补充"), ("P001-003", "AI推荐")}
    verify_clean(client)
    # 修改标签后搜索结果随之变化。
    assert client.app.state.jobs.wait_idle(15)
    found = [item["product_code"] for item in client.post("/api/search", json={"query": "过年"}).json()["results"] if item["by_keywords"]]
    assert found == ["P001-001"]
    assert {"发起修改标签", "修改标签与关系", "修改标签完成"} <= set(actions(client))


def test_retag_rejected_can_be_edited_and_resubmitted_or_withdrawn(client: TestClient) -> None:
    batch_id = start(client, "one", "retag")
    row = client.get(f"/api/batches/{batch_id}/products").json()["products"][0]
    assert client.patch(f"/api/batches/{batch_id}/products/{row['id']}", json={"tags": {"style": "新中式"}}).status_code == 200
    assert client.post(f"/api/batches/{batch_id}/submit", json={}).status_code == 200
    assert act(client, "first", batch_id, "claim").status_code == 200
    assert act(client, "first", batch_id, "reject", {"comment": "风格不对"}).status_code == 200
    login(client, "one")
    page = client.get(f"/api/batches/{batch_id}/products").json()
    assert page["batch"]["status"] == "已驳回" and page["batch"]["editable"] and page["history"][0]["comment"] == "风格不对"
    assert client.patch(f"/api/batches/{batch_id}/products/{row['id']}", json={"tags": {"style": "国潮"}}).status_code == 200
    assert client.post(f"/api/batches/{batch_id}/submit", json={}).json()["batch"]["round"] == 2
    assert client.post(f"/api/ops/{batch_id}/withdraw").json()["status"] == "已撤回"
    # 撤回后可以发起新的操作。
    assert client.post(f"/api/projects/{project_of(client)[0]}/ops/retag").status_code == 200


# ---- 作废与恢复 ----
@pytest.mark.parametrize("fault", FAULTS)
def test_void_set_keeps_items_then_restore_brings_relations_back(client: TestClient, tmp_path: Path, fault: tuple[str, int] | None) -> None:
    products, files = ids(client), file_ids(client)
    with client.app.state.database.session() as session:
        box_link = session.scalar(select(Link.link_id).where(Link.kind == "product", Link.target_id == products["P001-004"]))
    project_id = project_of(client)[0]
    assert client.post(f"/api/projects/{project_id}/ops/void", json={"product_ids": [products["P001-004"]], "reason": " "}).status_code == 422
    assert client.post(f"/api/projects/{project_id}/ops/void", json={"reason": "x"}).json()["code"] == "no_changes"
    assert client.post(f"/api/projects/{project_id}/ops/void", json={"file_ids": [files["单品/酒瓶.jpg"]], "reason": "x"}).status_code == 422
    batch_id = start(client, "one", "void", {"product_ids": [products["P001-004"]], "file_ids": [files["02设计方案/提案.pdf"]], "reason": "全家福拍错了"})
    preview = client.get(f"/api/ops/{batch_id}").json()["preview"]
    assert preview["reason"] == "全家福拍错了" and preview["products"] == ["P001-004 中秋礼盒A"] and "单品本身不受影响" in preview["notes"][0]
    review_and_run(client, batch_id, fault)
    base = root(tmp_path)
    assert not (base / "套装" / "中秋礼盒A.jpg").exists() and (base / "_历史版本" / f"中秋礼盒A_{TODAY}.jpg").is_file()
    assert not (base / "02设计方案" / "提案.pdf").exists() and (base / "_历史版本" / f"提案_{TODAY}.pdf").is_file()
    with client.app.state.database.session() as session:
        box = session.get(Product, products["P001-004"])
        assert box.status == "voided" and box.lifecycle["void_reason"] == "全家福拍错了" and box.lifecycle["voided_by"] == "item"
        # 单品仍在且不再属于该套装。
        assert all(session.get(Product, products[code]).status == "active" for code in ("P001-001", "P001-002", "P001-003", "P001-005"))
        assert not list(session.scalars(select(ProductRelation).where(ProductRelation.set_product_id == box.id)))
        assert session.scalar(select(func.count()).select_from(ProductRelation)) == 2
        assert session.get(CatalogFile, files["02设计方案/提案.pdf"]).status == "voided"
    assert archive_of(tmp_path, "单品档案/手提袋.json")["所属套装"] == [] and archive_of(tmp_path, "单品档案/酒瓶.json")["所属套装"] == ["P001-005"]
    voided = archive_of(tmp_path, "套装档案/中秋礼盒A.json")
    assert (voided["状态"], voided["包含单品"], voided["主图相对路径"]) == ("已作废", [], f"_历史版本/中秋礼盒A_{TODAY}.jpg")
    verify_clean(client)
    # 从搜索中消失；链接提示已作废。
    assert client.app.state.jobs.wait_idle(15)
    assert "P001-004" not in [item["product_code"] for item in client.post("/api/search", json={"query": "礼盒 陶瓷"}).json()["results"]]
    assert client.post("/api/search", json={"query": "P001-004"}).json()["results"] == []
    gone = client.get(f"/api/l/{box_link}", follow_redirects=False)
    assert gone.status_code == 410 and gone.json()["message"] == "该商品已作废，原因：全家福拍错了"
    detail = client.get(f"/api/projects/{project_id}").json()
    assert {row["new_rel_path"]: (row["reason"], row["file_status"], row["product_code"]) for row in detail["history"]} == {
        f"_历史版本/中秋礼盒A_{TODAY}.jpg": ("voided", "voided", "P001-004"), f"_历史版本/提案_{TODAY}.pdf": ("voided", "voided", None)}
    assert {"发起作废", "商品作废", "文件作废", "作废完成"} <= set(actions(client))
    # 恢复：仅终审发起；文件移回原位置，状态和关系恢复。
    body = {"product_ids": [products["P001-004"]], "file_ids": [files["02设计方案/提案.pdf"]], "reason": "误操作"}
    assert client.post(f"/api/projects/{project_id}/ops/restore", json=body).status_code == 403
    restore_id = start(client, "final", "restore", body)
    assert client.get(f"/api/ops/{restore_id}").json()["kind_label"] == "恢复"
    review_and_run(client, restore_id, fault)
    assert (base / "套装" / "中秋礼盒A.jpg").is_file() and (base / "02设计方案" / "提案.pdf").is_file() and list((base / "_历史版本").iterdir()) == []
    with client.app.state.database.session() as session:
        assert session.get(Product, products["P001-004"]).status == "active" and session.get(CatalogFile, files["02设计方案/提案.pdf"]).status == "active"
        assert {row.item_product_id for row in session.scalars(select(ProductRelation).where(ProductRelation.set_product_id == products["P001-004"]))} == {
            products["P001-001"], products["P001-002"], products["P001-003"]}
        assert not list(session.scalars(select(HistoryVersion)))
        assert {row.rel_path: row.id for row in session.scalars(select(CatalogFile))} == files
    assert sorted(archive_of(tmp_path, "单品档案/手提袋.json")["所属套装"]) == ["P001-004"] and archive_of(tmp_path, "套装档案/中秋礼盒A.json")["状态"] == "有效"
    verify_clean(client)
    assert client.get(f"/api/l/{box_link}", follow_redirects=False).status_code == 307


def test_void_set_with_cascade_voids_its_items_too(client: TestClient, tmp_path: Path) -> None:
    products = ids(client)
    batch_id = start(client, "one", "void", {"product_ids": [products["P001-005"]], "reason": "整套下架", "cascade": True})
    preview = client.get(f"/api/ops/{batch_id}").json()["preview"]
    assert len(preview["products"]) == 3 and "这些单品一并作废" in preview["notes"][0]
    review_and_run(client, batch_id)
    with client.app.state.database.session() as session:
        states = {row.product_code: row.status for row in session.scalars(select(Product))}
        # 礼盒B 包含月饼盒与酒瓶：两者一并作废；手提袋和礼盒A 不受影响。
        assert states == {"P001-001": "active", "P001-002": "voided", "P001-003": "voided", "P001-004": "active", "P001-005": "voided"}
        assert {row.item_product_id for row in session.scalars(select(ProductRelation))} == {products["P001-001"]}
    assert len(list((root(tmp_path) / "_历史版本").iterdir())) == 3
    verify_clean(client)
    # 只恢复其中一个单品：它与仍作废的套装之间的关系留待套装恢复时再接回。
    one = start(client, "final", "restore", {"product_ids": [products["P001-003"]], "reason": "酒瓶还要用"})
    review_and_run(client, one)
    with client.app.state.database.session() as session:
        pairs = {(row.set_product_id, row.item_product_id) for row in session.scalars(select(ProductRelation))}
        assert pairs == {(products["P001-004"], products["P001-001"]), (products["P001-004"], products["P001-003"])}
    rest = start(client, "final", "restore", {"product_ids": [products["P001-005"], products["P001-002"]], "reason": "全部恢复"})
    review_and_run(client, rest)
    with client.app.state.database.session() as session:
        assert session.scalar(select(func.count()).select_from(ProductRelation)) == 5
        assert all(row.status == "active" for row in session.scalars(select(Product)))
    verify_clean(client)


@pytest.mark.parametrize("fault", FAULTS)
def test_void_whole_project_moves_folder_and_restore_returns_it(client: TestClient, tmp_path: Path, fault: tuple[str, int] | None) -> None:
    project_id = project_of(client)[0]
    with client.app.state.database.session() as session:
        links = {row.kind: row.link_id for row in session.scalars(select(Link).order_by(Link.id.desc()))}
    assert client.post(f"/api/projects/{project_id}/ops/void-project", json={"reason": "项目取消"}).status_code == 403
    batch_id = start(client, "final", "void-project", {"reason": "项目取消"})
    assert client.get(f"/api/ops/{batch_id}").json()["kind_label"] == "整项目作废"
    review_and_run(client, batch_id, fault)
    # 整个文件夹位于 _已作废项目/。
    assert project_of(client)[1:] == (f"_已作废项目/{FOLDER}", "voided") and not root(tmp_path).exists()
    moved = root(tmp_path, f"_已作废项目/{FOLDER}")
    assert (moved / "单品" / "酒瓶.jpg").is_file() and archive_of(tmp_path, "单品档案/酒瓶.json", f"_已作废项目/{FOLDER}")["状态"] == "已作废"
    assert "行业：酒业" in (moved / "_项目档案.txt").read_text("utf-8")
    with client.app.state.database.session() as session:
        assert all(row.status == "voided" and row.lifecycle["voided_by"] == "project" for row in session.scalars(select(Product)))
    verify_clean(client)
    assert client.app.state.jobs.wait_idle(15)
    assert client.post("/api/search", json={"query": "陶瓷"}).json()["results"] == [] and client.get("/api/projects", params={"q": "P001"}).json()["items"][0]["status"] == "voided"
    for kind in ("project", "product", "file"):
        assert client.get(f"/api/l/{links[kind]}", params={"format": "json"}).json()["message"].endswith("已作废，原因：项目取消"), kind
    # 已作废的项目不能再发起普通操作。
    assert client.post(f"/api/projects/{project_id}/ops/retag").json()["code"] == "invalid_project_state"
    restore_id = start(client, "final", "restore", {"project": True, "reason": "项目重启"})
    assert client.get(f"/api/ops/{restore_id}").json()["kind_label"] == "整项目恢复"
    review_and_run(client, restore_id, fault)
    assert project_of(client)[1:] == (f"{HOME}/{FOLDER}", "active") and root(tmp_path).is_dir() and not moved.exists()
    with client.app.state.database.session() as session:
        assert all(row.status == "active" for row in session.scalars(select(Product)))
    verify_clean(client)
    assert client.get(f"/api/l/{links['file']}").status_code == 200
    assert {"整项目作废", "整项目作废完成", "整项目恢复", "整项目恢复完成"} <= set(actions(client))


# ---- 移动 ----
@pytest.mark.parametrize("fault", FAULTS)
def test_move_project_to_another_company(client: TestClient, tmp_path: Path, fault: tuple[str, int] | None) -> None:
    project_id = project_of(client)[0]
    tree = client.get("/api/catalog/tree").json()[0]["industries"]
    bank = tree[1]["companies"][0]["id"]
    with client.app.state.database.session() as session:
        links = {row.kind: row.link_id for row in session.scalars(select(Link).order_by(Link.id.desc()))}
    body = {"won": False, "company_id": bank, "reason": "实际未中标，且归属金融"}
    assert client.post(f"/api/projects/{project_id}/ops/move", json=body).status_code == 403
    login(client, "final")
    assert client.post(f"/api/projects/{project_id}/ops/move", json={**body, "company_id": 99999}).status_code == 422
    assert client.post(f"/api/projects/{project_id}/ops/move", json={**body, "reason": ""}).status_code == 422
    assert client.post(f"/api/projects/{project_id}/ops/move", json={"won": True, "company_id": tree[0]["companies"][0]["id"], "reason": "x"}).json()["code"] == "no_changes"
    batch_id = start(client, "final", "move", body)
    rows = {row["label"]: (row["before"], row["after"]) for row in client.get(f"/api/ops/{batch_id}").json()["preview"]["rows"]}
    assert rows["项目位置"] == (HOME, "02未中标项目/02金融/01招商银行") and rows["中标状态"] == ("已中标", "未中标")
    # 终审审核时不能用「修改位置」绕过移动流程。
    assert act(client, "first", batch_id, "claim").status_code == 200 and act(client, "first", batch_id, "pass", {"checklist": ALL}).status_code == 200
    assert act(client, "final", batch_id, "claim").json()["can_edit_path"] is False
    assert client.put(f"/api/review/{batch_id}/path", json={"won": True, "company_id": bank}).status_code == 409
    arm(client, fault)
    assert act(client, "final", batch_id, "pass", {"checklist": ALL}).status_code == 200
    settle(client, batch_id, fault)
    target = f"02未中标项目/02金融/01招商银行/{FOLDER}"
    assert project_of(client)[1] == target and root(tmp_path, target).is_dir() and not root(tmp_path).exists()
    with client.app.state.database.session() as session:
        project = session.scalars(select(Project)).one()
        assert (project.won, project.company_id) == (False, bank) and project.industry_id is not None
        entry = session.scalars(select(Oplog).where(Oplog.action == "移动项目")).one()
        assert entry.before["current_path"] == f"{HOME}/{FOLDER}" and entry.after["current_path"] == target and entry.after["reason"] == "实际未中标，且归属金融"
    text = (root(tmp_path, target) / "_项目档案.txt").read_text("utf-8")
    assert "行业：金融   中标：否" in text
    verify_clean(client)
    # 下载和链接正常。
    assert client.get(f"/api/l/{links['file']}").status_code == 200
    assert client.get(f"/api/l/{links['project']}", follow_redirects=False).status_code == 307
    assert client.get("/api/projects", params={"won": "false"}).json()["total"] == 1


# ---- 物理删除 ----
def void_bottle(client: TestClient) -> tuple[int, str]:
    products = ids(client)
    batch_id = start(client, "one", "void", {"product_ids": [products["P001-003"]], "reason": "不再使用"})
    review_and_run(client, batch_id)
    return products["P001-003"], "P001-003"


@pytest.mark.parametrize("fault", [None, ("ops", 1), ("step", 3)])
def test_purge_rules_and_execution_for_voided_product(client: TestClient, tmp_path: Path, fault: tuple[str, int] | None) -> None:
    project_id = project_of(client)[0]
    products = ids(client)
    purge = lambda body: client.post(f"/api/projects/{project_id}/ops/purge", json=body)  # noqa: E731
    with client.app.state.database.session() as session:
        link = session.scalar(select(Link.link_id).where(Link.kind == "product", Link.target_id == products["P001-003"]))
    # 只有终审；对有效内容不可用。
    assert purge({"product_ids": [products["P001-003"]], "reason": "x", "confirm": "P001-003"}).status_code == 403
    login(client, "final")
    assert purge({"product_ids": [products["P001-003"]], "reason": "x", "confirm": "P001-003"}).json()["code"] == "invalid_target"
    assert purge({"history_file_ids": [file_ids(client)["01定稿文件/终稿.ai"]], "reason": "x", "confirm": "P001"}).json()["code"] == "invalid_target"
    assert purge({"project": True, "reason": "x", "confirm": "P001"}).json()["code"] == "invalid_project_state"
    assert purge({"reason": "x", "confirm": "P001"}).json()["code"] == "no_changes"
    product_id, code = void_bottle(client)
    image = root(tmp_path) / "_历史版本" / f"酒瓶_{TODAY}.jpg"
    assert image.is_file() and (root(tmp_path) / "单品档案" / "酒瓶.json").is_file()
    login(client, "final")
    preview = client.post(f"/api/projects/{project_id}/ops/purge/preview", json={"product_ids": [product_id]}).json()
    assert preview["in_cooling"] and preview["cooling_days"] == 30 and preview["confirm_label"] == "商品编号" and preview["count"] == 2
    assert [item["path"].rsplit("/", 2)[-2:] for item in preview["files"]] == [["_历史版本", f"酒瓶_{TODAY}.jpg"], ["单品档案", "酒瓶.json"]]
    assert preview["total_size"] == len(jpeg()) and preview["summary"] == ["已作废商品 P001-003 酒瓶（主图和档案）"]
    body = {"product_ids": [product_id], "reason": "客户要求彻底删除", "confirm": code}
    # 冷静期内需要例外原因；确认码错误不执行。
    cooling = purge(body)
    assert cooling.status_code == 409 and cooling.json()["code"] == "cooling_period"
    wrong = purge({**body, "exception_reason": "法务要求", "confirm": "P001"})
    assert wrong.status_code == 422 and wrong.json()["code"] == "confirm_mismatch" and image.is_file()
    assert purge({**body, "exception_reason": "法务要求", "reason": " "}).status_code == 422
    arm(client, fault)
    done = purge({**body, "exception_reason": "法务要求", "confirm": " p001-003 "})
    assert done.status_code == 200 and (done.json()["kind_label"], done.json()["status"]) == ("物理删除", "入库中")
    settle(client, done.json()["id"], fault)
    # 文件在 NAS 上不存在；卡片柜保留记录并标记已删除。
    assert not image.exists() and not (root(tmp_path) / "单品档案" / "酒瓶.json").exists()
    with client.app.state.database.session() as session:
        product = session.get(Product, product_id)
        assert product.status == "deleted" and product.lifecycle["delete_reason"] == "客户要求彻底删除" and product.lifecycle["exception_reason"] == "法务要求"
        assert product.lifecycle["void_reason"] == "不再使用" and product.lifecycle["voided_at"] and product.lifecycle["deleted_at"]
        file = session.get(CatalogFile, product.file_id)
        assert (file.status, file.rel_path, file.fingerprint) == ("deleted", f"_历史版本/酒瓶_{TODAY}.jpg", hashlib.sha256(jpeg()).hexdigest())
        assert file.lifecycle["expected_missing"] is True and session.scalars(select(HistoryVersion)).one().expected_missing is True
        assert len(list(session.scalars(select(ProductTag).where(ProductTag.product_id == product_id)))) == 11
        batch = session.get(Batch, done.json()["id"])
        assert batch.kind == "purge" and batch.first_reviewer_id is None and batch.final_reviewer_id == batch.owner_id
        entry = session.scalars(select(Oplog).where(Oplog.action == "物理删除")).one()
        assert entry.after == {"reason": "客户要求彻底删除", "skipped_cooling": True, "exception_reason": "法务要求", "deleted": 2, "whole_project": False}
        assert [item["size"] for item in entry.before["files"]] == [len(jpeg()), 0]
    # 链接显示删除说明。
    message = client.get(f"/api/l/{link}", params={"format": "json"}).json()
    today = archive.local_date(datetime.now(timezone.utc))
    assert message["status"] == "deleted" and message["message"] == f"该商品已于 {today} 删除，原因：客户要求彻底删除"
    assert client.get(f"/api/files/{file.id}/download").status_code == 410
    # 缩略图缓存一并清掉。
    assert not (client.app.state.postops.thumb_root / "product" / str(product_id)).exists()
    text = (root(tmp_path) / "_项目档案.txt").read_text("utf-8")
    assert "P001-003" not in text and "酒瓶" not in text
    verify_clean(client)
    assert client.get(f"/api/projects/{project_id}").json()["project"]["products"] == 5
    login(client, "final")
    assert purge({**body, "exception_reason": "x"}).json()["code"] == "invalid_target"


def test_purge_after_cooling_period_history_file_and_whole_project(client: TestClient, tmp_path: Path) -> None:
    project_id = project_of(client)[0]
    files = file_ids(client)
    batch_id = start(client, "one", "void", {"file_ids": [files["02设计方案/提案.pdf"]], "reason": "过期方案"})
    review_and_run(client, batch_id)
    with client.app.state.database.session() as session:
        row = session.scalars(select(HistoryVersion)).one()
        row.created_at = datetime.now(timezone.utc) - timedelta(days=31)
    login(client, "final")
    preview = client.post(f"/api/projects/{project_id}/ops/purge/preview", json={"history_file_ids": [files["02设计方案/提案.pdf"]]}).json()
    assert not preview["in_cooling"] and preview["confirm_label"] == "立项编号" and preview["summary"] == [f"历史版本文件 _历史版本/提案_{TODAY}.pdf"]
    # 冷静期已满：不需要例外原因。
    done = client.post(f"/api/projects/{project_id}/ops/purge", json={"history_file_ids": [files["02设计方案/提案.pdf"]], "reason": "清理", "confirm": "P001"})
    assert done.status_code == 200
    settle(client, done.json()["id"], None)
    assert list((root(tmp_path) / "_历史版本").iterdir()) == []
    with client.app.state.database.session() as session:
        assert session.get(CatalogFile, files["02设计方案/提案.pdf"]).status == "deleted"
        assert session.scalars(select(Oplog).where(Oplog.action == "物理删除")).one().after["skipped_cooling"] is False
    assert "提案" not in (root(tmp_path) / "_项目档案.txt").read_text("utf-8")
    verify_clean(client)
    # 整个项目：先整项目作废，再删除。
    void_id = start(client, "final", "void-project", {"reason": "项目终止"})
    review_and_run(client, void_id)
    login(client, "final")
    preview = client.post(f"/api/projects/{project_id}/ops/purge/preview", json={"project": True}).json()
    assert preview["in_cooling"] and preview["count"] == 7 + 5 + 1 and "整个项目" in preview["summary"][0]
    assert client.post(f"/api/projects/{project_id}/ops/purge", json={"project": True, "product_ids": [1], "reason": "x", "confirm": "P001"}).status_code == 422
    done = client.post(f"/api/projects/{project_id}/ops/purge", json={"project": True, "reason": "合同到期销毁", "exception_reason": "客户书面要求", "confirm": "P001"})
    assert done.status_code == 200
    settle(client, done.json()["id"], None)
    assert not root(tmp_path, f"_已作废项目/{FOLDER}").exists()
    with client.app.state.database.session() as session:
        project = session.scalars(select(Project)).one()
        assert project.status == "deleted" and project.lifecycle["delete_reason"] == "合同到期销毁" and project.current_path == f"_已作废项目/{FOLDER}"
        assert all(row.status == "deleted" for row in session.scalars(select(Product))) and all(row.status == "deleted" for row in session.scalars(select(CatalogFile)))
        link = session.scalar(select(Link.link_id).where(Link.kind == "project"))
    assert "删除，原因：合同到期销毁" in client.get(f"/api/l/{link}", params={"format": "json"}).json()["message"]
    assert client.get("/api/projects", params={"q": "P001"}).json()["items"][0]["status"] == "deleted"
    # 编号仍被占用，不能再用同一编号上传。
    assert client.post("/api/batches/precheck", json={"folder_name": FOLDER, "files": []}).json()["issues"][0]["level"] == "block"


# ---- 通用规则 ----
def test_one_open_operation_per_project_and_rejected_ops_can_be_withdrawn(client: TestClient) -> None:
    products = ids(client)
    project_id = project_of(client)[0]
    batch_id = start(client, "one", "rename", {"project_name": "新名字"})
    for path, body in (("rename", {"project_name": "再改"}), ("retag", {}), ("void", {"product_ids": [products["P001-001"]], "reason": "x"})):
        busy = client.post(f"/api/projects/{project_id}/ops/{path}", json=body)
        assert busy.status_code == 409 and busy.json()["code"] == "project_busy" and busy.json()["detail"]["batch_id"] == batch_id, path
    assert client.post("/api/batches/precheck", json={"folder_name": FOLDER, "files": []}).json()["issues"][0]["level"] == "block"
    detail = client.get(f"/api/projects/{project_id}").json()
    assert detail["open_batch"] == {"id": batch_id, "batch_no": detail["open_batch"]["batch_no"], "kind": "rename", "kind_label": "改名", "status": "待初审"}
    home = {item["id"]: item for item in client.get("/api/batches").json()}
    assert home[batch_id]["kind_label"] == "改名" and home[batch_id]["project_id"] == project_id
    assert act(client, "first", batch_id, "claim").status_code == 200 and act(client, "first", batch_id, "reject", {"comment": "名字不合适"}).status_code == 200
    login(client, "one")
    rejected = client.get(f"/api/ops/{batch_id}").json()
    assert rejected["status"] == "已驳回" and rejected["history"][0]["comment"] == "名字不合适" and rejected["can_withdraw"]
    assert client.post(f"/api/projects/{project_id}/ops/retag").json()["code"] == "project_busy"
    assert client.post(f"/api/ops/{batch_id}/withdraw").json()["status"] == "已撤回"
    assert client.get(f"/api/projects/{project_id}").json()["open_batch"] is None
    assert client.post(f"/api/projects/{project_id}/ops/retag").status_code == 200
    assert client.get("/api/ops/99999").status_code == 404 and client.post("/api/ops/99999/withdraw").status_code == 404
    assert project_of(client)[1] == f"{HOME}/{FOLDER}"


def test_roles_required_to_initiate_and_execution_needs_final_session(client: TestClient, tmp_path: Path) -> None:
    project_id = project_of(client)[0]
    with client.app.state.database.session() as session:
        session.scalar(select(User).where(User.username == "one")).roles = []
    login(client, "one")
    assert client.post(f"/api/projects/{project_id}/ops/retag").status_code == 403
    with client.app.state.database.session() as session:
        session.scalar(select(User).where(User.username == "one")).roles = ["uploader"]
    # 以没有正式目录写权限的会话执行：被 NAS 拒绝，批次停在入库异常，文件原样未动。
    batch_id = start(client, "one", "rename", {"project_name": "权限测试"})
    assert act(client, "first", batch_id, "claim").status_code == 200 and act(client, "first", batch_id, "pass", {"checklist": ALL}).status_code == 200
    login(client, "one")
    with client.app.state.database.session() as session:
        batch = session.get(Batch, batch_id)
        one = session.scalar(select(User).where(User.username == "one"))
        batch.status, batch.final_reviewer_id = "入库中", one.id
        client.app.state.ingest.enqueue(session, batch, one)
    assert client.app.state.jobs.wait_idle(15)
    status, step, error = batch_state(client, batch_id)
    assert (status, step) == ("入库异常", 1) and error.startswith("第 2 步（移动与写入文件）") and "NAS 无权限" in error
    assert root(tmp_path).is_dir() and project_of(client)[1] == f"{HOME}/{FOLDER}"
    login(client, "first")
    page = client.get(f"/api/review/{batch_id}").json()
    assert page["ingest"]["total"] == 5 and page["ingest"]["current"] == "移动与写入文件" and not page["ingest"]["can_resume"]
