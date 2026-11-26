"""固定字段的管理入口，历史扩展字段保留供追溯。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import User
from app.errors import AppError
from app.schemas.ai import TagFieldItem, TagFieldsResponse, TagFieldsWrite
from app.services.auth.service import AuthService
from app.services.oplog import OplogService
from .field_catalog import FIXED_FIELDS, fixed_fields
from .settings import require_admin


class TagFieldService:
    def __init__(self, auth: AuthService) -> None:
        self.auth = auth

    @staticmethod
    def _read(session: Session) -> TagFieldsResponse:
        fields = fixed_fields(session)
        return TagFieldsResponse(version=max((field.version for field in fields), default=1),
                                 fields=[TagFieldItem.model_validate(field) for field in fields])

    def read(self, actor: User) -> TagFieldsResponse:
        with self.auth.lock, self.auth.database.session() as session:
            require_admin(session, actor)
            return self._read(session)

    def enabled_fields(self) -> list[dict[str, str | int | bool]]:
        with self.auth.lock, self.auth.database.session() as session:
            return [{"key": field.key, "label": field.label, "hint": field.hint, "sort_order": field.sort_order,
                     "enabled": field.enabled, "version": field.version} for field in fixed_fields(session)]

    def save(self, actor: User, body: TagFieldsWrite, host: str) -> TagFieldsResponse:
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            require_admin(session, actor)
            before = self._read(session)
            if body.version != before.version:
                raise AppError("tag_fields_version_conflict", "字段清单已更新，请刷新后重新保存", 409)
            requested = {field.key: field for field in body.fields}
            if requested.keys() != FIXED_FIELDS.keys():
                raise AppError("fixed_tag_fields", "须提交全部 11 个固定字段，只能改填写说明和显示顺序", 422)
            existing = fixed_fields(session)
            if all((field.hint, field.sort_order) == (requested[field.key].hint, requested[field.key].sort_order) for field in existing):
                return before
            for field in existing:
                item = requested[field.key]
                field.hint, field.sort_order, field.version = item.hint, item.sort_order, before.version + 1
            session.flush()
            after = self._read(session)
            OplogService(session).write(actor.id, "admin", "修改标签字段", "tag_fields", str(after.version),
                                       before.model_dump(), after.model_dump(), "成功", host)
            return after
