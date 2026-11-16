"""角色表管理；不替用户授予 NAS 文件权限。"""
from sqlalchemy import delete, select

from app.db.models import User, UserSession
from app.errors import AppError
from app.schemas.auth import RoleUpdate
from app.services.auth.service import AuthService
from app.services.oplog import OplogService


class RoleService:
    def __init__(self, auth: AuthService) -> None:
        self.auth = auth

    def list_users(self, actor: User) -> list[User]:
        with self.auth.lock, self.auth.database.session() as session:
            current = session.get(User, actor.id)
            if current is None or not current.enabled or "admin" not in current.roles:
                raise AppError("forbidden", "当前账号没有此操作权限", 403)
            return list(session.scalars(select(User).order_by(User.id)))

    def update(self, actor: User, change: RoleUpdate, host: str) -> User:
        with self.auth.lock:
            with self.auth.database.session() as session:
                # 与修改处于同一事务，再次确认操作者仍有效，避免检查后被撤权。
                current = session.get(User, actor.id)
                if current is None or not current.enabled or "admin" not in current.roles:
                    raise AppError("forbidden", "当前账号没有此操作权限", 403)
                user = session.get(User, change.user_id)
                if user is None:
                    raise AppError("user_not_found", "用户不存在，请让该用户先登录一次", 404)
                before = {"roles": user.roles, "enabled": user.enabled}
                user.roles = [role.value for role in change.roles]
                user.enabled = change.enabled
                if not user.enabled:
                    session.execute(delete(UserSession).where(UserSession.user_id == user.id))
                OplogService(session).write(current.id, "admin", "修改角色", "user", str(user.id), before,
                                           {"roles": user.roles, "enabled": user.enabled}, "成功", host)
            if not user.enabled:
                self.auth.pool.close(user.id, force=True)
            return user
