"""补充文件的正式目标和去重判定。"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Batch, BatchFile, CatalogFile, Project
from app.errors import AppError


def active_batch_id(session: Session, project_id: int) -> int | None:
    return session.scalar(select(Batch.id).where(
        Batch.op_lock_key == f"project:{project_id}", Batch.lock_active.is_(True)).limit(1))


def project_for_supplement(session: Session, project_id: int) -> Project:
    project = session.get(Project, project_id)
    if project is None or not project.current_path:
        raise AppError("project_not_found", "正式项目不存在", 404)
    if project.status != "active":
        raise AppError("invalid_project_state", "只有有效的已入库项目可以补充", 409)
    return project


def catalog_files(session: Session, project_id: int) -> dict[str, CatalogFile]:
    return {row.rel_path.casefold(): row for row in session.scalars(select(CatalogFile).where(
        CatalogFile.project_id == project_id, CatalogFile.status == "active"))}


def classify_file(file: BatchFile, current: CatalogFile | None) -> None:
    if file.upload_status != "done":
        file.conflict = file.conflict_choice = None
        return
    conflict = "new"
    if current is not None:
        same_bytes = (
            current.fingerprint_kind == file.fingerprint_kind == "full"
            and bool(file.fingerprint)
            and current.fingerprint == file.fingerprint
        )
        conflict = "duplicate" if same_bytes else "conflict"
    if file.conflict != conflict:
        file.conflict, file.conflict_choice = conflict, None


def invalidate_review(batch: Batch, file_id: int, product_id: int, kind: str) -> None:
    """增删待核对主图时，旧选择和相关套装确认不可沿用。"""
    operation = dict(batch.payload.get("operation", {}))
    operation["files"] = [row for row in operation.get("files", []) if row["id"] != file_id]
    operation["products"] = [row for row in operation.get("products", []) if row["id"] != product_id]
    if kind == "item":
        operation["relation_reviews"], operation["relation_batch_items"] = {}, {}
    else:
        operation["batch_set_items"] = {key: members for key, members in operation.get("batch_set_items", {}).items()
                                          if key != str(product_id)}
    batch.payload = {**batch.payload, "operation": operation}
