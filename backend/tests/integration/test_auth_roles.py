from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Iterator

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select, update

from app.api.dependencies import require_role
from app.config import DevUser, Settings
from app.db.models import Oplog, User, UserSession, utcnow
from app.main import create_app
from app.schemas.auth import Role
from app.services.auth.service import COOKIE_NAME, token_hash
from app.services.nas import NasPermissionDenied, NasSessionExpired


@pytest.fixture
def client(settings: Settings, tmp_path: Path) -> Iterator[TestClient]:
    settings.bootstrap_admin = "admin-demo"
    settings.dev_users = [DevUser(username=name, password=SecretStr("fake-password"), permissions={"temp": "write"})
                          for name in ("admin-demo", "member-demo")]
    application = create_app(settings, log_dir=tmp_path / "logs")
    with TestClient(application) as value:
        yield value


def login(client: TestClient, username: str = "member-demo") -> dict[str, object]:
    response = client.post("/api/auth/login", json={"username": username, "password": "fake-password"})
    assert response.status_code == 200
    return response.json()


def test_login_success_hash_cookie_me_and_logout(client: TestClient) -> None:
    response = client.post("/api/auth/login", json={"username": "member-demo", "password": "fake-password"})
    assert response.status_code == 200 and response.json()["roles"] == ["uploader"]
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Max-Age=28800" in cookie
    token = client.cookies[COOKIE_NAME]
    assert len(token) == 43 and token not in response.text
    with client.app.state.database.session() as session:
        saved = session.scalar(select(UserSession))
        assert saved.token_hash == token_hash(token) and saved.token_hash != token
        assert session.scalar(select(Oplog)).action == "登录"
    assert client.get("/api/auth/me").json()["username"] == "member-demo"
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    assert client.post("/api/auth/logout").status_code == 401
    with client.app.state.database.session() as session:
        assert session.scalar(select(UserSession)).revoked_at is not None
        assert [entry.action for entry in session.scalars(select(Oplog))] == ["登录", "退出登录"]


def test_wrong_password_and_validation_do_not_disclose_secrets(client: TestClient) -> None:
    for name in ("member-demo", "unknown-demo"):
        response = client.post("/api/auth/login", json={"username": name, "password": "not-the-password"})
        assert response.status_code == 401 and response.json()["code"] == "invalid_credentials"
        assert "not-the-password" not in response.text
    assert client.post("/api/auth/login", json={"username": "x", "password": ""}).status_code == 422
    with client.app.state.database.session() as session:
        assert session.scalar(select(User)) is None
        assert len(list(session.scalars(select(Oplog)))) == 2


def test_bootstrap_only_first_login_and_role_check(client: TestClient) -> None:
    member = login(client)
    assert client.get("/api/admin/roles").status_code == 403
    assert client.put("/api/admin/roles", json={"user_id": member["id"], "roles": ["admin"], "enabled": True}).status_code == 403
    client.post("/api/auth/logout")
    administrator = login(client, "admin-demo")
    assert set(administrator["roles"]) == {"admin", "uploader"}
    users = client.get("/api/admin/roles")
    assert users.status_code == 200 and len(users.json()) == 2
    changed = client.put("/api/admin/roles", json={"user_id": member["id"], "roles": ["first_reviewer"], "enabled": True})
    assert changed.status_code == 200 and changed.json()["roles"] == ["first_reviewer"]
    with client.app.state.database.session() as session:
        entry = session.scalar(select(Oplog).where(Oplog.action == "修改角色"))
        assert entry.before["roles"] == ["uploader"] and entry.after["roles"] == ["first_reviewer"]
    client.post("/api/auth/logout")
    assert login(client)["roles"] == ["first_reviewer"]
    client.post("/api/auth/logout")
    login(client, "admin-demo")
    client.put("/api/admin/roles", json={"user_id": administrator["id"], "roles": ["uploader"], "enabled": True})
    client.post("/api/auth/logout")
    assert login(client, "admin-demo")["roles"] == ["uploader"]
    assert client.get("/api/admin/roles").status_code == 403


def test_role_validation_and_missing_user(client: TestClient) -> None:
    login(client, "admin-demo")
    for roles in (["not-a-role"], ["admin", "admin"]):
        assert client.put("/api/admin/roles", json={"user_id": 1, "roles": roles, "enabled": True}).status_code == 422
    assert client.put("/api/admin/roles", json={"user_id": 9999, "roles": [], "enabled": True}).status_code == 404
    client.post("/api/auth/logout")
    assert client.get("/api/admin/roles").status_code == 401


