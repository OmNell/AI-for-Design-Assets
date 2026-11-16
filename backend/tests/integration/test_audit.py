"""WP08 两级审核、对照表、目录树与待办。"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.config import DevUser, Settings
from app.db.models import Batch, Oplog, ReviewPin, ReviewRound, User
from app.main import create_app
from app.services.notify import business_days_elapsed
from tests.integration.test_review import confirm_relations, jpeg, named, review, staged

ALL = {"path": True, "naming": True, "one_product": True, "relations": True, "tags": True, "content": True}
ROLES = {"one": ["uploader"], "first": ["first_reviewer"], "first2": ["first_reviewer"], "final": ["final_reviewer"],
         "both": ["first_reviewer", "final_reviewer"], "admin": ["uploader", "admin"]}


@pytest.fixture
def client(settings: Settings, tmp_path: Path) -> Iterator[TestClient]:
    settings.paths.dev_nas_dir = tmp_path / "nas"
    settings.bootstrap_admin = "admin"
    settings.dev_users = [DevUser(username=name, password="fake", permissions={"temp": "write"}) for name in ROLES]
    with TestClient(create_app(settings, log_dir=tmp_path / "logs")) as client:
        for name in ROLES:
            login(client, name)
        with client.app.state.database.session() as session:
            for user in session.scalars(select(User)):
                user.roles = ROLES[user.username]
        # 本文件只验证审核状态机；入库执行在 test_ingest.py，单独关闭派发后批次停在「入库中」。
        client.app.state.audit.ingest = None
        login(client, "one")
        yield client


def login(client: TestClient, user: str) -> int:
    response = client.post("/api/auth/login", json={"username": user, "password": "fake"})
    assert response.status_code == 200
    return response.json()["id"] if "id" in response.json() else client.get("/api/auth/me").json()["id"]


def submitted(client: TestClient, paths: list[str] | None = None) -> int:
    """以 one 上传并提交到「待初审」。"""
    login(client, "one")
    batch_id = staged(client, paths or ["单品/杯子.jpg", "单品/雨伞.jpg", "套装/礼盒.jpg", "01定稿文件/终稿.ai"])
    confirm_relations(client, batch_id, review(client, batch_id))
    assert client.post(f"/api/batches/{batch_id}/submit", json={"note": "请看位置"}).status_code == 200
    return batch_id


def add_company(client: TestClient, name: str = "企业", industry: str = "酒业", aliases: list[str] | None = None) -> dict:
    login(client, "admin")
    current = client.get("/api/admin/company-map").json()
    body: dict = {"industries": [], "companies": [{"name": name, "aliases": aliases or [], "industry_name": industry}]}
    if industry not in [item["name"] for item in current["industries"]]:
        body["industries"].append({"name": industry})
    response = client.put("/api/admin/company-map", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def act(client: TestClient, user: str, batch_id: int, action: str, body: dict | None = None):
    login(client, user)
    return client.post(f"/api/review/{batch_id}/{action}", json=body) if body is not None else client.post(f"/api/review/{batch_id}/{action}")


def status(client: TestClient, batch_id: int) -> str:
    with client.app.state.database.session() as session:
        return session.get(Batch, batch_id).status


def first_pass(client: TestClient, batch_id: int, user: str = "first") -> None:
    assert act(client, user, batch_id, "claim").status_code == 200
    assert act(client, user, batch_id, "pass", {"checklist": ALL}).status_code == 200


# ---- 对照表与目录树 ----
def test_company_map_assigns_sequences_without_reordering_and_logs(client: TestClient) -> None:
    add_company(client, "茅台", "酒业", ["贵州茅台", "MOUTAI"])
    add_company(client, "泸州老窖", "酒业")
    data = add_company(client, "招商银行", "金融")
    assert [(item["name"], item["seq"]) for item in data["industries"]] == [("酒业", 1), ("金融", 2)]
    companies = {item["name"]: item for item in data["companies"]}
    assert (companies["茅台"]["seq"], companies["泸州老窖"]["seq"], companies["招商银行"]["seq"]) == (1, 2, 1)
    # 改名与换行业不重排其他序号；换行业后接在新行业末尾。
    moved = client.put("/api/admin/company-map", json={"companies": [{"id": companies["茅台"]["id"], "name": "茅台", "aliases": ["贵州茅台"],
        "industry_id": data["industries"][1]["id"]}], "industries": [{"id": data["industries"][0]["id"], "name": "酒水"}]}).json()
    companies = {item["name"]: item for item in moved["companies"]}
    assert (companies["茅台"]["industry_name"], companies["茅台"]["seq"]) == ("金融", 2)
    assert (companies["泸州老窖"]["industry_name"], companies["泸州老窖"]["seq"]) == ("酒水", 2)
    assert client.put("/api/admin/company-map", json={"companies": [{"name": "新企业", "aliases": ["贵州茅台"], "industry_name": "金融"}]}).status_code == 409
    assert client.put("/api/admin/company-map", json={"industries": [{"name": "金融"}]}).status_code == 409
    assert client.put("/api/admin/company-map", json={"companies": [{"name": "某企业", "industry_name": "不存在"}]}).status_code == 422
    assert client.put("/api/admin/company-map", json={"industries": [{"name": "坏/名"}]}).status_code == 422
    assert client.put("/api/admin/company-map", json={"companies": [{"name": "一", "industry_name": "金融"}]}).status_code == 422
    with client.app.state.database.session() as session:
        entries = list(session.scalars(select(Oplog).where(Oplog.action == "修改企业行业对照表")))
        assert len(entries) == 4 and entries[0].before == {"industries": [], "companies": []}
    tree = client.get("/api/catalog/tree").json()
    assert [root["folder"] for root in tree] == ["01已中标项目", "02未中标项目"]
    assert [(industry["folder"], [company["folder"] for company in industry["companies"]]) for industry in tree[0]["industries"]] == [
        ("01酒水", ["02泸州老窖"]), ("02金融", ["01招商银行", "02茅台"])]
    login(client, "one")
    assert client.get("/api/admin/company-map").status_code == 403
    assert client.put("/api/admin/company-map", json={}).status_code == 403
    assert client.get("/api/catalog/tree").status_code == 200


# ---- 主流程 ----
def test_full_flow_first_pass_final_conflict_then_another_final_passes(client: TestClient) -> None:
    add_company(client)
    batch_id = submitted(client)
    login(client, "both")
    queue = client.get("/api/review/queue", params={"stage": "first"}).json()
    assert [item["id"] for item in queue] == [batch_id]
    assert (queue[0]["round"], queue[0]["uploader_name"], queue[0]["products"], queue[0]["files"]) == (1, "one", 3, 4)
    assert queue[0]["total_size"] == 4 * len(jpeg()) and queue[0]["waiting_seconds"] >= 0 and queue[0]["claimed_by"] is None
    assert client.get("/api/review/queue", params={"stage": "final"}).json() == []
    first_pass(client, batch_id, "both")
    assert status(client, batch_id) == "待终审"
    assert client.get("/api/review/queue", params={"stage": "first"}).json() == []
    final_queue = client.get("/api/review/queue", params={"stage": "final"}).json()
    assert final_queue[0]["id"] == batch_id and final_queue[0]["blocked"] is True
    # 同一个人既是本轮初审，又想终审：拒绝。
    conflict = act(client, "both", batch_id, "claim")
    assert conflict.status_code == 403 and conflict.json()["code"] == "reviewer_conflict"
    assert act(client, "both", batch_id, "pass", {"checklist": ALL}).status_code == 403
    assert act(client, "final", batch_id, "claim").status_code == 200
    done = act(client, "final", batch_id, "pass", {"checklist": ALL})
    assert done.status_code == 200 and done.json()["review"]["batch"]["status"] == "入库中"
    with client.app.state.database.session() as session:
        batch = session.get(Batch, batch_id)
        users = {user.username: user.id for user in session.scalars(select(User))}
        assert (batch.status, batch.first_reviewer_id, batch.final_reviewer_id) == ("入库中", users["both"], users["final"])
        assert batch.final_path == "01已中标项目/01酒业/01企业"
        decisions = [(row.stage, row.decision) for row in session.scalars(select(ReviewRound).order_by(ReviewRound.id))]
        assert decisions == [("first", "pass"), ("final", "pass")]
        assert [entry.action for entry in session.scalars(select(Oplog).where(Oplog.action.in_(["初审通过", "终审通过"])))] == ["初审通过", "终审通过"]
        # 本包不执行入库：没有任何 ingest 任务。
        from app.db.models import Job
        assert not list(session.scalars(select(Job).where(Job.kind == "ingest")))
    assert act(client, "final", batch_id, "pass", {"checklist": ALL}).status_code == 409


def test_checklist_must_be_complete(client: TestClient) -> None:
    add_company(client)
    batch_id = submitted(client)
    assert act(client, "first", batch_id, "claim").status_code == 200
    for checklist in ({}, {**ALL, "tags": False}, {key: True for key in list(ALL)[:5]}, {**ALL, "extra": True}):
        response = act(client, "first", batch_id, "pass", {"checklist": checklist})
        assert response.status_code == 422 and response.json()["code"] == "checklist_incomplete"
    assert status(client, batch_id) == "待初审"
    assert act(client, "first", batch_id, "pass", {"checklist": ALL}).status_code == 200


def test_claim_rules_roles_and_admin_release(client: TestClient) -> None:
    batch_id = submitted(client)
    assert act(client, "one", batch_id, "claim").status_code == 403
    assert act(client, "final", batch_id, "claim").status_code == 403
    assert act(client, "first", batch_id, "pass", {"checklist": ALL}).json()["code"] == "not_claimed"
    assert act(client, "first", batch_id, "claim").json()["claimed_by_me"] is True
    assert act(client, "first", batch_id, "claim").status_code == 200
    taken = act(client, "first2", batch_id, "claim")
    assert taken.status_code == 409 and taken.json()["code"] == "already_claimed" and taken.json()["detail"]["claimed_by"] == "first"
    assert act(client, "first2", batch_id, "pass", {"checklist": ALL}).status_code == 403
    assert act(client, "first2", batch_id, "release").status_code == 403
    assert act(client, "admin", batch_id, "release").status_code == 200
    assert act(client, "admin", batch_id, "release").status_code == 409
    assert act(client, "admin", batch_id, "claim").status_code == 403
    assert act(client, "first2", batch_id, "claim").status_code == 200
    assert act(client, "first", 999, "claim").status_code == 404
    login(client, "one")
    assert client.get("/api/review/queue", params={"stage": "first"}).status_code == 403
    assert client.get(f"/api/review/{batch_id}").status_code == 403
    assert client.get("/api/review/queue", params={"stage": "x"}).status_code == 422
    login(client, "admin")
    assert client.get("/api/review/queue", params={"stage": "first"}).json()[0]["claimed_by"]["name"] == "first2"


def test_reviewer_can_review_own_upload(client: TestClient) -> None:
    add_company(client)
    with client.app.state.database.session() as session:
        session.scalar(select(User).where(User.username == "one")).roles = ["uploader", "first_reviewer"]
    batch_id = submitted(client)
    first_pass(client, batch_id, "one")
    assert status(client, batch_id) == "待终审"


def test_audit_page_data_and_thumbnail_access(client: TestClient) -> None:
    login(client, "one")
    batch_id = staged(client, ["单品/杯子.jpg", "单品/无法判断.jpg", "单品/调用失败.jpg", "套装/礼盒.jpg", "01定稿文件/终稿.ai", "05 详情页/a.jpg"])
    data = review(client, batch_id)
    products = named(data)
    client.patch(f"/api/batches/{batch_id}/products/{products['杯子']['id']}", json={"tags": {"material": "竹", "color": "红"}})
    client.patch(f"/api/batches/{batch_id}/products/{products['无法判断']['id']}", json={"confirm_fields": ["category"]})
    client.patch(f"/api/batches/{batch_id}/products/{products['调用失败']['id']}", json={"tags": {"category": "雨伞"}})
    client.post(f"/api/batches/{batch_id}/products", data={"kind": "item"}, files={"file": ("新增.jpg", jpeg(), "image/jpeg")})
    assert client.app.state.jobs.wait_idle(8)
    client.delete(f"/api/batches/{batch_id}/products/{products['杯子']['id']}")
    box = products["礼盒"]
    keep = [item["batch_product_id"] for item in box["relation_items"] if item["batch_product_id"] != products["杯子"]["id"]]
    added = named(review(client, batch_id))["新增"]["id"]
    assert client.put(f"/api/batches/{batch_id}/products/{box['id']}/relations", json={"item_ids": [*keep, added]}).status_code == 200
    assert client.post(f"/api/batches/{batch_id}/submit", json={"note": "备注内容"}).status_code == 200
    login(client, "first")
    page = client.get(f"/api/review/{batch_id}").json()
    assert page["stage"] == "first" and page["can_claim"] and not page["claimed_by_me"] and not page["can_edit_path"]
    assert page["review"]["batch"]["editable"] is False
    assert page["path"] == {"recommended": None, "final": None, "effective": None, "company_name": "企业", "company_in_tree": False}
    assert page["acks"] == ["缺少 02设计方案、03拍摄照片及视频、04授权文件 · 上传人已知晓"]
    assert page["stats"] == {"manual_edits": 1, "ai_confirmed": 1, "manual_filled": 1, "appended": 1, "removed": 1}
    assert page["relations"] == [{"set_id": box["id"], "set_name": "礼盒", "items": ["无法判断", "调用失败", "新增"], "ai_count": 2, "manual_count": 1, "confirmed": True}]
    assert page["note"] == "备注内容" and page["history"] == [] and len(page["checklist"]) == 6
    assert {item["name"]: item["count"] for item in page["folder_counts"]}["01定稿文件"] == 1
    file_id = page["review"]["products"][0]["batch_file_id"]
    assert client.get(f"/api/batches/{batch_id}/thumbs/{file_id}/300").status_code == 200
    login(client, "final")
    assert client.get(f"/api/review/{batch_id}").status_code == 403
    assert client.get(f"/api/batches/{batch_id}/thumbs/{file_id}/300").status_code == 403
    assert client.get("/api/review/999").status_code == 404


# ---- 驳回 ----
def test_first_reject_with_pins_then_resubmit_restarts_from_first(client: TestClient) -> None:
    add_company(client)
    batch_id = submitted(client)
    cup = named(review(client, batch_id))["杯子"]["id"]
    assert act(client, "first", batch_id, "claim").status_code == 200
    assert client.put(f"/api/review/{batch_id}/pins", json={"pins": [{"batch_product_id": 99999, "x": 0.5, "y": 0.5, "text": "x"}]}).status_code == 422
    assert client.put(f"/api/review/{batch_id}/pins", json={"pins": [{"batch_product_id": cup, "x": 1.5, "y": 0.5, "text": "x"}]}).status_code == 422
    saved = client.put(f"/api/review/{batch_id}/pins", json={"pins": [{"batch_product_id": cup, "x": 0.25, "y": 0.75, "text": " 这里有两个商品 "}]})
    assert saved.status_code == 200 and saved.json()["pins"] == [{"batch_product_id": cup, "product_name": "杯子", "x": 0.25, "y": 0.75, "text": "这里有两个商品"}]
    assert act(client, "first", batch_id, "reject", {"comment": "   "}).json()["code"] == "comment_required"
    rejected = act(client, "first", batch_id, "reject", {"comment": "标签不合理"})
    assert rejected.status_code == 200 and status(client, batch_id) == "已驳回"
    assert act(client, "first", batch_id, "reject", {"comment": "再来"}).status_code == 409
    login(client, "one")
    data = review(client, batch_id)
    assert data["batch"]["status"] == "已驳回" and data["batch"]["editable"]
    assert len(data["history"]) == 1
    entry = data["history"][0]
    assert (entry["round"], entry["stage"], entry["decision"], entry["comment"], entry["reviewer_name"]) == (1, "first", "reject", "标签不合理", "first")
    assert entry["pins"] == [{"batch_product_id": cup, "product_name": "杯子", "x": 0.25, "y": 0.75, "text": "这里有两个商品"}]
    assert client.patch(f"/api/batches/{batch_id}/products/{cup}", json={"tags": {"material": "改好了"}}).status_code == 200
    resubmitted = client.post(f"/api/batches/{batch_id}/submit", json={}).json()
    assert (resubmitted["batch"]["status"], resubmitted["batch"]["round"]) == ("待初审", 2)
    with client.app.state.database.session() as session:
        batch = session.get(Batch, batch_id)
        assert batch.first_reviewer_id is None and batch.final_reviewer_id is None
        entry = session.scalars(select(Oplog).where(Oplog.action == "初审驳回")).one()
        assert entry.after == {"status": "已驳回", "round": 1, "comment": "标签不合理", "pins": 1}
    login(client, "first")
    page = client.get(f"/api/review/{batch_id}").json()
    assert page["history"][0]["comment"] == "标签不合理" and page["pins"] == [] and page["can_claim"]
    assert client.get("/api/review/queue", params={"stage": "first"}).json()[0]["round"] == 2


def test_final_reject_clears_reviewers_and_previous_first_may_final_next_round(client: TestClient) -> None:
    add_company(client)
    batch_id = submitted(client)
    first_pass(client, batch_id, "both")
    assert act(client, "final", batch_id, "claim").status_code == 200
    assert act(client, "final", batch_id, "reject", {"comment": "位置不对"}).status_code == 200
    assert status(client, batch_id) == "已驳回"
    login(client, "one")
    history = review(client, batch_id)["history"]
    assert [(item["stage"], item["decision"]) for item in history] == [("final", "reject"), ("first", "pass")]
    assert client.post(f"/api/batches/{batch_id}/submit", json={}).json()["batch"]["round"] == 2
    assert status(client, batch_id) == "待初审"
    # 回避约束只针对同一轮：上一轮的初审人这一轮可以做终审。
    first_pass(client, batch_id, "first")
    assert act(client, "both", batch_id, "claim").status_code == 200
    assert act(client, "both", batch_id, "pass", {"checklist": ALL}).status_code == 200
    assert status(client, batch_id) == "入库中"


def test_pins_discarded_on_pass_and_when_claim_changes(client: TestClient) -> None:
    add_company(client)
    batch_id = submitted(client)
    cup = named(review(client, batch_id))["杯子"]["id"]
    pin = {"pins": [{"batch_product_id": cup, "x": 0.1, "y": 0.2, "text": "意见"}]}
    assert act(client, "first", batch_id, "claim").status_code == 200
    assert client.put(f"/api/review/{batch_id}/pins", json=pin).status_code == 200
    assert act(client, "admin", batch_id, "release").status_code == 200
    assert act(client, "first2", batch_id, "claim").json()["pins"] == []
    assert client.put(f"/api/review/{batch_id}/pins", json=pin).status_code == 200
    assert act(client, "first2", batch_id, "pass", {"checklist": ALL}).status_code == 200
    with client.app.state.database.session() as session:
        assert not list(session.scalars(select(ReviewPin)))
    login(client, "one")
    assert client.put(f"/api/review/{batch_id}/pins", json=pin).status_code == 403


# ---- 存放位置 ----
def test_only_final_reviewer_changes_path_and_log_keeps_before_after(client: TestClient) -> None:
    data = add_company(client)
    other = add_company(client, "招商银行", "金融")
    bank = next(item["id"] for item in other["companies"] if item["name"] == "招商银行")
    batch_id = submitted(client)
    assert act(client, "first", batch_id, "claim").json()["can_edit_path"] is False
    assert client.put(f"/api/review/{batch_id}/path", json={"won": False, "company_id": bank}).status_code == 403
    assert act(client, "first", batch_id, "pass", {"checklist": ALL}).status_code == 200
    login(client, "final")
    assert client.put(f"/api/review/{batch_id}/path", json={"won": False, "company_id": bank}).json()["code"] == "not_claimed"
    assert act(client, "final", batch_id, "claim").json()["can_edit_path"] is True
    assert client.put(f"/api/review/{batch_id}/path", json={"won": False, "company_id": 99999}).status_code == 422
    changed = client.put(f"/api/review/{batch_id}/path", json={"won": False, "company_id": bank})
    assert changed.status_code == 200
    assert changed.json()["path"]["effective"] == "02未中标项目/02金融/01招商银行" and changed.json()["path"]["recommended"] == "01已中标项目/01酒业/01企业"
    with client.app.state.database.session() as session:
        entry = session.scalars(select(Oplog).where(Oplog.action == "修改存放位置")).one()
        assert entry.before == {"path": "01已中标项目/01酒业/01企业", "won": True}
        assert entry.after == {"path": "02未中标项目/02金融/01招商银行", "won": False}
    assert act(client, "final", batch_id, "pass", {"checklist": ALL}).status_code == 200
    with client.app.state.database.session() as session:
        assert session.get(Batch, batch_id).final_path == "02未中标项目/02金融/01招商银行"
    assert data["companies"]


def test_pending_path_blocks_final_until_admin_adds_company(client: TestClient) -> None:
    batch_id = submitted(client)
    first_pass(client, batch_id)
    assert act(client, "final", batch_id, "claim").json()["path"]["company_in_tree"] is False
    blocked = act(client, "final", batch_id, "pass", {"checklist": ALL})
    assert blocked.status_code == 409 and blocked.json()["code"] == "path_pending"
    assert "等待管理员在对照表中录入该企业" in blocked.json()["message"] and status(client, batch_id) == "待终审"
    login(client, "admin")
    assert [item["key"] for item in client.get("/api/todos").json()] == ["path_pending"]
    add_company(client, "标准企业", "酒业", ["企业"])
    assert client.get("/api/todos").json() == []
    login(client, "final")
    page = client.get(f"/api/review/{batch_id}").json()
    assert page["path"]["effective"] == "01已中标项目/01酒业/01标准企业" and page["path"]["company_in_tree"]
    assert act(client, "final", batch_id, "pass", {"checklist": ALL}).status_code == 200
    assert status(client, batch_id) == "入库中"


# ---- 状态机 ----
STATES = ["上传中", "上传中断", "已暂存", "AI 处理中", "AI 处理失败", "待核对", "待初审", "待终审", "已驳回", "入库中", "入库异常", "已入库", "已撤回"]


@pytest.mark.parametrize("state", STATES)
def test_review_actions_are_rejected_outside_review_states(client: TestClient, state: str) -> None:
    add_company(client)
    batch_id = submitted(client)
    with client.app.state.database.session() as session:
        batch = session.get(Batch, batch_id)
        users = {user.username: user.id for user in session.scalars(select(User))}
        batch.status, batch.first_reviewer_id, batch.final_reviewer_id = state, users["first"], users["final"]
    cup = named(review(client, batch_id))["杯子"]["id"] if state != "已撤回" else 1
    expected = {"待初审": {"first": 200, "final": 403}, "待终审": {"first": 403, "final": 200}}.get(state, {"first": 409, "final": 409})
    for user in ("first", "final"):
        login(client, user)
        code = expected[user]
        assert client.post(f"/api/review/{batch_id}/claim").status_code == code
        assert client.put(f"/api/review/{batch_id}/pins", json={"pins": [{"batch_product_id": cup, "x": 0, "y": 0, "text": ""}]}).status_code == code
    # 通过与驳回各自验证迁移结果。
    transitions = {"待初审": ("first", "待终审"), "待终审": ("final", "入库中")}
    for action in ("pass", "reject"):
        with client.app.state.database.session() as session:
            session.get(Batch, batch_id).status = state
        for user in ("first", "final"):
            response = act(client, user, batch_id, action, {"checklist": ALL} if action == "pass" else {"comment": "意见"})
            allowed = state in transitions and transitions[state][0] == user
            assert response.status_code == (200 if allowed else expected[user]), (state, user, action)
            if allowed:
                assert status(client, batch_id) == (transitions[state][1] if action == "pass" else "已驳回")
                with client.app.state.database.session() as session:
                    row = session.scalar(select(ReviewRound).where(ReviewRound.batch_id == batch_id, ReviewRound.stage == user))
                    session.get(Batch, batch_id).status = state
                    row.decision = None
            else:
                assert status(client, batch_id) == state
    # 上传人的提交只在待核对与已驳回合法。
    with client.app.state.database.session() as session:
        session.get(Batch, batch_id).status = state
    login(client, "one")
    assert client.post(f"/api/batches/{batch_id}/submit", json={}).status_code == (200 if state in ("待核对", "已驳回") else 409)


def test_uploader_withdraws_rejected_batch_but_not_while_under_review(client: TestClient, settings: Settings) -> None:
    add_company(client)
    batch_id = submitted(client)
    login(client, "one")
    assert client.post(f"/api/batches/{batch_id}/cancel").status_code == 409
    assert act(client, "first", batch_id, "claim").status_code == 200
    assert act(client, "first", batch_id, "reject", {"comment": "不要了"}).status_code == 200
    login(client, "first")
    assert client.post(f"/api/batches/{batch_id}/cancel").status_code == 403
    login(client, "one")
    assert client.post(f"/api/batches/{batch_id}/cancel").json()["status"] == "已撤回"
    with client.app.state.database.session() as session:
        assert not (settings.paths.dev_nas_dir / "工作台临时区" / session.get(Batch, batch_id).batch_no).exists()


# ---- 待办 ----
def test_todos_follow_state_changes_per_role(client: TestClient) -> None:
    add_company(client)
    def todos(user: str) -> dict[str, int]:
        login(client, user)
        return {item["key"]: item["count"] for item in client.get("/api/todos").json()}
    login(client, "one")
    batch_id = staged(client, ["单品/杯子.jpg"])
    mine = client.get("/api/todos").json()
    assert [(item["key"], item["count"], item["link"]) for item in mine] == [("check", 1, f"/batches/{batch_id}/review")]
    assert todos("first") == {} and todos("final") == {}
    login(client, "one")
    assert client.post(f"/api/batches/{batch_id}/submit", json={}).status_code == 200
    assert todos("one") == {} and todos("first") == {"first_review": 1} and todos("both") == {"first_review": 1} and todos("final") == {}
    first_pass(client, batch_id)
    assert todos("first") == {} and todos("final") == {"final_review": 1} and todos("both") == {"final_review": 1}
    assert act(client, "final", batch_id, "claim").status_code == 200
    assert act(client, "final", batch_id, "reject", {"comment": "改"}).status_code == 200
    assert todos("final") == {} and todos("one") == {"rejected": 1} and todos("admin") == {}
    with client.app.state.database.session() as session:
        session.get(Batch, batch_id).status = "AI 处理失败"
    assert todos("one") == {"ai_failed": 1} and todos("admin") == {"ai_failed": 1} and todos("first") == {}
    with client.app.state.database.session() as session:
        batch = session.get(Batch, batch_id)
        batch.status = "待初审"
        batch.payload = {**batch.payload, "stage_since": (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()}
    assert todos("admin") == {"overdue": 1} and todos("first") == {"first_review": 1}
    client.cookies.clear()
    assert client.get("/api/todos").status_code == 401


def test_business_days_skip_weekends() -> None:
    local = timezone(timedelta(hours=8))
    friday = datetime(2026, 11, 13, 10, 0, tzinfo=local)
    assert business_days_elapsed(friday, datetime(2026, 11, 16, 9, 0, tzinfo=local)) == 0
    assert business_days_elapsed(friday, datetime(2026, 11, 16, 10, 0, tzinfo=local)) == 1
    assert business_days_elapsed(friday, datetime(2026, 11, 18, 10, 0, tzinfo=local)) == 3
    assert business_days_elapsed(friday, datetime(2026, 11, 15, 23, 0, tzinfo=local)) == 0
