"""初始化账号、会话、配置、操作日志和任务表。"""
from alembic import op
import sqlalchemy as sa

revision = "0001_skeleton"
down_revision = None
branch_labels = None
depends_on = None


def common() -> list[sa.Column]:
    return [sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False)]


def upgrade() -> None:
    op.create_table("users", *common(),
        sa.Column("username", sa.String, nullable=False, unique=True),
        sa.Column("display_name", sa.String), sa.Column("roles", sa.JSON, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False), sa.Column("last_login_at", sa.DateTime(timezone=True)))
    op.create_table("sessions", *common(),
        sa.Column("token_hash", sa.String, nullable=False, unique=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("client_host", sa.String, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("settings", *common(),
        sa.Column("key", sa.String, nullable=False, unique=True),
        sa.Column("value", sa.JSON, nullable=False), sa.Column("encrypted", sa.Boolean, nullable=False))
    op.create_table("oplog", *common(),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("role", sa.String, nullable=False), sa.Column("action", sa.String, nullable=False),
        sa.Column("object_type", sa.String, nullable=False), sa.Column("object_id", sa.String),
        sa.Column("before", sa.JSON), sa.Column("after", sa.JSON),
        sa.Column("result", sa.String, nullable=False), sa.Column("client_host", sa.String, nullable=False))
    op.create_table("jobs", *common(),
        sa.Column("kind", sa.String, nullable=False), sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("status", sa.String, nullable=False), sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("run_as_user_id", sa.Integer, sa.ForeignKey("users.id")), sa.Column("error", sa.Text),
        sa.Column("scheduled_at", sa.DateTime(timezone=True)), sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)))
    op.execute("CREATE TRIGGER oplog_no_update BEFORE UPDATE ON oplog BEGIN SELECT RAISE(ABORT, '操作日志禁止修改'); END")
    op.execute("CREATE TRIGGER oplog_no_delete BEFORE DELETE ON oplog BEGIN SELECT RAISE(ABORT, '操作日志禁止删除'); END")


def downgrade() -> None:
    op.execute("DROP TRIGGER oplog_no_update")
    op.execute("DROP TRIGGER oplog_no_delete")
    for table in ("jobs", "oplog", "settings", "sessions", "users"):
        op.drop_table(table)
