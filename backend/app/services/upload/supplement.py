"""补充包上传侧的规则：逐文件判定新增 / 重复 / 冲突，以及「另存为新文件」的命名。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Batch, BatchFile, CatalogFile


def catalog_files(session: Session, project_id: int) -> dict[str, CatalogFile]:
    """项目内当前有效的文件，按小写相对路径索引（NAS 上的文件名不区分大小写）。"""
    return {row.rel_path.casefold(): row for row in session.scalars(
        select(CatalogFile).where(CatalogFile.project_id == project_id, CatalogFile.status == "active"))}


def classify(session: Session, batch: Batch) -> dict[str, int]:
    """全部上传完成后调用：只按相对路径和指纹判定，不看文件名里的版本字样。"""
    existing = catalog_files(session, batch.project_id)
    counts = {"new": 0, "duplicate": 0, "conflict": 0}
    for file in session.scalars(select(BatchFile).where(BatchFile.batch_id == batch.id)):
        if file.conflict_choice == "keep_both":
            # 已经改名另存的文件保持原判定，不因改名后的路径变成「新增」。
            counts["conflict"] += 1
            continue
        current = existing.get(file.rel_path.casefold())
        if current is None:
            file.conflict = "new"
        elif current.fingerprint == file.fingerprint and current.size == file.size:
            file.conflict = "duplicate"
        else:
            file.conflict = "conflict"
        counts[file.conflict] += 1
    return counts


def versioned_name(rel_path: str, taken: set[str]) -> str:
    """原名_v2，已存在则递增；taken 为小写的已占用路径。"""
    directory, _, name = rel_path.rpartition("/")
    stem, dot, extension = name.rpartition(".")
    if not dot:
        stem, extension = name, ""
    number = 2
    while True:
        candidate = f"{directory + '/' if directory else ''}{stem}_v{number}{'.' + extension if dot else ''}"
        if candidate.casefold() not in taken:
            return candidate
        number += 1


def unresolved(session: Session, batch: Batch) -> list[BatchFile]:
    return [file for file in session.scalars(select(BatchFile).where(BatchFile.batch_id == batch.id).order_by(BatchFile.rel_path))
            if file.conflict == "conflict" and file.conflict_choice not in ("replace", "keep_both") and not file.removed]
