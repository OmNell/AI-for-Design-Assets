"""待审主图的人工身份资料，以文件身份和完整 SHA-256 绑定。"""
from pathlib import PurePosixPath
import re
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Batch, BatchFile, BatchProduct
from app.errors import AppError
from app.services.thumbs import is_main_image

AWAITING_DESCRIPTION = "awaiting_description"
EDITABLE = ("已暂存", "AI 处理失败", "待核对", "已驳回")


def editable(batch: Batch) -> bool:
    return batch.kind in {"ingest", "supplement"} and batch.status in EDITABLE


def validated_text(value: str) -> str:
    text = value.strip()
    if not 2 <= len(text) <= 180 or not any(char.isalnum() for char in text):
        raise AppError("invalid_pre_description", "请用 2–180 字描述产品，不能只填空白或标点", 422)
    return text


def is_completed_main(file: BatchFile) -> bool:
    return (is_main_image(file) and not file.removed and file.conflict != "duplicate"
            and file.upload_status == "done" and file.size > 0 and file.bytes_received == file.size)


def valid_fingerprint(file: BatchFile) -> bool:
    return file.fingerprint_kind == "full" and bool(re.fullmatch(r"[0-9a-f]{64}", file.fingerprint or ""))


def state(product: BatchProduct, file: BatchFile) -> Literal["missing", "ready", "reconfirm"]:
    if not product.pre_description:
        return "missing"
    if (not product.pre_description_version or not product.pre_description_by or not product.pre_description_at
            or not valid_fingerprint(file)
            or product.pre_description_fingerprint != file.fingerprint):
        return "reconfirm"
    return "ready"


def products_for_batch(session: Session, batch: Batch, *, initialize: bool = False) -> list[tuple[BatchProduct, BatchFile]]:
    session.flush()
    products = {product.batch_file_id: product for product in session.scalars(
        select(BatchProduct).where(BatchProduct.batch_id == batch.id))}
    result = []
    for file in session.scalars(select(BatchFile).where(BatchFile.batch_id == batch.id).order_by(BatchFile.id)):
        if not is_completed_main(file):
            continue
        product = products.get(file.id)
        if product is None:
            if not initialize:
                continue
            path = PurePosixPath(file.rel_path)
            product = BatchProduct(batch_id=batch.id, batch_file_id=file.id, display_name=path.stem,
                                   kind="set" if path.parts[0] == "套装" else "item",
                                   ai_status=AWAITING_DESCRIPTION)
            session.add(product)
        result.append((product, file))
    session.flush()
    return result


def require_descriptions(products: list[tuple[BatchProduct, BatchFile]]) -> None:
    missing = [{"product_id": product.id, "batch_file_id": file.id, "name": product.display_name,
                "state": status} for product, file in products if (status := state(product, file)) != "ready"]
    if missing:
        names = "、".join(entry["name"] for entry in missing[:3])
        extra = f"等 {len(missing)} 张" if len(missing) > 3 else ""
        raise AppError("pre_descriptions_required", f"请先填写或重新确认主图描述：{names}{extra}", 409, missing)
