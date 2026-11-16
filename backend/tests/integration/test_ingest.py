"""WP09 入库执行器：9 步、幂等、断点继续、档案与卡片柜一致。"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import delete, func, select

from app.config import DevUser, PathsConfig, Settings
from app.db.models import (Batch, BatchProduct, CatalogFile, Job, Link, Oplog, Product, ProductRelation, ProductTag, Project, User, UserSession)
from app.main import create_app
from app.services import archive
from app.services.links import resolve
from tests.integration.test_audit import ALL, act, login
from tests.integration.test_review import jpeg, named, review, staged

ROLES = {"one": ["uploader"], "first": ["first_reviewer"], "final": ["final_reviewer"], "admin": ["uploader", "admin"]}
PATHS = ["单品/酒瓶.jpg", "单品/月饼盒.jpg", "单品/手提袋.jpg", "单品/多余.jpg", "套装/中秋礼盒A.jpg", "套装/中秋礼盒B.jpg",
         "01定稿文件/终稿.ai", "05 详情页/主视觉.jpg"]
FOLDER = "P001_企业_项目"
TARGET = "设计资产/01已中标项目/01酒业/01企业/" + FOLDER


@contextmanager
def application(root: Path) -> Iterator[TestClient]:
    settings = Settings(mode="dev", paths=PathsConfig(data_dir=root / "data", dev_nas_dir=root / "nas"), bootstrap_admin="admin")
    # 只有终审账号对正式目录有写权限，其余账号只读。
    settings.dev_users = [DevUser(username=name, password="fake", permissions={"temp": "write", "formal": "write" if name == "final" else "read"}) for name in ROLES]
    with TestClient(create_app(settings, log_dir=root / "logs")) as client:
        for name in ROLES:
            login(client, name)
        with client.app.state.database.session() as session:
            for user in session.scalars(select(User)):
                user.roles = ROLES[user.username]
        login(client, "admin")
        assert client.put("/api/admin/company-map", json={"industries": [{"name": "酒业"}], "companies": [{"name": "企业", "industry_name": "酒业"}]}).status_code == 200
        yield client


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with application(tmp_path) as client:
        yield client


def to_final(client: TestClient, paths: list[str] = PATHS) -> int:
    """上传、核对（移除一张单品）、提交、初审通过、终审领取；返回批次号。"""
    login(client, "one")
    batch_id = staged(client, paths)
    data = review(client, batch_id)
    products = named(data)
    if "多余" in products:
        assert client.delete(f"/api/batches/{batch_id}/products/{products['多余']['id']}").status_code == 200
    for product in review(client, batch_id)["products"]:
        if product["kind"] == "set":
            ids = [item["batch_product_id"] for item in product["relation_items"]]
            ids = ids[:2] if product["display_name"] == "中秋礼盒B" else ids
            assert client.put(f"/api/batches/{batch_id}/products/{product['id']}/relations", json={"item_ids": ids}).status_code == 200
    if "酒瓶" in products:
        client.patch(f"/api/batches/{batch_id}/products/{products['酒瓶']['id']}", json={"tags": {"material": "玻璃"}})
    assert client.post(f"/api/batches/{batch_id}/submit", json={}).status_code == 200
    assert act(client, "first", batch_id, "claim").status_code == 200
    assert act(client, "first", batch_id, "pass", {"checklist": ALL}).status_code == 200
    assert act(client, "final", batch_id, "claim").status_code == 200
    return batch_id


def approve(client: TestClient, batch_id: int) -> dict:
    response = act(client, "final", batch_id, "pass", {"checklist": ALL})
    assert response.status_code == 200, response.text
    assert client.app.state.jobs.wait_idle(10)
    return response.json()


def batch_of(client: TestClient, batch_id: int) -> tuple[str, int, str | None]:
    with client.app.state.database.session() as session:
        batch = session.get(Batch, batch_id)
        return batch.status, batch.ingest_step, batch.ingest_error


def tree(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "<目录>"
            for path in sorted(root.rglob("*"))}


def snapshot(client: TestClient, root: Path) -> dict[str, object]:
    """入库结果的完整快照（不含随机生成的链接 ID 与时间戳）。"""
    with client.app.state.database.session() as session:
        codes = {row.id: row.product_code for row in session.scalars(select(Product))}
        files = {row.id: row.rel_path for row in session.scalars(select(CatalogFile))}
        return {"nas": tree(root / "nas"), "thumbs": sorted(str(path.relative_to(root / "data" / "thumbs")) for path in (root / "data" / "thumbs").rglob("*.jpg")),
            "projects": [(row.project_code, row.current_path, row.status, row.won, row.code_source, row.company_id is not None) for row in session.scalars(select(Project))],
            "files": sorted((row.rel_path, row.size, row.fingerprint, row.fingerprint_kind, row.category, row.status) for row in session.scalars(select(CatalogFile))),
            "products": sorted((row.product_code, row.kind, row.display_name, files[row.file_id], row.archive_rel_path, row.free_text, row.vision_model) for row in session.scalars(select(Product))),
            "relations": sorted((codes[row.set_product_id], codes[row.item_product_id], row.source) for row in session.scalars(select(ProductRelation))),
            "tags": sorted((codes[row.product_id], row.field_key, row.value, row.source) for row in session.scalars(select(ProductTag))),
            "links": sorted(session.execute(select(Link.kind, func.count()).group_by(Link.kind)).all()),
            "batch_codes": sorted(row.product_code or "" for row in session.scalars(select(BatchProduct))),
            "batch": [(row.status, row.ingest_step, row.ingest_error, row.final_path) for row in session.scalars(select(Batch))],
            "embed_jobs": len(list(session.scalars(select(Job).where(Job.kind == "embed"))))}


def run_steps_again(client: TestClient, batch_id: int) -> None:
    ingest = client.app.state.ingest
    with client.app.state.database.session() as session:
        final_id = session.scalar(select(User.id).where(User.username == "final"))
    fs = client.app.state.auth.pool.get(final_id)
    for step in (ingest._check_target, ingest._make_parents, ingest._move, ingest._assign_codes, ingest._write_product_archives,
                 ingest._write_project_archive, ingest._register, ingest._links, ingest._finish):
        step(batch_id, fs)


def test_full_ingest_layout_codes_archives_catalog_and_links(client: TestClient, tmp_path: Path) -> None:
    batch_id = to_final(client)
    assert approve(client, batch_id)["review"]["batch"]["status"] == "入库中"
    assert batch_of(client, batch_id) == ("已入库", 9, None)
    nas = tmp_path / "nas"
    project = nas / TARGET
    assert sorted(path.name for path in project.iterdir()) == ["01定稿文件", "05 详情页", "_历史版本", "_项目档案.txt", "单品", "单品档案", "套装", "套装档案"]
    assert sorted(path.name for path in (project / "单品").iterdir()) == ["手提袋.jpg", "月饼盒.jpg", "酒瓶.jpg"]
    assert list((project / "_历史版本").iterdir()) == []
    with client.app.state.database.session() as session:
        batch_no = session.get(Batch, batch_id).batch_no
    # 临时区只剩批次的回收区。
    assert tree(nas / "工作台临时区") == {batch_no: "<目录>", f"{batch_no}/_回收区": "<目录>", f"{batch_no}/_回收区/单品": "<目录>",
                                       f"{batch_no}/_回收区/单品/多余.jpg": hashlib.sha256(jpeg()).hexdigest()}
    # 编号连续：先单品（按文件名排序）后套装。
    items = {path.stem: json.loads(path.read_text("utf-8")) for path in (project / "单品档案").iterdir()}
    sets = {path.stem: json.loads(path.read_text("utf-8")) for path in (project / "套装档案").iterdir()}
    assert {name: doc["商品编号"] for name, doc in items.items()} == {"手提袋": "P001-001", "月饼盒": "P001-002", "酒瓶": "P001-003"}
    assert {name: doc["商品编号"] for name, doc in sets.items()} == {"中秋礼盒A": "P001-004", "中秋礼盒B": "P001-005"}
    assert all(doc["结束标记"] == "完" and doc["状态"] == "有效" and doc["立项编号"] == "P001" and doc["识图模型"] == "fake-vision" for doc in [*items.values(), *sets.values()])
    # 套装档案的「包含单品」与单品档案的「所属套装」互相对应。
    contains = {doc["商品编号"]: {item["商品编号"] for item in doc["包含单品"]} for doc in sets.values()}
    assert contains["P001-004"] == {"P001-001", "P001-002", "P001-003"} and len(contains["P001-005"]) == 2
    for doc in items.values():
        assert set(doc["所属套装"]) == {code for code, members in contains.items() if doc["商品编号"] in members}
    assert items["酒瓶"]["标签"]["材质"] == "玻璃" and items["酒瓶"]["标签来源"]["材质"] == "人工" and items["酒瓶"]["标签来源"]["品类"] == "AI"
    assert items["酒瓶"]["主图相对路径"] == "单品/酒瓶.jpg" and items["酒瓶"]["主图指纹"] == hashlib.sha256(jpeg()).hexdigest()
    text = (project / "_项目档案.txt").read_text("utf-8")
    parsed = archive.parse_project(text)
    assert (parsed.project_code, parsed.company, parsed.industry, parsed.won, parsed.uploader, parsed.first_reviewer, parsed.final_reviewer) == ("P001", "企业", "酒业", True, "one", "first", "final")
    assert [line.code for line in parsed.products] == ["P001-001", "P001-002", "P001-003", "P001-004", "P001-005"]
    assert parsed.products[3].related == ("001", "002", "003") and "P001-004" in [f"P001-{serial}" for serial in parsed.products[0].related]
    # 卡片柜里的文件数、大小、指纹与 NAS 上实际一致。
    with client.app.state.database.session() as session:
        record = session.scalars(select(Project)).one()
        assert (record.current_path, record.status, record.folder_name) == ("01已中标项目/01酒业/01企业/" + FOLDER, "active", FOLDER)
        catalog = list(session.scalars(select(CatalogFile)))
        on_disk = {str(path.relative_to(project)): path for path in project.rglob("*") if path.is_file()
                   and not str(path.relative_to(project)).startswith(("单品档案", "套装档案", "_"))}
        assert {row.rel_path for row in catalog} == set(on_disk) and len(catalog) == 7
        for row in catalog:
            data = on_disk[row.rel_path].read_bytes()
            assert (row.size, row.fingerprint, row.fingerprint_kind, row.status) == (len(data), hashlib.sha256(data).hexdigest(), "full", "active")
        assert {row.rel_path: row.category for row in catalog}["05 详情页/主视觉.jpg"] == "05 详情页"
        assert {row.category for row in catalog} == {"01定稿文件", "05 详情页", "单品", "套装"}
        assert [row.rel_path for row in parsed.files] == sorted(row.rel_path for row in catalog)
        assert session.scalar(select(func.count()).select_from(Product)) == 5 and session.scalar(select(func.count()).select_from(ProductRelation)) == 5
        assert session.scalar(select(func.count()).select_from(ProductTag)) == 55
        # 永久链接：项目、每个商品、每个文件各一条，可解析。
        links = list(session.scalars(select(Link)))
        assert sorted((kind, count) for kind, count in session.execute(select(Link.kind, func.count()).group_by(Link.kind))) == [("file", 7), ("product", 5), ("project", 1)]
        assert len({link.link_id for link in links}) == 13 and all(len(link.link_id) == 10 for link in links)
        assert resolve(session, links[0].link_id) == (links[0].kind, links[0].target_id) and resolve(session, "nope") is None
        final_id = session.scalar(select(User.id).where(User.username == "final"))
        fs = client.app.state.auth.pool.get(final_id)
        assert archive.verify(session, fs, client.app.state.ingest.paths, record) == []
        # 缩略图迁到商品目录，原接口仍能取到；每个商品一个 embed 任务。
        product_ids = list(session.scalars(select(Product.id)))
        assert all((tmp_path / "data" / "thumbs" / "product" / str(value) / "300.jpg").is_file() for value in product_ids)
        assert not (tmp_path / "data" / "thumbs" / "batch" / str(batch_id)).exists()
        assert [job.status for job in session.scalars(select(Job).where(Job.kind == "embed"))] == ["done"] * 5
        ingest_job = session.scalars(select(Job).where(Job.kind == "ingest")).one()
        assert ingest_job.run_as_user_id == final_id and ingest_job.status == "done"
        assert [entry.after["ingest_step"] for entry in session.scalars(select(Oplog).where(Oplog.action == "入库步骤完成").order_by(Oplog.id))] == list(range(1, 10))
    login(client, "final")
    page = client.get(f"/api/review/{batch_id}").json()
    assert page["ingest"] == {"step": 9, "total": 9, "current": None, "error": None, "can_resume": False}
    assert client.get(f"/api/batches/{batch_id}/thumbs/{page['review']['products'][0]['batch_file_id']}/300").status_code == 200
    assert client.post(f"/api/review/{batch_id}/resume-ingest").status_code == 409


def test_every_step_is_idempotent_after_success(client: TestClient, tmp_path: Path) -> None:
    batch_id = to_final(client)
    approve(client, batch_id)
    before = snapshot(client, tmp_path)
    for _ in range(2):
        run_steps_again(client, batch_id)
    assert client.app.state.jobs.wait_idle(5)
    assert snapshot(client, tmp_path) == before
    assert before["batch_codes"] == ["", "P001-001", "P001-002", "P001-003", "P001-004", "P001-005"]


@pytest.mark.parametrize("step", [1, 2, 3, 4, 5, 6, 7, 8])
def test_interrupted_after_step_resumes_to_identical_result(tmp_path: Path, step: int) -> None:
    with application(tmp_path / "clean") as clean:
        approve(clean, to_final(clean))
        expected = snapshot(clean, tmp_path / "clean")
    with application(tmp_path / "broken") as client:
        batch_id = to_final(client)
        client.app.state.ingest.fault_after = step
        approve(client, batch_id)
        status, done, error = batch_of(client, batch_id)
        assert (status, done) == ("入库异常", step) and error.startswith(f"第 {step + 1} 步") and "强制中断" in error
        page = client.get(f"/api/review/{batch_id}").json()
        assert page["ingest"]["can_resume"] and page["ingest"]["step"] == step and page["ingest"]["error"] == error
        assert {item["key"] for item in client.get("/api/todos").json()} == {"ingest_broken"}
        login(client, "admin")
        assert "ingest_alert" in {item["key"] for item in client.get("/api/todos").json()}
        login(client, "final")
        assert client.post(f"/api/review/{batch_id}/resume-ingest").json()["review"]["batch"]["status"] == "入库中"
        assert client.app.state.jobs.wait_idle(10)
        assert batch_of(client, batch_id) == ("已入库", 9, None)
        assert client.get("/api/todos").json() == []
        result = snapshot(client, tmp_path / "broken")
        assert result == expected
        # 没有重复的商品编号、重复的记录或残缺的档案。
        codes = [code for code in result["batch_codes"] if code]
        assert len(codes) == len(set(codes)) == 5 and not [name for name in result["nas"] if name.endswith(".写入中")]


def test_logged_out_final_reviewer_blocks_job_until_relogin_and_continue(client: TestClient) -> None:
    batch_id = to_final(client)
    client.app.state.ingest.fault_after = 2
    approve(client, batch_id)
    assert batch_of(client, batch_id)[0] == "入库异常"
    # 模拟「终审已退出登录，队列里还有待执行的入库任务」。
    # 测试里同一账号登录过多次（相当于多台电脑）；先让其余登录失效，这一次退出才是最后一个会话，NAS 连接随之关闭。
    login(client, "final")
    with client.app.state.database.session() as session:
        final_id = session.scalar(select(User.id).where(User.username == "final"))
        current = session.scalars(select(UserSession).where(UserSession.user_id == final_id).order_by(UserSession.id.desc())).first()
        session.execute(delete(UserSession).where(UserSession.user_id == final_id, UserSession.id != current.id))
    assert client.post("/api/auth/logout").status_code == 200
    with client.app.state.database.session() as session:
        batch = session.get(Batch, batch_id)
        batch.status, batch.ingest_error = "入库中", None
        client.app.state.ingest.enqueue(session, batch, session.get(User, batch.final_reviewer_id))
    assert client.app.state.jobs.wait_idle(10)
    status, step, error = batch_of(client, batch_id)
    assert (status, step) == ("入库异常", 2) and "操作人会话已失效" in error
    for user, code in (("one", 403), ("first", 403), ("admin", 403)):
        login(client, user)
        assert client.post(f"/api/review/{batch_id}/resume-ingest").status_code == code
    login(client, "final")
    assert client.post(f"/api/review/{batch_id}/resume-ingest").status_code == 200
    assert client.app.state.jobs.wait_idle(10)
    assert batch_of(client, batch_id) == ("已入库", 9, None)
    assert client.post("/api/review/999/resume-ingest").status_code == 404


def test_move_with_non_final_nas_session_is_denied_by_local_fs(client: TestClient, tmp_path: Path) -> None:
    batch_id = to_final(client)
    client.app.state.ingest.fault_after = 2
    approve(client, batch_id)
    assert batch_of(client, batch_id)[:2] == ("入库异常", 2)
    # 把任务换成只有正式目录只读权限的账号的会话来执行第 3 步。
    login(client, "one")
    with client.app.state.database.session() as session:
        batch = session.get(Batch, batch_id)
        one = session.scalar(select(User).where(User.username == "one"))
        batch.status, batch.ingest_error, batch.final_reviewer_id = "入库中", None, one.id
        client.app.state.ingest.enqueue(session, batch, one)
    assert client.app.state.jobs.wait_idle(10)
    status, step, error = batch_of(client, batch_id)
    assert (status, step) == ("入库异常", 2) and error.startswith("第 3 步") and "NAS 无权限" in error
    assert not (tmp_path / "nas" / TARGET).exists()
    with client.app.state.database.session() as session:
        assert (tmp_path / "nas" / "工作台临时区" / session.get(Batch, batch_id).batch_no / FOLDER / "单品" / "酒瓶.jpg").is_file()
        assert not list(session.scalars(select(Project)))


@pytest.mark.parametrize("obstacle", [FOLDER, "P001_企业_另一个名字"])
def test_existing_same_name_or_same_code_folder_fails_step_one(client: TestClient, tmp_path: Path, obstacle: str) -> None:
    batch_id = to_final(client)
    blocker = tmp_path / "nas" / "设计资产/01已中标项目/01酒业/01企业" / obstacle
    blocker.mkdir(parents=True)
    approve(client, batch_id)
    status, step, error = batch_of(client, batch_id)
    assert (status, step) == ("入库异常", 0) and error.startswith("第 1 步") and ("同名" in error or "同编号" in error)
    with client.app.state.database.session() as session:
        assert not [row for row in session.scalars(select(BatchProduct)) if row.product_code]
    blocker.rmdir()
    login(client, "final")
    assert client.post(f"/api/review/{batch_id}/resume-ingest").status_code == 200
    assert client.app.state.jobs.wait_idle(10)
    assert batch_of(client, batch_id) == ("已入库", 9, None)


def test_code_already_in_catalog_fails_step_one(client: TestClient) -> None:
    batch_id = to_final(client)
    with client.app.state.database.session() as session:
        session.add(Project(project_code="P001", code_source="company", company_name="别家", won=True, project_name="旧", folder_name="P001_别家_旧", current_path="x", status="active"))
    approve(client, batch_id)
    assert batch_of(client, batch_id)[0] == "入库异常" and "已有入库项目" in batch_of(client, batch_id)[2]


def test_verify_reports_differences_and_rebuild_restores(client: TestClient, tmp_path: Path) -> None:
    batch_id = to_final(client)
    approve(client, batch_id)
    project_dir = tmp_path / "nas" / TARGET
    paths = client.app.state.ingest.paths
    with client.app.state.database.session() as session:
        project = session.scalars(select(Project)).one()
        fs = client.app.state.auth.pool.get(session.scalar(select(User.id).where(User.username == "final")))
        original = tree(project_dir)
        (project_dir / "单品档案" / "酒瓶.json").unlink()
        bottle = project_dir / "单品档案" / "月饼盒.json"
        bottle.write_text(bottle.read_text("utf-8").replace('"结束标记": "完"', '"结束标记": ""'), "utf-8")
        box = project_dir / "套装档案" / "中秋礼盒A.json"
        box.write_text(box.read_text("utf-8").replace("AI推荐", "人工补充", 1), "utf-8")
        (project_dir / "套装档案" / "不认识.json").write_text("{}", "utf-8")
        summary = project_dir / "_项目档案.txt"
        summary.write_text(summary.read_text("utf-8")[:80], "utf-8")
        found = {(item.path, item.problem) for item in archive.verify(session, fs, paths, project)}
        assert found == {("单品档案/酒瓶.json", "missing"), ("单品档案/月饼盒.json", "damaged"), ("套装档案/中秋礼盒A.json", "mismatch"),
                         ("套装档案/不认识.json", "unexpected"), ("_项目档案.txt", "damaged")}
        assert archive.rebuild(session, fs, paths, project) == 5
        assert archive.verify(session, fs, paths, project) == [] and tree(project_dir) == original
        assert archive.rebuild(session, fs, paths, project) == 0
        # 卡片柜变了，档案随之不一致；重写后以卡片柜为准。
        session.scalar(select(Product).where(Product.product_code == "P001-003")).free_text = "改过的描述"
        session.flush()
        assert [(item.path, item.problem) for item in archive.verify(session, fs, paths, project)] == [("单品档案/酒瓶.json", "mismatch")]
        assert archive.rebuild(session, fs, paths, project) == 1
        assert json.loads((project_dir / "单品档案" / "酒瓶.json").read_text("utf-8"))["自由描述"] == "改过的描述"
        # 档案解析回结构化数据后与卡片柜生成的一致。
        docs = archive.from_catalog(session, project)
        assert archive.parse_project((project_dir / "_项目档案.txt").read_text("utf-8")) == docs.project
        for doc in docs.products:
            assert archive.parse_product((project_dir / doc.archive_rel_path).read_text("utf-8"), doc.archive_rel_path) == doc


def test_same_display_name_gets_distinct_archives_and_h_code_source(client: TestClient, tmp_path: Path) -> None:
    batch_id = to_final(client, ["单品/杯子.jpg", "单品/杯子.png", "单品/碗.jpg"])
    approve(client, batch_id)
    assert batch_of(client, batch_id) == ("已入库", 9, None)
    names = sorted(path.name for path in (tmp_path / "nas" / TARGET / "单品档案").iterdir())
    assert names == ["杯子.jpg.json", "杯子.png.json", "碗.json"]
    with client.app.state.database.session() as session:
        assert sorted(session.scalars(select(Product.archive_rel_path))) == ["单品档案/杯子.jpg.json", "单品档案/杯子.png.json", "单品档案/碗.json"]
        assert not (tmp_path / "nas" / TARGET / "套装档案").exists() or not list((tmp_path / "nas" / TARGET / "套装档案").iterdir())