def test_expired_token_cleared_and_rejected(client: TestClient) -> None:
    login(client)
    with client.app.state.database.session() as session:
        session.execute(update(UserSession).values(expires_at=utcnow() - timedelta(seconds=1)))
    response = client.get("/api/auth/me")
    assert response.status_code == 401 and response.json()["code"] == "session_expired"
    assert COOKIE_NAME not in client.cookies


def test_nas_expiry_invalidates_old_tokens_even_after_new_login(client: TestClient) -> None:
    login(client)
    old_token = client.cookies[COOKIE_NAME]
    first = client.app.state.auth.context(old_token, "设备甲")
    client.app.state.auth.pool.close(first.session_id)
    response = client.get("/api/auth/me")
    assert response.status_code == 401 and response.json()["code"] == "nas_session_expired"
    assert response.json()["message"] == "NAS 连接已超时，请重新登录"
    login(client)
    client.cookies.set(COOKIE_NAME, old_token, domain="testserver.local", path="/")
    assert client.get("/api/auth/me").status_code == 401


def test_disabling_user_revokes_all_tokens(client: TestClient) -> None:
    member = login(client)
    old_token = client.cookies[COOKIE_NAME]
    client.cookies.clear()
    login(client, "admin-demo")
    assert client.put("/api/admin/roles", json={"user_id": member["id"], "roles": ["uploader"], "enabled": False}).status_code == 200
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, old_token)
    assert client.get("/api/auth/me").status_code == 401
    assert client.post("/api/auth/login", json={"username": "member-demo", "password": "fake-password"}).status_code == 403


def test_multiple_browser_tokens_have_independent_nas_sessions(client: TestClient) -> None:
    login(client)
    token_a = client.cookies[COOKIE_NAME]
    auth = client.app.state.auth
    a = auth.context(token_a, "设备甲")
    fs_a = auth.pool.get(a.session_id)
    login(client)
    token_b = client.cookies[COOKIE_NAME]
    b = auth.context(token_b, "设备乙")
    fs_b = auth.pool.get(b.session_id)
    assert token_a != token_b and a.user.id == b.user.id and a.session_id != b.session_id
    assert fs_a is not fs_b
    assert client.post("/api/auth/logout").status_code == 200
    with pytest.raises(NasSessionExpired):
        auth.pool.get(b.session_id)
    assert auth.pool.get(a.session_id) is fs_a
    client.cookies.set(COOKIE_NAME, token_a, domain="testserver.local", path="/")
    assert client.get("/api/auth/me").status_code == 200
    assert client.post("/api/auth/logout").status_code == 200
    with pytest.raises(NasSessionExpired):
        auth.pool.get(a.session_id)


def test_nas_open_failure_no_user_or_token_persisted(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(*args: object) -> None:
        raise NasPermissionDenied("fake-password")
    monkeypatch.setattr(client.app.state.auth.pool, "open", refused)
    response = client.post("/api/auth/login", json={"username": "member-demo", "password": "fake-password"})
    assert response.status_code == 403 and "fake-password" not in response.text
    with client.app.state.database.session() as session:
        assert session.scalar(select(User)) is None
        assert session.scalar(select(UserSession)) is None


def test_reviewer_inherits_uploader_but_admin_does_not_inherit_review(client: TestClient) -> None:
    application = client.app
    @application.get("/api/test-upload-role")
    def upload_role(user=Depends(require_role(Role.UPLOADER))) -> dict[str, bool]:
        return {"ok": True}
    application.router.routes.insert(0, application.router.routes.pop())
    member = login(client)
    with application.state.database.session() as session:
        session.get(User, member["id"]).roles = ["first_reviewer"]
    assert client.get("/api/test-upload-role").status_code == 200
    with application.state.database.session() as session:
        session.get(User, member["id"]).roles = ["admin"]
    assert client.get("/api/test-upload-role").status_code == 403


def test_cross_site_writes_blocked_and_same_origin_login_works(client: TestClient) -> None:
    credentials = {"username": "member-demo", "password": "fake-password"}
    assert client.post("/api/auth/login", headers={"Origin": "http://other.test"}, json=credentials).status_code == 403
    assert client.post("/api/auth/login", headers={"Origin": "http://testserver"}, json=credentials).status_code == 200
    assert client.post("/api/auth/logout", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/api/auth/me").status_code == 200
