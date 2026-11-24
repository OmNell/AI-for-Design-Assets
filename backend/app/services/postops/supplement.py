"""补充文件的正式目标和去重判定。"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Batch, BatchFile, CatalogFile, Project
from app.errors import AppError
from app.schemas.audit import SupplementComparison, SupplementComparedFile, SupplementReplacement


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



def frozen_comparison(content: dict) -> SupplementComparison:
    """使用冻结资料和商品映射还原补充决定，兼容已有审核快照。"""
    operation = content["operation"]
    before = operation["before"]
    files = {row["id"]: row for row in before["files"] if row["status"] == "active"}
    by_path = {row["rel_path"].casefold(): row for row in files.values()}
    products = {row["id"]: row["file_id"] for row in before["products"] if row["status"] == "active"}
    replacements = {row["id"]: row.get("existing_product_id") for row in operation["params"].get("products", [])}
    decisions = {row["id"]: row["choice"] for row in operation["params"].get("files", [])}
    mains = {row["batch_file_id"]: row["id"] for row in content["review"]["products"]}
    summary = SupplementComparison()
    for uploaded in content["files"]:
        if uploaded.get("conflict") == "duplicate":
            summary.skipped += 1
            continue
        file_id = uploaded["id"]
        choice = decisions.get(file_id, uploaded.get("conflict_choice"))
        product = replacements.get(mains.get(file_id))
        prior = files.get(products.get(product)) if product else None
        if prior is None and choice == "replace":
            prior = by_path.get(uploaded["rel_path"].casefold())
        if prior is not None:
            summary.replaced += 1
            if file_id not in mains:
                summary.materials.append(SupplementReplacement(
                    original=SupplementComparedFile.model_validate(prior),
                    uploaded=SupplementComparedFile.model_validate(uploaded)))
        else:
            summary.added += 1
            summary.kept_both += int(choice == "keep_both")
    return summary
