"""只读遍历正式 NAS，保守定位文件并登记异常。"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
import json
from time import monotonic
from typing import Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (Batch, CatalogFile, ExpectedDeletion, HistoryVersion, IngestPlan, InspectionFinding,
                           InspectionRun, Job, PostopPlan, Project, utcnow)
from app.services import archive
from app.services.archive.publication import RECEIPT
from app.services.admin.company_map import WON_FOLDERS
from app.services.auth.service import AuthService
from app.services.catalog.locator import VISIBLE_PROJECTS
from app.services.jobs import JobContext
from app.services.nas import NasFs, NasNotFound, Region, safe_join
from app.services.nas.factory import create_paths
from app.services.nas.fs import NasStat
from app.services.oplog import OplogService
from app.services.postops.shadow import MARKER, WORKSPACE, physical
from app.services.upload.fingerprint import fingerprint


@dataclass
class Finding:
    kind: str
    path: str
    detail: str
    project_id: int | None = None
    file_id: int | None = None
    auto_fixed: bool = False


@dataclass
class ProjectScan:
    project: Project
    files: list[CatalogFile]
    historical_paths: dict[int, str]
    root: str = ""
    fixes: dict[int, str] = field(default_factory=dict)
    probes: dict[int, dict] = field(default_factory=dict)


def walk(fs: NasFs, base: str, path: str = "") -> Iterator[tuple[str, NasStat]]:
    try:
        names = fs.list(safe_join(base, path) if path else base)
    except NasNotFound:
        return
    for name in names:
        child = safe_join(path, name) if path else safe_join(name)
        try:
            item = fs.stat(safe_join(base, child))
        except NasNotFound:
            continue
        yield child, item
        if item.is_dir:
            yield from walk(fs, base, child)


class InspectionService:
    def __init__(self, auth: AuthService) -> None:
        self.auth = auth
        self.paths = create_paths(auth.config)

    def _snapshot(self, session: Session):
        session.connection().exec_driver_sql("BEGIN")
        projects = {p.id: ProjectScan(p, [], {}) for p in session.scalars(select(Project).where(
            Project.status.in_(VISIBLE_PROJECTS), Project.current_path != ""))}
        ignored = {p.current_path for p in session.scalars(select(Project).where(
            Project.status.not_in(VISIBLE_PROJECTS), Project.current_path != ""))}
        retired = {}
        active = {b.project_id for b in session.scalars(select(Batch).where(Batch.lock_active.is_(True)))}
        for plan, batch in session.execute(select(IngestPlan, Batch).join(Batch).where(Batch.lock_active.is_(True))):
            root = plan.manifest.get("project", {}).get("current_path")
            if root:
                ignored.add(root)
        for plan, batch in session.execute(select(PostopPlan, Batch).join(Batch).order_by(PostopPlan.batch_id)):
            project = projects.get(batch.project_id)
            current = project.project.current_path if project else None
            if batch.status == "已入库":
                for root, data in ((plan.previous_path, plan.baseline), (plan.target_path, plan.manifest)):
                    if root != current:
                        retired[root] = (data, plan.progress.get("published_at"))
            elif plan.target_path != current:
                ignored.add(plan.target_path)
            ignored.add(safe_join(WORKSPACE, batch.batch_no))
            if plan.purge:
                ignored.add(safe_join(WORKSPACE, "删除-" + plan.progress["nonce"]))
        for file in session.scalars(select(CatalogFile)):
            if file.project_id in projects:
                projects[file.project_id].files.append(file)
        for row in session.scalars(select(HistoryVersion).order_by(HistoryVersion.id)):
            if row.project_id in projects:
                projects[row.project_id].historical_paths[row.file_id] = row.new_rel_path
        expected = {row.path: row for row in session.scalars(select(ExpectedDeletion))}
        return projects, ignored, active, expected, retired

    def _tree(self, fs: NasFs, ignored: set[str]) -> tuple[dict[str, NasStat], set[str]]:
        root = self.paths.root(Region.FORMAL)
        fs.stat(root)  # 根目录失联必须成为失败报告。
        files, directories = {}, set()

        def scan(parent: str) -> None:
            prefix = safe_join(root, parent) if parent else root
            for name in fs.list(prefix):
                relative = safe_join(parent, name) if parent else safe_join(name)
                if relative in ignored:
                    continue
                path = safe_join(root, relative)
                try:
                    info = fs.stat(path)
                    if info.is_dir:
                        directories.add(relative)
                        scan(relative)
                    else:
                        files[relative] = info
                except NasNotFound:
                    continue

        scan("")
        return files, directories

    @staticmethod
    def _relative(scan: ProjectScan, row: CatalogFile) -> str:
        return row.physical_rel_path or scan.historical_paths.get(row.id) or row.rel_path

    def _hash(self, fs: NasFs, path: str, info: NasStat, cache: dict[str, str]) -> str:
        if path not in cache:
            full_path = self.paths.join(Region.FORMAL, path)
            actual = fingerprint(fs, full_path, info.size)
            if fs.stat(full_path) != info:
                raise NasNotFound("文件在巡检期间发生变化")
            cache[path] = actual
        return cache[path]

    def _root(self, fs: NasFs, scan: ProjectScan, dirs: set[str], files: dict[str, NasStat],
              claimed: set[str], cache: dict[str, str]) -> str:
        project = scan.project
        if project.current_path in dirs:
            return project.current_path
        verified = []
        for candidate in dirs - claimed:
            name = candidate.rsplit("/", 1)[-1]
            group = candidate.split("/")
            if (len(group) < 2 or group[0] == WORKSPACE
                    or name.partition("_")[0].casefold() != project.project_code.casefold()):
                continue
            try:
                proof = archive.parse_project(fs, self.paths.join(Region.FORMAL, candidate))
            except (archive.ArchiveError, NasNotFound):
                proof = None
            if proof and (proof["project"]["id"] != project.id
                          or proof["project"].get("asset_version", 0) != project.asset_version):
                continue
            if project.asset_version and proof is None:
                continue
            evidence = 0
            eligible = [f for f in scan.files if f.status != "deleted"]
            for row in eligible:
                path = safe_join(candidate, self._relative(scan, row))
                info = files.get(path)
                if info and info.size == row.size and row.fingerprint_kind == "full":
                    try:
                        if self._hash(fs, path, info, cache) == row.fingerprint:
                            evidence += 1
                            if proof:
                                break
                    except NasNotFound:
                        continue
            if evidence and (proof is not None or evidence == len(eligible)):
                verified.append(candidate)
                if len(verified) > 1:
                    break
        return verified[0] if len(verified) == 1 else ""

    def _scan(self, fs: NasFs, scan: ProjectScan, root: str, files: dict[str, NasStat],
              expected: dict[str, ExpectedDeletion], cache: dict[str, str], found: list[Finding], known: set[str]) -> None:
        project = scan.project
        scan.root = root

        def finding(kind: str, path: str, detail: str, row: CatalogFile | None = None) -> None:
            found.append(Finding(kind, path, detail, project.id, row.id if row else None))

        if not root:
            finding("missing", project.current_path, "未找到可唯一确认身份的项目目录")
            for row in scan.files:
                where = safe_join(project.current_path, self._relative(scan, row))
                if row.status != "deleted" and where not in expected:
                    finding("missing", where, "项目目录无法可靠定位", row)
            return
        if root != project.current_path:
            finding("moved", root, f"原项目目录：{project.current_path}")
        occupied = {safe_join(root, self._relative(scan, row)) for row in scan.files if row.status != "deleted"}
        known.update(path for path in occupied if path not in expected or expected[path].completed_at is None)
        with self.auth.database.session() as session:
            data = archive.from_catalog(session, project)
            docs = {archive.PROJECT_FILE, *(row["archive_rel_path"] for row in data["products"])}
            known.update(safe_join(root, doc) for doc in docs | {MARKER, RECEIPT, "_工作台入库校验"})
            problems = archive.verify(session, project, fs, self.paths.join(Region.FORMAL, root))
        for issue in problems:
            finding("archive_broken", root, issue)
        if not problems and root != project.current_path:
            finding("archive_broken", root, "档案仍登记原项目位置，需按卡片柜修复")

        candidates: dict[int, list[str]] = defaultdict(list)
        for path, info in files.items():
            if path.startswith(root + "/") and path not in known and path not in expected:
                if path.split("/")[len(root.split("/"))] not in archive.PRODUCT_DIR.values():
                    candidates[info.size].append(path)
        moved: dict[str, list[CatalogFile]] = defaultdict(list)
        missing: list[tuple[CatalogFile, str, list[str]]] = []
        for row in scan.files:
            path = safe_join(root, self._relative(scan, row))
            original = safe_join(project.current_path, self._relative(scan, row))
            if row.status == "deleted":
                continue
            if path not in files and original in expected:
                continue
            info = files.get(path)
            if info is None:
                matches = []
                if row.fingerprint_kind == "full":
                    for candidate in candidates[row.size]:
                        try:
                            if self._hash(fs, candidate, files[candidate], cache) == row.fingerprint:
                                matches.append(candidate)
                        except NasNotFound:
                            continue
                missing.append((row, path, matches))
                for candidate in matches:
                    moved[candidate].append(row)
            elif info.size != row.size or abs(int(info.mtime * 1000) - row.mtime) > 1:
                probe = {"path": path, "size": info.size, "mtime": info.mtime, "sha": row.fingerprint}
                if row.inspection_probe == probe:
                    continue
                try:
                    actual = self._hash(fs, path, info, cache)
                except NasNotFound:
                    finding("missing", path, "巡检读取时文件位置发生变化", row)
                    continue
                if actual != row.fingerprint:
                    finding("modified", path, json.dumps({"size": info.size, "mtime": info.mtime,
                        "registered_sha256": row.fingerprint, "observed_sha256": actual}, ensure_ascii=False), row)
                else:
                    scan.probes[row.id] = probe
        for row, path, matches in missing:
            if len(matches) == 1 and len(moved[matches[0]]) == 1:
                actual = matches[0]
                scan.fixes[row.id] = actual.removeprefix(root + "/")
                known.add(actual)
                finding("moved", actual, f"原文件位置：{path}", row)
            else:
                finding("missing", path, "文件缺失或存在多个无法确认的候选", row)

    def _apply(self, session: Session, scans: dict[int, ProjectScan], active: set[int | None],
               findings: list[Finding]) -> None:
        current_active = set(session.scalars(select(Batch.project_id).where(Batch.lock_active.is_(True))))
        roots = set(session.scalars(select(Project.current_path)))
        report = {(r.project_id, r.file_id): r for r in findings if r.kind == "moved"}
        for identifier, scan in scans.items():
            old = scan.project
            fresh = session.get(Project, identifier)
            if (not scan.root or identifier in active | current_active or fresh is None
                    or (fresh.current_path, fresh.asset_version, fresh.status) != (old.current_path, old.asset_version, old.status)):
                continue
            if scan.root != old.current_path and scan.root in roots:
                continue
            if scan.root != old.current_path:
                roots.add(scan.root)
                fresh.current_path = scan.root
                report[identifier, None].auto_fixed = True
            original = {row.id: row for row in scan.files}
            if scan.fixes or scan.probes:
                for row in session.scalars(select(CatalogFile).where(CatalogFile.project_id == identifier)):
                    previous = original.get(row.id)
                    if previous is None or (row.physical_rel_path, row.fingerprint, row.status) != (
                            previous.physical_rel_path, previous.fingerprint, previous.status):
                        continue
                    if row.id in scan.fixes:
                        row.physical_rel_path = scan.fixes[row.id]
                        report[identifier, row.id].auto_fixed = True
                    elif row.id in scan.probes:
                        row.inspection_probe = scan.probes[row.id]

    def _scan_retired(self, fs: NasFs, retired: dict, files: dict[str, NasStat], dirs: set[str],
                      expected: dict[str, ExpectedDeletion], cache: dict[str, str], found: list[Finding], known: set[str]) -> None:
        for root, (data, published) in retired.items():
            if root not in dirs:
                # 保留的目录整体失踪时用一条记录报告，避免误指向为当前目录。
                if any(row["status"] != "deleted" and safe_join(root, physical(data, row)) not in expected for row in data["files"]):
                    found.append(Finding("missing", root, "已保留历史版本目录丢失", data["project"]["id"]))
                continue
            known.update(safe_join(root, path) for path in
                         {MARKER, RECEIPT, archive.PROJECT_FILE, *(row["archive_rel_path"] for row in data["products"])})
            cutoff = datetime.fromisoformat(published).timestamp() if published else float("inf")
            for row in data["files"]:
                path = safe_join(root, physical(data, row))
                if row["status"] == "deleted":
                    continue
                if path in expected:
                    if expected[path].completed_at is None:
                        known.add(path)
                    continue
                known.add(path)
                info = files.get(path)
                if info is None:
                    found.append(Finding("missing", path, "历史版本文件丢失", data["project"]["id"], row["id"]))
                elif info.size != row["size"] or info.mtime > cutoff:
                    try:
                        if self._hash(fs, path, info, cache) != row["fingerprint"]:
                            found.append(Finding("modified", path, "历史版本文件指纹异常", data["project"]["id"], row["id"]))
                    except NasNotFound:
                        found.append(Finding("missing", path, "历史文件在读取时变化", data["project"]["id"], row["id"]))

    def handle(self, context: JobContext) -> None:
        with self.auth.database.session() as session:
            run = session.scalar(select(InspectionRun).where(InspectionRun.job_id == context.job.id))
            if run.summary:
                # 报告已完成、队列尚未登记完成时，重启沿用已经持久化的结果。
                return
        with self.auth.database.session() as session:
            scans, ignored, active, expected, retired = self._snapshot(session)
        start = monotonic()
        fs = self.auth.pool.system()
        files, dirs = self._tree(fs, ignored)
        found, known, cache = [], set(), {}
        claimed = {scan.project.current_path for scan in scans.values()} | retired.keys()
        self._scan_retired(fs, retired, files, dirs, expected, cache, found, known)
        for scan in scans.values():
            root = self._root(fs, scan, dirs, files, claimed, cache)
            self._scan(fs, scan, root, files, expected, cache, found, known)
        owners = {root: data["project"]["id"] for root, (data, _) in retired.items()}
        owners.update({scan.root: scan.project.id for scan in scans.values() if scan.root})
        for path in sorted(files.keys() - known):
            # 预期消失只掩盖缺失，留在已完成清单里的同名新文件仍须报告。
            parent = path.rpartition("/")[0]
            while parent and parent not in owners:
                parent = parent.rpartition("/")[0]
            found.append(Finding("unregistered", path, "正式目录中的文件没有有效登记", owners.get(parent)))
        ancestors = set()
        for root in owners.keys() | ignored:
            prefix = root.rpartition("/")[0]
            while prefix:
                ancestors.add(prefix)
                prefix = prefix.rpartition("/")[0]
        filed = set()
        for path in files:
            prefix = path.rpartition("/")[0]
            while prefix and prefix not in filed:
                filed.add(prefix)
                prefix = prefix.rpartition("/")[0]
        for directory in sorted(dirs - filed - owners.keys() - ancestors):
            parts = directory.split("/")
            if (parts[0] in WON_FOLDERS.values() and len(parts) == 4) or (
                    parts[0] == "_已作废项目" and len(parts) == 2):
                found.append(Finding("unregistered", directory, "没有有效登记的项目目录"))
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            revisions = {row.id: (row.status, row.asset_version, row.current_path) for row in session.scalars(select(Project))}
            changed = {identifier for identifier, scan in scans.items() if revisions.get(identifier) != (
                scan.project.status, scan.project.asset_version, scan.project.current_path)}
            managed = {value[2] for value in revisions.values() if value[2]}
            for plan, batch in session.execute(select(PostopPlan, Batch).join(Batch)):
                managed.update((plan.target_path, plan.previous_path, safe_join(WORKSPACE, batch.batch_no)))
                if plan.purge:
                    managed.add(safe_join(WORKSPACE, "删除-" + plan.progress["nonce"]))
            recent = managed - (owners.keys() | ignored)
            found = [row for row in found if row.project_id not in changed and (
                row.kind != "unregistered" or not any(row.path == path or row.path.startswith(path + "/") for path in recent))]
            self._apply(session, {key: value for key, value in scans.items() if key not in changed}, active, found)
            run = session.scalar(select(InspectionRun).where(InspectionRun.job_id == context.job.id))
            for result in found:
                session.add(InspectionFinding(run_id=run.id, kind=result.kind, path=result.path,
                    project_id=result.project_id, file_id=result.file_id, auto_fixed=result.auto_fixed, detail=result.detail))
                if result.auto_fixed:
                    OplogService(session).write(None, "system", "巡检纠正实际位置",
                        "file" if result.file_id else "project", str(result.file_id or result.project_id),
                        result.detail, {"path": result.path}, "成功", "后台任务")
            run.summary = {"projects": len(scans), "files": len(files), "count": len(found),
                           "kinds": dict(Counter(result.kind for result in found)), "hashed_files": len(cache),
                           "auto_fixed": sum(result.auto_fixed for result in found),
                           "manual": sum(not result.auto_fixed for result in found),
                           "active_operations": len(active - {None}), "changed_during_scan": len(changed), "elapsed_ms": round(1000 * (monotonic() - start))}
            OplogService(session).write(None, "system", "完成正式资产巡检", "inspection", str(run.id),
                                        None, run.summary, "成功", "后台任务")

    @staticmethod
    def changed(session: Session, job: Job) -> None:
        run = session.scalar(select(InspectionRun).where(InspectionRun.job_id == job.id))
        if run is None:
            run = InspectionRun(job_id=job.id, started_at=utcnow(), status=job.status)
            session.add(run)
        run.status, run.error = job.status, job.error
        if job.started_at:
            run.started_at = job.started_at
        run.finished_at = job.finished_at
