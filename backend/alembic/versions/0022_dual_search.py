"""记录检索模式并为编号、正式商品和结构化筛选建立索引。"""
from alembic import op
import sqlalchemy as sa

revision = "0022_dual_search"
down_revision = "0021_pre_descriptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 历史请求走原混合检索入口，含原本地编号直达。
    op.add_column("search_logs", sa.Column("mode", sa.String(), nullable=False, server_default="semantic"))
    op.create_index("ix_projects_search_code", "projects", [sa.text("project_code COLLATE NOCASE")])
    op.create_index("ix_products_search_code", "products", [sa.text("product_code COLLATE NOCASE")])
    op.create_index("ix_projects_search_filters", "projects", ["status", "industry_id", "company_id", "won"])
    op.create_index("ix_product_tags_search_confirmed", "product_tags", ["confirmation_status", "product_id"])


def downgrade() -> None:
    op.drop_index("ix_product_tags_search_confirmed", table_name="product_tags")
    op.drop_index("ix_projects_search_filters", table_name="projects")
    op.drop_index("ix_products_search_code", table_name="products")
    op.drop_index("ix_projects_search_code", table_name="projects")
    op.drop_column("search_logs", "mode")
