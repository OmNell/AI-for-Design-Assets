"""视觉识别证据与当前待审文件绑定，编辑标签不产生证据。"""
from app.db.models import BatchFile, BatchProduct
from .pre_descriptions import state, valid_fingerprint


def has_visual_evidence(product: BatchProduct, file: BatchFile) -> bool:
    return (
        product.batch_file_id == file.id
        and product.batch_id == file.batch_id
        and file.upload_status == "done"
        and not file.removed
        and valid_fingerprint(file)
        and file.fingerprint == product.ai_fingerprint
        and product.ai_success_at is not None
        and bool(product.ai_model)
        and (product.ai_description_version is None and not product.pre_description_version
             or (product.ai_description_version == product.pre_description_version and state(product, file) == "ready"))
    )


def matches_frozen_description(evidence: dict[str, object], fingerprint: str) -> bool:
    version = evidence.get("pre_description_version")
    if version is None:
        return evidence.get("pre_description") is None and evidence.get("ai_description_version") is None
    return (type(version) is int and version > 0 and evidence.get("ai_description_version") == version
            and evidence.get("pre_description_fingerprint") == fingerprint
            and bool(evidence.get("pre_description")) and bool(evidence.get("pre_description_by"))
            and bool(evidence.get("pre_description_at")))
