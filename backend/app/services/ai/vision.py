"""视觉识别证据与当前待审文件绑定，编辑标签不产生证据。"""
from app.db.models import BatchFile, BatchProduct


def has_visual_evidence(product: BatchProduct, file: BatchFile) -> bool:
    return (
        product.batch_file_id == file.id
        and product.batch_id == file.batch_id
        and file.upload_status == "done"
        and not file.removed
        and file.fingerprint_kind == "full"
        and file.fingerprint is not None
        and file.fingerprint == product.ai_fingerprint
        and product.ai_success_at is not None
        and bool(product.ai_model)
    )
