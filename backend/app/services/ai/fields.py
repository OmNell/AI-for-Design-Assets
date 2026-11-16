"""自由文本字段清单；删除等同停用，历史商品值不改写。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import TagField, User
from app.errors import AppError
from app.schemas.ai import TagFieldItem, TagFieldsResponse, TagFieldsWrite
from app.services.auth.service import AuthService
from app.services.oplog import OplogService
from .settings import require_admin


class TagFieldService:
    def __init__(self, auth: AuthService) -> None:
        self.auth = auth

    @staticmethod
    def _read(session: Session) -> TagFieldsResponse:
        fields = list(session.scalars(select(TagField).order_by(TagField.sort_order, TagField.id)))
        return TagFieldsResponse(version=max((field.version for field in fields), default=1),
                                 fields=[TagFieldItem.model_validate(field) for field in fields])

    def read(self, actor: User) -> TagFieldsResponse:
        with self.auth.lock, self.auth.database.session() as session:
            require_admin(session, actor)
            return self._read(session)

    def enabled_fields(self) -> list[dict[str, str | int | bool]]:
        with self.auth.lock, self.auth.database.session() as session:
            return [{"key": field.key, "label": field.label, "hint": field.hint, "sort_order": field.sort_order,
                     "enabled": field.enabled, "version": field.version}
                    for field in session.scalars(select(TagField).where(TagField.enabled.is_(True)).order_by(TagField.sort_order, TagField.id))]

    def save(self, actor: User, body: TagFieldsWrite, host: str) -> TagFieldsResponse:
        with self.auth.lock, self.auth.database.session() as session:
            require_admin(session, actor)
            before = self._read(session)
            if body.version != before.version:
                raise AppError("tag_fields_version_conflict", "字段清单已更新，请刷新后重新保存", 409)
            version = before.version + 1
            existing = {field.key: field for field in session.scalars(select(TagField))}
            requested = {field.key: field for field in body.fields}
            for key, field in existing.items():
                field.version = version
                if key not in requested:
                    field.enabled = False
            for item in body.fields:
                field = existing.get(item.key)
                if field is None:
                    field = TagField(**item.model_dump(), version=version)
                    session.add(field)
                else:
                    for key, value in item.model_dump().items():
                        setattr(field, key, value)
                    field.version = version
            session.flush()
            after = self._read(session)
            OplogService(session).write(actor.id, "admin", "修改标签字段", "tag_fields", str(version),
                                       before.model_dump(), after.model_dump(), "成功", host)
            return after
