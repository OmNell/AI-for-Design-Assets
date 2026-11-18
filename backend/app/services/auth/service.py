"""工作台认证；令牌仅存哈希，NAS 凭据只交给本人会话池。"""
from __future__ import annotations

import hashlib
import secrets
import threading
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

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


@dataclass(frozen=True)
class LoginContext:
    user: User
    session_id: int


class AuthService:
    def __init__(self, database: Database, pool: SessionPool, backend: AuthBackend, config: Settings) -> None:
        self.database = database
        self.pool = pool
        self.backend = backend
        self.config = config
        self.lock = threading.RLock()
        self.on_login: Callable[[int, int], None] | None = None
        self.on_close: Callable[[int, str], None] | None = None

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
            opened_id: int | None = None
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
                    token = secrets.token_urlsafe(32)
                    expires = utcnow() + timedelta(hours=self.config.server.session_hours)
                    login = UserSession(token_hash=token_hash(token), user_id=user.id, client_host=host, expires_at=expires)
                    session.add(login)
                    session.flush()
                    self.pool.open(login.id, username, password)
                    opened_id = login.id
                    user.last_login_at = utcnow()
                    OplogService(session).write(user.id, ",".join(user.roles), "登录", "user", str(user.id),
                                               None, None, "成功", host)
                if self.on_login is not None:
                    try:
                        self.on_login(user.id, login.id)
                    except Exception:
                        logging.getLogger("nas_workbench").error("登录后的缩略图恢复未完成，可在任务列表重试")
                return user, token, expires
            except (NasPermissionDenied, NasUnavailable) as error:
                self._failed_login(username, host)
                code = "nas_permission_denied" if isinstance(error, NasPermissionDenied) else "nas_unavailable"
                raise AppError(code, "NAS 无权限" if isinstance(error, NasPermissionDenied) else "NAS 连接暂时不可用",
                               403 if isinstance(error, NasPermissionDenied) else 503) from None
            except Exception:
                if opened_id is not None:
                    self.pool.close(opened_id)
                raise
            finally:
                password = ""

    def close_session(self, session_id: int, host: str, *, force: bool = False) -> None:
        try:
            self.pool.close(session_id, force=force)
        except NasUnavailable:
            logging.getLogger("nas_workbench").warning("NAS 连接关闭失败，已从会话池移除")
        if self.on_close is not None:
            self.on_close(session_id, host)

    def disconnect(self, session_id: int, host: str) -> None:
        with self.lock:
            with self.database.session() as session:
                login = session.get(UserSession, session_id)
                if login is not None and login.revoked_at is None:
                    login.revoked_at = utcnow()
                    OplogService(session).write(login.user_id, "system", "NAS 会话中断", "user",
                                               str(login.user_id), None, None, "成功", host)
            self.close_session(session_id, host, force=True)

    def current(self, token: str | None, host: str, *, require_nas: bool = True) -> User:
        return self.context(token, host, require_nas=require_nas).user

    def context(self, token: str | None, host: str, *, require_nas: bool = True) -> LoginContext:
        if token is None or len(token) != 43:
            raise AppError("login_required", "请先登录", 401)
        with self.lock:
            failure: AppError | None = None
            with self.database.session() as session:
                login = session.scalar(select(UserSession).where(UserSession.token_hash == token_hash(token)))
                if login is None or login.revoked_at is not None:
                    raise AppError("login_required", "请先登录", 401)
                user = session.get(User, login.user_id)
                if user is None or not user.enabled:
                    failure = AppError("login_required", "账号已停用，请重新登录", 401)
                elif is_expired(login.expires_at):
                    failure = AppError("session_expired", "登录已过期，请重新登录", 401)
                elif require_nas:
                    try:
                        self.pool.get(login.id)
                    except NasSessionExpired:
                        failure = AppError("nas_session_expired", "NAS 连接已超时，请重新登录", 401)
                if failure is not None:
                    login.revoked_at = utcnow()
                    OplogService(session).write(login.user_id, ",".join(user.roles) if user else "system",
                                               "会话失效", "user", str(login.user_id), None, None, "成功", host)
            if failure is not None:
                self.close_session(login.id, host)
                raise failure
            assert user is not None
            return LoginContext(user, login.id)

    def logout(self, token: str, host: str) -> None:
        with self.lock:
            with self.database.session() as session:
                login = session.scalar(select(UserSession).where(UserSession.token_hash == token_hash(token)))
                if login is None or login.revoked_at is not None:
                    raise AppError("login_required", "请先登录", 401)
                user = session.get(User, login.user_id)
                login.revoked_at = utcnow()
                OplogService(session).write(login.user_id, ",".join(user.roles), "退出登录", "user", str(login.user_id),
                                           None, None, "成功", host)
            self.close_session(login.id, host)
