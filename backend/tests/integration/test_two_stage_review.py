"""使用真实 WP07 提交入口和 dev 模式贯穿队列、回避、驳回与执行交接。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.config import DevUser, Settings
from app.db.models import Batch, BatchProduct, Job, ReviewRound, ReviewSnapshot, UserSession, utcnow
from app.main import create_app
from app.schemas.audit import CHECKLIST
from app.services.auth.service import COOKIE_NAME
from test_ai_pipeline import login
from test_review import ready, confirm_fields

ALL = {"checklist": dict.fromkeys(CHECKLIST, True)}
CATALOG = {"industries":[{"name":"酒业"}], "companies":[{"name":"酒企","industry_name":"酒业","aliases":["企业"]}]}


class Flow:
    def __init__(self, client: TestClient, paths: list[str] | None = None):
        self.client = client
        self.cookies = {}
        self.users = {}
        for name in ("one", "first", "final", "admin", "other"):
            self.sign_in(name)
        for name in ("one", "first", "final", "admin"):
            self.roles(name, ["uploader", "first_reviewer", "final_reviewer"] + (["admin"] if name == "admin" else []))
        self.sign_in("one")
        self.batch = ready(client, paths or ["单品/杯.jpg"])
        self.bid = int(self.batch.split("/")[-1])
        self.base = f"/api/review/{self.bid}"
        self.products = client.get(self.batch+"/products").json()["products"]
        self.product = self.products[0]
        for product in self.products:
            confirm_fields(client, self.batch, product)
            if product["kind"] == "set":
                items = [item["id"] for item in self.products if item["kind"] == "item"]
                assert client.put(self.batch + f"/products/{product['id']}/relations",
                                  json={"item_ids": items, "confirm_kind": "confirmed_members"}).status_code == 200
        assert self.call("one", "POST", self.batch+"/submit", {}).status_code == 200

    def sign_in(self, name):
        login(self.client, name)
        self.cookies[name] = self.client.cookies[COOKIE_NAME]
        self.users[name] = self.client.get("/api/auth/me").json()["id"]

    def call(self, name, method, path, body=None, version=None):
        headers = {"Cookie": f"{COOKIE_NAME}={self.cookies[name]}"}
        if version is not None:
            headers["X-Review-Version"] = version
        return self.client.request(method, path, headers=headers, json=body)

    def roles(self, name, roles):
        result = self.call("admin", "PUT", "/api/admin/roles", {"user_id":self.users[name], "roles":roles,"enabled":True})
        assert result.status_code == 200, result.text

    def audit(self, name, action, body=None, version=None):
        if version is None:
            version = self.call(name, "GET", self.base).json()["version"]
        return self.call(name, "PUT" if action in ("pins","path") else "POST", self.base+"/"+action, body, version)

    def todos(self, name):
        return {r["key"]:r["count"] for r in self.call(name, "GET", "/api/todos").json()}


def configure(settings: Settings, tmp_path: Path) -> None:
    settings.paths.dev_nas_dir = tmp_path / "nas"
    settings.bootstrap_admin = "admin"
    settings.dev_users = [DevUser(username=n,password="fake",permissions={"temp":"write"}) for n in ("one","first","final","admin","other")]


@pytest.fixture
def flow(settings: Settings, tmp_path: Path):
    configure(settings, tmp_path)
    with TestClient(create_app(settings,log_dir=tmp_path/"logs")) as client:
        yield Flow(client)


def test_approval_snapshot_catalog_and_identity(flow):
    f = flow
    assert f.todos("first")["first_review"] == 1 and "first_review" not in f.todos("one")
    assert f.audit("one","claim").status_code == 403
    f.roles("admin", ["admin"])
    assert f.audit("admin", "claim").status_code == 403
    assert f.call("other","GET",f.base).status_code == 403
    assert f.call("other","GET","/api/review/queue?stage=first").status_code == 403
    assert f.call("other","PUT","/api/admin/company-map",CATALOG).status_code == 403
    first = f.audit("first","claim").json()
    assert first["claimed_by_me"] and not first["review"]["batch"]["editable"]
    assert f.audit("first","pass",{"checklist":{"path":True}}).status_code == 422
    assert f.audit("first","path",{"won":True,"company_id":1}).status_code == 403
    assert f.call("first","PATCH",f.batch+f"/products/{f.product['id']}",{"free_text":"越权"}).status_code == 403
    assert f.call("one","PATCH",f.batch+f"/products/{f.product['id']}",{"free_text":"锁定"}).status_code == 409
    with f.client.app.state.database.session() as s:
        s.get(BatchProduct,f.product["id"]).free_text = "模拟草稿偏移"
    assert f.audit("first","pass",ALL).json()["review"]["products"][0]["free_text"] == f.product["free_text"]
    assert f.audit("first","claim").status_code == 403
    assert "final_review" not in f.todos("first") and f.todos("final")["final_review"] == 1
    assert f.audit("final","claim",version=first["version"]).status_code == 409
    final = f.audit("final","claim").json()
    assert f.audit("final","pass",ALL).json()["code"] == "path_pending"
    assert f.todos("admin")["path_pending"] == 1
    company = f.call("admin","PUT","/api/admin/company-map",CATALOG).json()["companies"][0]
    assert f.audit("final","pass",ALL,final["version"]).status_code == 409
    selected = f.audit("final","path",{"won":False,"company_id":company["id"]}).json()
    assert selected["review"]["batch"]["won"] is True and selected["path"]["final"].startswith("02")
    assert f.call("admin","PUT","/api/admin/company-map",{"companies":[{**company,"name":"酒厂"}]}).status_code == 200
    assert f.audit("final","pass",ALL,selected["version"]).status_code == 409
    assert f.audit("final","pass",ALL).json()["code"] == "path_pending"
    f.audit("final","path",{"won":False,"company_id":company["id"]})
    done = f.audit("final","pass",ALL)
    assert done.status_code == 200, done.text
    assert done.json()["review"]["batch"]["status"] == "入库中"
    assert done.json()["review"]["batch"]["review_snapshot_hash"] == first["review"]["batch"]["review_snapshot_hash"]
    accepted_path = done.json()["path"]["effective"]
    assert f.call("admin", "PUT", "/api/admin/company-map", {"companies": [{**company, "name": "新酒厂"}]}).status_code == 200
    assert f.call("final", "GET", f.base).json()["path"]["effective"] == accepted_path
    assert f.audit("final","pass",ALL).status_code == 409
    with f.client.app.state.database.session() as s:
        rows = list(s.scalars(select(ReviewRound).order_by(ReviewRound.id)))
        assert rows[0].snapshot_id == rows[1].snapshot_id
        assert rows[1].reviewer_id == s.get(UserSession,rows[1].execution_session_id).user_id == f.users["final"]
        assert rows[1].path_change["after"]["path"] == s.get(Batch,f.bid).final_path
        job = s.scalar(select(Job).where(Job.kind == "ingest"))
        assert job.run_as_user_id == f.users["final"] and job.run_as_session_id == rows[1].execution_session_id
    config = f.client.app.state.auth.config
    assert list(config.paths.dev_nas_dir.rglob("杯.jpg"))[0].is_file()
    assert not list((config.paths.dev_nas_dir / config.nas.root_path.strip("/")).rglob("*"))


@pytest.mark.parametrize("reject_stage",["first","final"])
def test_reject_resubmit_swap_and_prior_submitters(flow,reject_stage):
    f = flow
    f.audit("first","claim")
    reviewer = "first"
    if reject_stage=="final":
        f.audit("first","pass",ALL)
        f.audit("final","claim")
        reviewer="final"
    pins = {"pins":[{"batch_product_id":f.product["id"],"x":0.2,"y":0.3,"text":"请核对瓶盖"}]}
    assert f.audit(reviewer,"pins",pins).status_code == 200
    assert f.audit(reviewer,"reject",{"comment":" "}).status_code == 422
    assert f.audit(reviewer,"reject",{"comment":"请调整"}).status_code == 200
    page = f.call("one","GET",f.batch+"/products").json()
    assert page["history"][0]["comment"] == "请调整" and page["history"][0]["pins"][0]["text"]=="请核对瓶盖"
    assert f.todos("one")["rejected"] == 1
    for sql in ["UPDATE review_rounds SET comment='覆盖' WHERE decision IS NOT NULL","DELETE FROM review_pins","UPDATE review_pins SET text='篡改'"]:
        with pytest.raises(IntegrityError),f.client.app.state.database.session() as s:
            s.execute(text(sql))
    old_version = f.call(reviewer, "GET", f.base).json()["version"]
    assert f.call("admin", "PUT", f"/api/admin/batches/{f.bid}/owner", {"user_id": f.users[reviewer]}).status_code == 409
    assert f.call("admin","PUT",f"/api/admin/batches/{f.bid}/owner",{"user_id":f.users["admin"]}).status_code == 200
    second = f.call("admin","POST",f.batch+"/submit",{}).json()["batch"]
    assert second["status"]=="待初审" and second["round"]==2
    assert f.audit("final", "claim", version=old_version).status_code == 409
    assert f.audit("one","claim").status_code==403 and f.audit("admin","claim").status_code==403
    assert f.audit("final","claim").status_code==200 and f.audit("final","pass",ALL).status_code==200
    assert f.audit("first","claim").status_code==200
    assert f.call("admin","PUT","/api/admin/company-map",CATALOG).status_code==200
    assert f.audit("first","pass",ALL).status_code==200


def test_concurrent_claim_role_revocation_release_and_termination(flow):
    f=flow
    version=f.call("first","GET",f.base).json()["version"]
    with ThreadPoolExecutor(max_workers=2) as ex:
        results=list(ex.map(lambda n:f.audit(n,"claim",version=version).status_code, ["first","final"]))
    assert sorted(results)==[200,409]
    winner=("first","final")[results.index(200)]
    ver=f.call(winner,"GET",f.base).json()["version"]
    f.roles(winner,["uploader"])
    assert f.audit(winner,"pass",ALL,ver).status_code==403
    assert f.audit("admin","release").status_code==200
    f.roles(winner,["first_reviewer","final_reviewer"])
    f.audit("first","claim")
    assert f.audit("first","pass",ALL,ver).status_code==409
    f.audit("first","pass",ALL)
    f.audit("final","claim")
    f.call("admin","PUT","/api/admin/company-map",CATALOG)
    ver=f.call("final","GET",f.base).json()["version"]
    with ThreadPoolExecutor(max_workers=2) as ex:
        tasks=[ex.submit(f.audit,"final","pass",ALL,ver),ex.submit(f.call,"one","POST",f.batch+"/cancel",{"reason":"终止测试"})]
    assert sorted(t.result().status_code for t in tasks)==[200,409]
    with f.client.app.state.database.session() as s:
        batch=s.get(Batch,f.bid)
        assert (batch.status,batch.lock_active) in [("已终止",False),("入库中",True),("入库异常",True)]
        assert s.scalar(select(ReviewSnapshot.id)) is not None
    assert f.audit("admin","release",version=ver).status_code==409


def test_catalog_conflicts_sequences_and_queue_time(flow):
    f=flow
    result=f.call("admin","PUT","/api/admin/company-map",CATALOG).json()
    second={"name":"印业"}
    assert f.call("admin","PUT","/api/admin/company-map",{"industries":[second]}).status_code==200
    assert f.call("admin","PUT","/api/admin/company-map",{"companies":[{"name":"另一","aliases":["企业"],"industry_name":"印业"}]}).status_code==409
    assert f.call("admin","PUT","/api/admin/company-map",{"industries":[second]}).status_code==409
    assert f.call("admin","PUT","/api/admin/company-map",{"industries":[{"name":"A/B"}]}).status_code==422
    company=result["companies"][0]
    f.call("admin","PUT","/api/admin/company-map",{"companies":[{"id":company["id"],"name":"酒企","industry_name":"印业"}]})
    new=f.call("admin","PUT","/api/admin/company-map",{"companies":[{"name":"另一","industry_name":"酒业"}]}).json()
    assert next(c["seq"] for c in new["companies"] if c["name"]=="另一")==2
    assert len(f.call("other","GET","/api/catalog/tree").json())==2
    with f.client.app.state.database.session() as s:
        batch=s.get(Batch,f.bid)
        batch.payload={**batch.payload,"stage_since":(utcnow()-timedelta(days=8)).isoformat()}
    assert f.todos("admin")["overdue"]==1


def test_restart_preserves_claim_snapshot_and_draft(settings: Settings, tmp_path: Path):
    configure(settings, tmp_path)
    with TestClient(create_app(settings, log_dir=tmp_path / "logs")) as before:
        flow = Flow(before)
        claim = flow.audit("first", "claim").json()
        pin = {"batch_product_id": flow.product["id"], "x": 0.2, "y": 0.4, "text": "草稿"}
        assert flow.audit("first", "pins", {"pins": [pin]}).status_code == 200
    with TestClient(create_app(settings, log_dir=tmp_path / "logs")) as after:
        login(after, "first")
        restored = after.get(flow.base).json()
        assert restored["version"] == claim["version"] and restored["claimed_by_me"]
        assert restored["pins"][0]["text"] == "草稿"
        headers = {"X-Review-Version": restored["version"]}
        body = {"comment": "核实产品", "pins": [{**pin, "text": "最终意见"}]}
        result = after.post(flow.base + "/reject", headers=headers, json=body)
        assert result.status_code == 200, result.text
        assert result.json()["history"][0]["pins"][0]["text"] == "最终意见"
        assert after.post(flow.base + "/reject", headers=headers, json=body).status_code == 409
        with after.app.state.database.session() as session:
            assert len(list(session.scalars(select(ReviewRound)))) == 1


def test_admin_reads_and_failed_decision_rolls_back(flow, monkeypatch):
    f = flow
    for path in ("/api/admin/company-map", "/api/catalog/tree", "/api/todos", "/api/review/queue?stage=first"):
        assert f.call("admin", "GET", path).status_code == 200
        assert f.client.get(path, headers={"cookie": ""}).status_code == 401
    assert f.call("other", "GET", "/api/admin/company-map").status_code == 403
    assert f.audit("first", "claim").status_code == 200
    assert f.audit("first", "pins", {"pins": [{"batch_product_id": 98765, "x": 0.5, "y": 0.5}]}).status_code == 422
    version = f.call("first", "GET", f.base).json()["version"]

    def interrupted(*args):
        raise RuntimeError("模拟提交审核事务中断")

    with monkeypatch.context() as patch:
        patch.setattr(f.client.app.state.notify, "emit", interrupted)
        with pytest.raises(RuntimeError, match="模拟提交审核事务中断"):
            f.audit("first", "pass", ALL, version)
    restored = f.call("first", "GET", f.base).json()
    assert restored["version"] == version and restored["history"] == [] and restored["claimed_by_me"]
    assert f.audit("first", "pass", ALL, version).status_code == 200
    assert f.audit("first", "pass", ALL, version).status_code in (403, 409)
