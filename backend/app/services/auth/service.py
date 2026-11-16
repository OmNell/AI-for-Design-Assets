"""工作台认证；令牌仅存哈希，NAS 凭据只交给本人会话池。"""
from __future__ import annotations

import hashlib
import secrets
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.config import Settings
from app.db.database import Database
from app.db.models import User, UserSession, utcnow
from app.errors import AppError
from app.services.auth.backends import AuthBackend, AuthUnavailable
from app.services.nas import NasPermissionDenied, NasSessionExpired, NasUnavailable, SessionPool
from app.services.oplog import OplogService

COOKIE_NAME = "nas_workbench_session"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_expired(value: datetime) -> bool:
    return value.replace(tzinfo=timezone.utc) <= utcnow()


class AuthService:
    def __init__(self, database: Database, pool: SessionPool, backend: AuthBackend, config: Settings) -> None:
        self.database = database
        self.pool = pool
        self.backend = backend
        self.config = config
        self.lock = threading.RLock()

    def _failed_login(self, username: str, host: str) -> None:
        with self.database.session() as session:
            user = session.scalar(select(User).where(User.username == username))
            OplogService(session).write(user.id if user else None, "anonymous", "登录", "user",
                                       str(user.id) if user else None, None, None, "失败", host)

    def login(self, username: str, password: str, host: str) -> tuple[User, str, datetime]:
        with self.lock:
            try:
                verified = self.backend.verify(username, password)
            except AuthUnavailable:
                self._failed_login(username, host)
                raise AppError("auth_unavailable", "身份验证服务暂时不可用", 503) from None
            if not verified:
                self._failed_login(username, host)
                raise AppError("invalid_credentials", "账号或密码不正确", 401)
            with self.database.session() as session:
                existing = session.scalar(select(User).where(User.username == username))
                disabled = existing is not None and not existing.enabled
            if disabled:
                self._failed_login(username, host)
                raise AppError("user_disabled", "账号已停用，请联系管理员", 403)
            opened = False
            user_id: int | None = None
            try:
                with self.database.session() as session:
                    user = session.scalar(select(User).where(User.username == username))
                    if user is None:
                        roles = ["uploader"]
                        if username == self.config.bootstrap_admin:
                            roles.append("admin")
                        user = User(username=username, roles=roles, enabled=True)
                        session.add(user)
                        session.flush()
                    user_id = user.id
                    self.pool.open(user.id, username, password)
                    opened = True
                    token = secrets.token_urlsafe(32)
                    expires = utcnow() + timedelta(hours=self.config.server.session_hours)
                    session.add(UserSession(token_hash=token_hash(token), user_id=user.id,
                                            client_host=host, expires_at=expires))
                    user.last_login_at = utcnow()
                    OplogService(session).write(user.id, ",".join(user.roles), "登录", "user", str(user.id),
                                               None, None, "成功", host)
                return user, token, expires
            except (NasPermissionDenied, NasUnavailable) as error:
                self._failed_login(username, host)
                code = "nas_permission_denied" if isinstance(error, NasPermissionDenied) else "nas_unavailable"
                raise AppError(code, "NAS 无权限" if isinstance(error, NasPermissionDenied) else "NAS 连接暂时不可用",
                               403 if isinstance(error, NasPermissionDenied) else 503) from None
            except Exception:
                if opened and user_id is not None:
                    self.pool.close(user_id)
                raise
            finally:
                password = ""

    def _close_if_unused(self, user_id: int) -> None:
        with self.database.session() as session:
            active = session.scalar(select(UserSession.id).where(
                UserSession.user_id == user_id, UserSession.expires_at > utcnow()).limit(1))
        if active is None:
            self.pool.close(user_id)

    def current(self, token: str | None, host: str) -> User:
        if token is None or len(token) != 43:
            raise AppError("login_required", "请先登录", 401)
        with self.lock:
            failure: AppError | None = None
            with self.database.session() as session:
                login = session.scalar(select(UserSession).where(UserSession.token_hash == token_hash(token)))
                if login is None:
                    raise AppError("login_required", "请先登录", 401)
                user = session.get(User, login.user_id)
                if user is None or not user.enabled:
                    session.execute(delete(UserSession).where(UserSession.user_id == login.user_id))
                    failure = AppError("login_required", "账号已停用，请重新登录", 401)
                elif is_expired(login.expires_at):
                    session.delete(login)
                    failure = AppError("session_expired", "登录已过期，请重新登录", 401)
                else:
                    try:
                        self.pool.get(user.id)
                    except NasSessionExpired:
                        session.execute(delete(UserSession).where(UserSession.user_id == user.id))
                        failure = AppError("nas_session_expired", "NAS 连接已超时，请重新登录", 401)
                if failure is not None:
                    OplogService(session).write(user.id if user else None, ",".join(user.roles) if user else "system",
                                               "会话失效", "user", str(login.user_id), None, None, "成功", host)
            if failure is not None:
                self._close_if_unused(login.user_id)
                raise failure
            return user

    def logout(self, token: str, host: str) -> None:
        with self.lock:
            with self.database.session() as session:
                login = session.scalar(select(UserSession).where(UserSession.token_hash == token_hash(token)))
                if login is None:
                    raise AppError("login_required", "请先登录", 401)
                user = session.get(User, login.user_id)
                user_id = login.user_id
                session.delete(login)
                OplogService(session).write(user_id, ",".join(user.roles), "退出登录", "user", str(user_id),
                                           None, None, "成功", host)
            self._close_if_unused(user_id)
