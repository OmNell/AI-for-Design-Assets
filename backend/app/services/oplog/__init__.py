"""操作日志只追加；由调用方事务提交，确保与业务操作原子写入。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import Oplog


class OplogService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def write(self, user: int | None, role: str, action: str, object_type: str,
              object_id: str | None, before: object | None, after: object | None,
              result: str, client_host: str) -> Oplog:
        entry = Oplog(user_id=user, role=role, action=action, object_type=object_type,
                      object_id=object_id, before=before, after=after, result=result,
                      client_host=client_host)
        self.session.add(entry)
        self.session.flush()
        return entry
