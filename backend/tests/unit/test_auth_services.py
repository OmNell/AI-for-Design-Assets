from __future__ import annotations


import pytest
from sqlalchemy import select

from app.config import Settings
from app.db.database import Database
from app.db.models import User, UserSession
from app.errors import AppError
from app.schemas.auth import Role, RoleUpdate
from app.services.admin import RoleService
from app.services.auth.service import AuthService
from app.services.nas.errors import NasUnavailable
from app.services.nas.factory import create_session_pool
from app.services.nas.local import LocalFs
from app.services.nas.pool import SessionPool


def test_auth_service_unavailable_does_not_issue_token(database: Database, settings: Settings,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(username: str, password: str) -> None:
        raise NasUnavailable("密码不应被传播")

    with create_session_pool(settings) as pool:
        monkeypatch.setattr(pool, "connect", unavailable)
        service = AuthService(database, pool, settings)
        with pytest.raises(AppError) as error:
            service.login("demo", "fake-password", "本机")
        assert error.value.status_code == 503
        with database.session() as session:
            assert session.scalar(select(UserSession)) is None


def test_role_service_checks_actor_inside_transaction(database: Database, settings: Settings) -> None:
    with database.session() as session:
        actor = User(username="actor", roles=["uploader"])
        target = User(username="target", roles=["uploader"])
        session.add_all([actor, target]); session.flush()
    with create_session_pool(settings) as pool:
        service = RoleService(AuthService(database, pool, settings))
        with pytest.raises(AppError) as error:
            service.update(actor, RoleUpdate(user_id=target.id, roles=[Role.ADMIN], enabled=True), "本机")
        assert error.value.status_code == 403
        with database.session() as session:
            assert session.get(User, target.id).roles == ["uploader"]


@pytest.mark.parametrize("alias", ["ALICE", r"NAS\ALICE", "Alice@nas"])
def test_smb_alias_cannot_bypass_local_disable_or_bootstrap(
        database: Database, settings: Settings, alias: str) -> None:
    settings.mode = "prod"
    settings.bootstrap_admin = "Alice"
    opened = []

    def connect(username: str, password: str) -> LocalFs:
        assert password == "fake-password"
        fs = LocalFs(settings.paths.dev_nas_dir, {})
        opened.append(fs)
        return fs

    with SessionPool(connect, lambda: pytest.fail("不得切换到系统账号")) as pool:
        auth = AuthService(database, pool, settings)
        with pytest.raises(AppError) as rejected:
            auth.login(alias, "fake-password", "设备甲")
        assert rejected.value.code == "nas_account_name_conflict"
        assert opened[-1]._closed
        with database.session() as session:
            assert session.scalar(select(User)) is None
            assert session.scalar(select(UserSession)) is None
        user, first_token, _ = auth.login("Alice", "fake-password", "设备甲")
        _, second_token, _ = auth.login("Alice", "fake-password", "设备乙")
        assert user.roles == ["uploader", "admin"]
        assert pool.get(auth.context(first_token, "设备甲").session_id) is opened[1]
        assert pool.get(auth.context(second_token, "设备乙").session_id) is opened[2]
        auth.logout(first_token, "设备甲")
        assert opened[1]._closed and not opened[2]._closed
        auth.logout(second_token, "设备乙")
        with database.session() as session:
            session.get(User, user.id).enabled = False
        with pytest.raises(AppError) as disabled:
            auth.login(alias, "fake-password", "设备甲")
        assert disabled.value.code == "user_disabled"
        assert opened[-1]._closed
        with database.session() as session:
            assert list(session.scalars(select(User.username))) == ["Alice"]
