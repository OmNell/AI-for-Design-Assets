"""已通过发布校验的 NAS 恢复凭据，用于 SQLite 全失时恢复。"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy.orm import Session

from app.db.models import Batch
from app.services import archive
from app.services.nas import NasFs, safe_join
from app.services.oplog import OplogService

RECEIPT = f"{archive.HISTORY_DIR}/_发布凭据.json"


def digest(data: archive.Manifest) -> str:
    return hashlib.sha256(json.dumps(archive._canonical(data), ensure_ascii=False,
                                     sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def receipt(data: archive.Manifest) -> dict:
    project = data["project"]
    return {"format": 1, "project_id": project["id"], "project_code": project["project_code"],
            "asset_version": project.get("asset_version", 0), "root": project["current_path"],
            "manifest_sha256": digest(data)}


def document(data: archive.Manifest) -> str:
    return json.dumps(receipt(data), ensure_ascii=False, sort_keys=True) + "\n"


def confirmed(fs: NasFs, root: str, data: archive.Manifest, relative: str) -> bool:
    path = safe_join(root, RECEIPT)
    if data["project"]["current_path"] != relative or not fs.exists(path) or fs.stat(path).size > 8192:
        return False
    try:
        return json.loads(archive._read(fs, path)) == receipt(data)
    except (UnicodeError, json.JSONDecodeError):
        return False


def seal(session: Session, batch: Batch, fs: NasFs, root: str, data: archive.Manifest) -> None:
    """验证和数据库写入检查均通过后，先持久化凭据，再提交可见状态。"""
    OplogService(session).write(batch.final_reviewer_id, "final_reviewer", "登记恢复发布凭据", "batch",
        str(batch.id), None, {"asset_version": data["project"].get("asset_version", 0)}, "成功", "后台发布")
    session.flush()
    archive.write(fs, root, {RECEIPT: document(data)}, replace=batch.kind == "archive_repair")
