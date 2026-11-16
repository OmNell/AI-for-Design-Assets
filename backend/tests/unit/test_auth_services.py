from __future__ import annotations


import pytest
from sqlalchemy import select

from app.config import Settings
from app.db.database import Database
from app.db.models import User, UserSession
from app.errors import AppError
from app.schemas.auth import Role, RoleUpdate
from app.services.admin import RoleService
from app.services.auth.backends import AuthUnavailable, FakeAuthBackend
from app.services.auth.service import AuthService
from app.services.nas.factory import create_session_pool


def test_auth_service_unavailable_does_not_issue_token(database: Database, settings: Settings) -> None:
    class OfflineBackend:
        def verify(self, username: str, password: str) -> bool:
            raise AuthUnavailable("密码不应被传播")
    with create_session_pool(settings) as pool:
        service = AuthService(database, pool, OfflineBackend(), settings)
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
        service = RoleService(AuthService(database, pool, FakeAuthBackend([]), settings))
        with pytest.raises(AppError) as error:
            service.update(actor, RoleUpdate(user_id=target.id, roles=[Role.ADMIN], enabled=True), "本机")
        assert error.value.status_code == 403
        with database.session() as session:
            assert session.get(User, target.id).roles == ["uploader"]
