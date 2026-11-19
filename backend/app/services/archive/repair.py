"""生成冻结的修复清单，并核对档案目标与待修复状态。"""
from __future__ import annotations

import hashlib

from app.errors import AppError
from app.services import archive
from app.services.nas import NasFs, safe_join
from app.services.upload.fingerprint import fingerprint


def target_documents(data: dict) -> dict[str, dict]:
    return {path: {"rel_path": path, "fingerprint": hashlib.sha256(body.encode()).hexdigest(),
                   "size": len(body.encode())} for path, body in archive.documents(data).items()}


def existing_document(fs: NasFs, root: str, path: str) -> dict | None:
    full = safe_join(root, path)
    if not fs.exists(full):
        return None
    info = fs.stat(full)
    if info.is_dir:
        raise AppError("archive_path_conflict", "待修复档案位置已被目录占用", 409)
    return {"rel_path": path, "fingerprint": fingerprint(fs, full, info.size), "size": info.size}


def changes(fs: NasFs, root: str, before: dict) -> dict:
    return {path: {"before": existing_document(fs, root, path), "after": expected}
            for path, expected in target_documents(before).items()}


def check(fs: NasFs, root: str, data: dict, frozen: dict) -> None:
    if target_documents(data) != {path: entry["after"] for path, entry in frozen.items()}:
        raise AppError("archive_repair_changed", "当前卡片柜的档案内容与审核目标不符", 409)
    for path, entry in frozen.items():
        if existing_document(fs, root, path) not in (entry["before"], entry["after"]):
            raise AppError("archive_repair_changed", "档案在审核后被修改，请核对后继续", 409)
