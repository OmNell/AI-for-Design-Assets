"""用当前访问者的 NAS 会话定位文件，并从同一读句柄核验完整指纹。"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import BinaryIO, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CatalogFile, HistoryVersion, InspectionFinding, Project
from app.errors import AppError
from app.services.auth.service import AuthService, LoginContext
from app.services.nas import NasFs, NasNotFound, NasPaths, Region, safe_join
from app.services.nas.factory import create_paths
from app.services.oplog import OplogService
from app.services.upload.service import UploadService

CHUNK_SIZE = 1024 * 1024
VISIBLE_PROJECTS = ("active", "voided", "deleted")
ARCHIVES = {"单品档案", "套装档案", "_项目档案.txt", "_工作台入库校验", "_工作台版本标记"}
DRIFT = "NAS 上的文件内容与正式登记不一致。当前文件未经审核确认。"


@dataclass(frozen=True)
class Reference:
    file: CatalogFile
    project: Project
    relative: str
    occupied: set[str]


@dataclass
class ReadAsset:
    reference: Reference
    stream: BinaryIO
    digest: str
    size: int
    mtime: float
    warning: str | None = None

    @property
    def name(self) -> str:
        return self.reference.file.rel_path.rsplit("/", 1)[-1]

    @property
    def status(self) -> str:
        return "voided" if self.reference.project.status == "voided" else self.reference.file.status


def reference(session: Session, file_id: int) -> Reference:
    file = session.get(CatalogFile, file_id)
    project = session.get(Project, file.project_id) if file else None
    if not project or project.status not in VISIBLE_PROJECTS:
        raise AppError("file_not_found", "文件不存在", 404)
    if project.status == "deleted" or file.status == "deleted":
        raise AppError("file_deleted", "该文件已物理删除", 410)
    if file.status not in ("active", "voided", "replaced"):
        raise AppError("file_not_found", "文件不存在", 404)
    history = {}
    for row in session.scalars(select(HistoryVersion).where(HistoryVersion.project_id == project.id).order_by(HistoryVersion.id)):
        history[row.file_id] = row
    if file_id in history and history[file_id].expected_missing:
        raise AppError("file_deleted", "该文件已物理删除", 410)

    def physical(row: CatalogFile) -> str:
        old = history.get(row.id)
        return row.physical_rel_path or (old.new_rel_path if old else row.rel_path)

    occupied = {physical(row).casefold() for row in session.scalars(
        select(CatalogFile).where(CatalogFile.project_id == project.id, CatalogFile.id != file_id))}
    return Reference(file, project, physical(file), occupied)


def _measure(stream: BinaryIO) -> tuple[int, str]:
    digest = hashlib.sha256()
    length = 0
    while chunk := stream.read(CHUNK_SIZE):
        length += len(chunk)
        digest.update(chunk)
    stream.seek(0)
    return length, digest.hexdigest()


def _open(fs: NasFs, target: str, ref: Reference) -> ReadAsset:
    stat = fs.stat(target)
    if stat.is_dir:
        raise NasNotFound("登记位置没有文件")
    stream = fs.open_read(target)
    try:
        size, digest = _measure(stream)
        after = fs.stat(target)
        if size != stat.size or stat != after:
            raise AppError("file_unstable", "NAS 文件正在变化，请稍后重试", 409)
        return ReadAsset(ref, stream, digest, size, stat.mtime)
    except BaseException:
        stream.close()
        raise


def _directories(fs: NasFs, root: str) -> Iterator[str]:
    try:
        for name in fs.list(root):
            if fs.stat(safe_join(root, name)).is_dir:
                yield name
    except NasNotFound:
        return


def _roots(fs: NasFs, paths: NasPaths, code: str) -> Iterator[str]:
    def belongs(name: str) -> bool:
        return name.partition("_")[0].casefold() == code.casefold()

    for branch in ("01已中标项目", "02未中标项目"):
        for industry in _directories(fs, paths.join(Region.FORMAL, branch)):
            for company in _directories(fs, paths.join(Region.FORMAL, branch, industry)):
                parent = safe_join(branch, industry, company)
                for folder in _directories(fs, paths.join(Region.FORMAL, parent)):
                    if belongs(folder):
                        yield safe_join(parent, folder)
    for folder in _directories(fs, paths.join(Region.FORMAL, "_已作废项目")):
        if belongs(folder):
            yield safe_join("_已作废项目", folder)


def _candidates(fs: NasFs, root: str, size: int, rel: str = "") -> Iterator[tuple[str, float]]:
    prefix = safe_join(root, rel) if rel else root
    for name in fs.list(prefix):
        if not rel and name in ARCHIVES:
            continue
        child = safe_join(rel, name) if rel else safe_join(name)
        stat = fs.stat(safe_join(root, child))
        if stat.is_dir:
            yield from _candidates(fs, root, size, child)
        elif stat.size == size:
            yield child, stat.mtime


class AssetLocator:
    def __init__(self, auth: AuthService) -> None:
        self.auth = auth
        self.paths = create_paths(auth.config)

    def _finding(self, session: Session, ref: Reference, kind: str, path: str, detail: str,
                 login: LoginContext, host: str, *, fixed: bool = False, before: object = None) -> None:
        prior = session.scalar(select(InspectionFinding).where(
            InspectionFinding.run_id.is_(None), InspectionFinding.kind == kind, InspectionFinding.file_id == ref.file.id, InspectionFinding.path == path))
        if prior is not None and not fixed:
            return
        if prior is None:
            session.add(InspectionFinding(kind=kind, project_id=ref.project.id, file_id=ref.file.id,
                                          path=path, detail=detail, auto_fixed=fixed))
        action = {"moved": "记录 NAS 违规移动", "modified": "记录 NAS 内容异常", "missing": "登记文件待巡检"}[kind]
        OplogService(session).write(login.user.id, ",".join(login.user.roles), action, "file", str(ref.file.id),
                                    before, {"path": path, "project_id": ref.project.id, "detail": detail},
                                    "成功" if fixed else "异常", host)

    def _record(self, ref: Reference, kind: str, root: str, rel: str, login: LoginContext, host: str) -> None:
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            UploadService._check_login(session, login)
            current = reference(session, ref.file.id)
            if (current.project.current_path, current.relative, current.file.fingerprint, current.file.status) != (
                ref.project.current_path, ref.relative, ref.file.fingerprint, ref.file.status
            ):
                # 同一观察已由并发下载登记时保留这次授权读取。
                if kind == "moved" and (current.project.current_path, current.relative, current.file.fingerprint) == (
                    root, rel, ref.file.fingerprint
                ):
                    return
                raise AppError("asset_changed", "文件登记位置或状态已变化，请重试", 409)
            if kind == "moved":
                before = {"project_path": current.project.current_path, "physical_rel_path": current.relative}
                current.project.current_path = root
                current.file.physical_rel_path = rel
                self._finding(session, ref, kind, safe_join(root, rel), "完整 SHA-256 确认后已修正物理位置",
                              login, host, fixed=True, before=before)
            else:
                detail = DRIFT if kind == "modified" else "文件缺失或存在相同指纹的多个候选，等待核对"
                self._finding(session, ref, kind, safe_join(root, rel), detail, login, host)

    def _relocated(self, fs: NasFs, ref: Reference) -> tuple[str, str] | None:
        old_root = self.paths.join(Region.FORMAL, ref.project.current_path)
        if fs.exists(old_root):
            roots = [ref.project.current_path]
        elif ref.project.asset_version:
            # 版本目录缺失时不能用旧版本中的相同 SHA-256 误接当前公开对象。
            return None
        else:
            roots = list(dict.fromkeys(_roots(fs, self.paths, ref.project.project_code)))
        matched: list[tuple[str, str]] = []
        for root in roots:
            absolute = self.paths.join(Region.FORMAL, root)
            entries = list(_candidates(fs, absolute, ref.file.size))
            # 先考虑原相对位置，再考虑原文件名与修改时间。所有可疑的同大小路径都核验，避免把副本当作唯一移动。
            name = ref.relative.rsplit("/", 1)[-1]
            entries.sort(key=lambda row: (row[0] != ref.relative, row[0].rsplit("/", 1)[-1] != name,
                                          int(row[1] * 1000) != ref.file.mtime))
            for candidate, _ in entries:
                if candidate.casefold() in ref.occupied:
                    continue
                with fs.open_read(safe_join(absolute, candidate)) as stream:
                    size, digest = _measure(stream)
                if size == ref.file.size and digest == ref.file.fingerprint:
                    matched.append((root, candidate))
                    if len(matched) > 1:
                        return None
        return matched[0] if matched else None

    def open(self, fs: NasFs, ref: Reference, login: LoginContext, host: str) -> ReadAsset:
        if ref.file.fingerprint_kind != "full":
            raise AppError("invalid_fingerprint", "文件缺少完整指纹，请联系管理员", 409)
        path = self.paths.join(Region.FORMAL, ref.project.current_path, ref.relative)
        try:
            opened = _open(fs, path, ref)
        except NasNotFound:
            try:
                found = self._relocated(fs, ref)
            except NasNotFound:
                found = None
            if not found:
                self._record(ref, "missing", ref.project.current_path, ref.relative, login, host)
                raise AppError("file_missing", "文件已不存在或无法唯一定位", 404) from None
            root, relative = found
            opened = _open(fs, self.paths.join(Region.FORMAL, root, relative), ref)
            try:
                if opened.digest != ref.file.fingerprint or opened.size != ref.file.size:
                    raise AppError("file_unstable", "NAS 文件正在变化，请稍后重试", 409)
                self._record(ref, "moved", root, relative, login, host)
            except BaseException:
                opened.stream.close()
                raise
            return opened
        if opened.digest != ref.file.fingerprint or opened.size != ref.file.size:
            try:
                self._record(ref, "modified", ref.project.current_path, ref.relative, login, host)
            except BaseException:
                opened.stream.close()
                raise
            opened.warning = DRIFT
        return opened
