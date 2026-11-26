"""管理员保存 AI 连接设置；密钥只以 Fernet 密文进入 settings。"""
from __future__ import annotations

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import AiConfig
from app.db.models import Setting, User
from app.errors import AppError
from app.schemas.ai import AiSettingsResponse, AiSettingsWrite
from app.services.auth.service import AuthService
from app.services.crypto import SecretCipher
from app.services.oplog import OplogService


def require_admin(session: Session, actor: User) -> None:
    current = session.get(User, actor.id)
    if current is None or not current.enabled or "admin" not in current.roles:
        raise AppError("forbidden", "当前账号没有管理 AI 设置的权限", 403)


class AiSettingsService:
    def __init__(self, auth: AuthService, cipher: SecretCipher) -> None:
        self.auth = auth
        self.cipher = cipher

    def _effective(self, session: Session) -> AiConfig:
        base = self.auth.config.ai.model_copy(deep=True)
        row = session.scalar(select(Setting).where(Setting.key == "ai"))
        if row is None:
            return base
        values = base.model_dump()
        if isinstance(row.value, dict):
            values.update(row.value)
        return AiConfig.model_validate(values)

    def effective(self) -> AiConfig:
        with self.auth.lock, self.auth.database.session() as session:
            return self._effective(session)

    @staticmethod
    def _public(config: AiConfig) -> AiSettingsResponse:
        return AiSettingsResponse(**config.model_dump(exclude={"api_key_enc"}),
                                  api_key_set=bool(config.api_key_enc.get_secret_value()))

    def read(self, actor: User) -> AiSettingsResponse:
        with self.auth.lock, self.auth.database.session() as session:
            require_admin(session, actor)
            return self._public(self._effective(session))

    def save(self, actor: User, body: AiSettingsWrite, host: str) -> AiSettingsResponse:
        with self.auth.lock, self.auth.database.session() as session:
            require_admin(session, actor)
            if self.auth.config.mode == "prod":
                try:
                    url = httpx.URL(body.base_url)
                    valid_url = url.scheme == "https" and bool(url.host)
                except (httpx.InvalidURL, ValueError):
                    valid_url = False
                if not valid_url:
                    raise AppError("invalid_ai_url", "生产模式的百炼接口地址必须使用 HTTPS", 422)
            current = self._effective(session)
            before = self._public(current)
            values = body.model_dump(exclude={"api_key"})
            supplied = body.api_key.get_secret_value() if body.api_key is not None else ""
            try:
                values["api_key_enc"] = self.cipher.encrypt(supplied) if supplied else current.api_key_enc.get_secret_value()
            finally:
                supplied = ""
            settings = AiConfig.model_validate(values)
            row = session.scalar(select(Setting).where(Setting.key == "ai"))
            if row is None:
                row = Setting(key="ai", value=values, encrypted=bool(values["api_key_enc"]))
                session.add(row)
            else:
                row.value = values
                row.encrypted = bool(values["api_key_enc"])
            after = self._public(settings)
            OplogService(session).write(actor.id, "admin", "修改 AI 设置", "settings", "ai",
                                       before.model_dump(), after.model_dump(), "成功", host)
            return after
