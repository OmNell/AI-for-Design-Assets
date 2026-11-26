"""从正式 NAS 档案恢复可证明的稳定身份，全程只读 NAS。"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from sqlalchemy import select

from app.config import Settings
from app.db.database import Database
from app.db.models import HCodeSequence, Project, Setting
from app.services import archive
from app.services.ai.field_catalog import FIXED_FIELDS
from app.services.ai.orchestrator import AiService
from app.services.ai.settings import AiSettingsService
from app.services.archive import publication
from app.services.auth.service import AuthService
from app.services.crypto import SecretCipher
from app.services.ingest import vectors
from app.services.jobs import JobQueue
from app.services.nas import NasFs, NasNotFound, Region, safe_join
from app.services.nas.factory import create_auth_backend, create_paths, create_session_pool
from app.services.oplog import OplogService
from app.services.thumbs import ThumbService
from app.services.thumbs.images import ImageDecodeError, render_variants
from app.services.upload.fingerprint import fingerprint

import re

UNRECOVERABLE = (
    "登录会话及凭据", "用户与角色授权", "完整审核轮次及意见", "操作日志及访问与搜索日志",
    "任务与调度历史", "候选及回滚模型向量", "完整的企业行业对照表及别名",
    "未在档案中留证的预期删除及作废生效时间", "无法证明的发布结论",
)


@dataclass
class Candidate:
    path: str
    code: str
    data: archive.Manifest | None
    verified: bool = False
    error: str | None = None

    def report(self) -> dict:
        return {
            "path": self.path, "code": self.code, "project_id": self.data["project"]["id"] if self.data else None,
            "version": self.version, "manifest_sha256": self.sha,
            "publication_receipt": self.verified, "error": self.error,
        }

    @property
    def sha(self) -> str | None:
        return publication.digest(self.data) if self.data else None

    @property
    def version(self) -> int | None:
        return self.data["project"].get("asset_version", 0) if self.data else None


def _project_directories(fs: NasFs, root: str, relative: str = "") -> Iterator[str]:
    skip = {"_工作台准备", archive.HISTORY_DIR, *archive.PRODUCT_DIR.values()}
    current = safe_join(root, relative) if relative else root
    for name in fs.list(current):
        if name in skip:
            continue
        path = safe_join(relative, name) if relative else name
        absolute = safe_join(root, path)
        if not fs.stat(absolute).is_dir:
            continue
        parts = path.split("/")
        project_name = len(name.split("_")) == 3 and bool(name.partition("_")[0])
        project_folder = project_name and (len(parts) >= 4 or parts[0] == "_已作废项目")
        if project_folder or fs.exists(safe_join(absolute, archive.PROJECT_FILE)):
            yield path
        else:
            yield from _project_directories(fs, root, path)


def _validate(data: archive.Manifest) -> None:
    archive.validate(data)
    products = {row["id"]: row for row in data["products"]}
    files = {row["id"]: row for row in data["files"]}
    tags = defaultdict(set)
    for row in data["tags"]:
        key = row["field_key"]
        confirmation = row["confirmation_status"]
        value = row["value"]
        fields = tags[row["product_id"]]
        if (key not in FIXED_FIELDS or key in fields or confirmation not in ("confirmed_value", "confirmed_unknown")
                or not isinstance(value, str) or (confirmation == "confirmed_value") != bool(value.strip())):
            raise archive.ArchiveError("固定字段、确认状态或标签值不一致")
        fields.add(key)
    if any(tags[identifier] != FIXED_FIELDS.keys() for identifier in products):
        raise archive.ArchiveError("商品缺少已确认的固定标签字段")
    if len({(r["set_product_id"], r["item_product_id"]) for r in data["relations"]}) != len(data["relations"]):
        raise archive.ArchiveError("套装关系重复")
    if (len({r["product_code"].casefold() for r in products.values()}) != len(products)
            or len({r["id"] for r in data["history"]}) != len(data["history"])):
        raise archive.ArchiveError("商品编号或历史记录重复")
    paths = set()
    for row in files.values():
        if row["status"] not in ("active", "voided", "replaced", "deleted"):
            raise archive.ArchiveError("文件状态无法证明")
    for row in products.values():
        if row["status"] not in ("active", "voided", "deleted") or (row["status"] == "active" and files[row["file_id"]]["status"] != "active"):
            raise archive.ArchiveError("商品状态与主图不一致")
    if data["project"].get("status", "active") not in ("active", "voided", "deleted"):
        raise archive.ArchiveError("项目正式状态无法确认")
    for row, relative in _files(data):
        key = relative.casefold()
        if key in paths:
            raise archive.ArchiveError("多个文件映射到同一物理路径")
        paths.add(key)


def _files(data: archive.Manifest) -> Iterator[tuple[dict, str]]:
    versions = {r["file_id"]: r for r in sorted(data["history"], key=lambda row: row["id"])}
    for row in data["files"]:
        history = versions.get(row["id"])
        if row["status"] == "deleted" or (history and history["expected_missing"]):
            continue
        yield row, row.get("physical_rel_path") or (history["new_rel_path"] if history else row["rel_path"])


def verify_files(fs: NasFs, root: str, data: archive.Manifest) -> None:
    for row, relative in _files(data):
        path = safe_join(root, relative)
        info = fs.stat(path)
        if info.is_dir or info.size != row["size"] or fingerprint(fs, path, row["size"]) != row["fingerprint"] or fs.stat(path) != info:
            raise archive.ArchiveError(f"原文件未通过完整 SHA-256 校验（文件 ID {row['id']}）")


def scan(fs: NasFs, root: str, selections: dict[str, dict] | None = None) -> tuple[dict, list[Candidate]]:
    candidates: dict[str, list[Candidate]] = defaultdict(list)
    review = selections or {}
    for path in _project_directories(fs, root):
        absolute = safe_join(root, path)
        guess = path.rsplit("/", 1)[-1].partition("_")[0]
        row = Candidate(path, guess, None)
        try:
            row.data = archive.parse_project(fs, absolute)
            row.code = row.data["project"]["project_code"]
            # 先保留身份和版本，SKU 档案损坏时仍可阻止回退到旧版本。
            row.data = archive.parse(fs, absolute)
            _validate(row.data)
            if guess.casefold() != row.code.casefold():
                raise archive.ArchiveError("目录编号与项目档案不一致")
            row.verified = publication.confirmed(fs, absolute, row.data, path)
        except (archive.ArchiveError, NasNotFound, ValueError, TypeError, KeyError) as exc:
            row.error = str(exc) if isinstance(exc, archive.ArchiveError) else "档案缺失或字段不合法"
        candidates[row.code.casefold()].append(row)
    selected, unresolved, used = [], [], set()
    for key, rows in sorted(candidates.items()):
        approved = review.get(key)
        proved = [row for row in rows if row.verified and not row.error]
        highest = max((row.version for row in proved), default=-1)
        if approved:
            used.add(key)
            matches = [r for r in rows if r.path == approved.get("path") and r.sha == approved.get("manifest_sha256")
                       and not r.error and r.data and r.version >= highest]
            chosen = matches[0] if len(matches) == 1 and str(approved.get("basis") or "").strip() else None
        else:
            matches = [r for r in proved if r.version == highest]
            chosen = matches[0] if len(matches) == 1 and all(
                r is matches[0] or r.version is not None and r.version < highest for r in rows) else None
        if chosen:
            try:
                verify_files(fs, safe_join(root, chosen.path), chosen.data)
            except (archive.ArchiveError, NasNotFound) as exc:
                chosen.error = str(exc)
                chosen = None
        if chosen:
            selected.append(chosen)
        else:
            unresolved.append({"project_code": key, "reason": "缺少唯一可用的已发布证据或文件核验失败",
                               "candidates": [row.report() for row in rows]})
    if set(review) - used:
        raise archive.ArchiveError("手动批准包含扫描中未出现的项目")
    identities: dict[tuple[str, object], list[Candidate]] = defaultdict(list)
    for entry in selected:
        data = entry.data
        identities["project", data["project"]["id"]].append(entry)
        for name in ("files", "products", "history"):
            for row in data[name]:
                identities[name, row["id"]].append(entry)
        for row in data["products"]:
            identities["product_code", row["product_code"].casefold()].append(entry)
        for row in data["links"]:
            identities["link", row["link_id"]].append(entry)
    conflict = {row.path for rows in identities.values() if len(rows) > 1 for row in rows}
    for row in selected:
        if row.path in conflict:
            unresolved.append({"project_code": row.code, "reason": "稳定 ID、商品编号或原短链接与另一项目冲突",
                               "candidates": [row.report()]})
    selected = [row for row in selected if row.path not in conflict]
    return {"selected": [row.report() for row in selected], "unresolved": unresolved,
            "scanned": sum(map(len, candidates.values())), "not_recoverable": list(UNRECOVERABLE)}, selected


def recovery_identity(selected: list[Candidate]) -> dict:
    return {str(row.data["project"]["id"]): {"path": row.path, "sha256": row.sha} for row in selected}


def rebuild(fs: NasFs, root: str, target: Path, selected: list[Candidate]) -> dict[str, int]:
    if not selected:
        raise ValueError("没有已验证的可恢复项目")
    target.mkdir(mode=0o700, parents=False, exist_ok=False)
    database = Database(target)
    totals = {key: 0 for key in ("projects", "files", "products", "tags", "relations", "history", "links")}
    try:
        database.migrate()
        with database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            for row in selected:
                absolute = safe_join(root, row.path)
                data = archive.parse(fs, absolute)
                if publication.digest(data) != row.sha:
                    raise archive.ArchiveError("档案在验证后变化，请重新生成恢复计划")
                if row.verified and not publication.confirmed(fs, absolute, data, row.path):
                    raise archive.ArchiveError("正式发布凭据在验证后变化")
                verify_files(fs, absolute, data)
                project = archive.register(session, data, current_path=row.path)
                if match := re.fullmatch(r"H(\d{4})-(\d+)", project.project_code):
                    year, sequence = map(int, match.groups())
                    counter = session.get(HCodeSequence, year)
                    if counter:
                        counter.last_sequence = max(counter.last_sequence, sequence)
                    else:
                        session.add(HCodeSequence(year=year, last_sequence=sequence))
                totals["projects"] += 1
                for name in totals.keys() - {"projects"}:
                    totals[name] += len(data[name])
            session.add(Setting(key="archive_recovery", value=recovery_identity(selected), encrypted=False))
            if session.connection().exec_driver_sql("PRAGMA foreign_key_check").all():
                raise archive.ArchiveError("恢复后外键完整性检查失败")
        with database.engine.connect() as connection:
            if connection.exec_driver_sql("PRAGMA integrity_check").scalar() != "ok":
                raise archive.ArchiveError("恢复后 SQLite 完整性检查失败")
        return totals
    finally:
        database.close()


def rebuild_auxiliary(config: Settings, directory: Path, fs: NasFs, root: str,
                      selected: list[Candidate]) -> dict:
    """只在新恢复库生成派生数据，用既有正式向量门禁控制公开。"""
    work = config.model_copy(deep=True)
    work.paths.data_dir = directory
    database = Database(directory)
    pool = create_session_pool(work)
    cipher = (SecretCipher(directory) if work.mode == "dev"
              else SecretCipher(config.paths.data_dir, read_only=True))
    auth = AuthService(database, pool, create_auth_backend(work), work)
    queue = JobQueue(database, pool)
    ai = AiService(auth, queue, AiSettingsService(auth, cipher), cipher, ThumbService(auth, queue))
    result: dict = {"active": [], "blocked": [], "thumbnails_failed": []}
    try:
        with database.session() as session:
            origin = session.scalar(select(Setting).where(Setting.key == "archive_recovery"))
            if origin is None or origin.value != recovery_identity(selected):
                raise ValueError("目标并非同一份已核验计划生成的隔离库")
            model = vectors.model_for(ai, session)
        for candidate in selected:
            data = candidate.data
            project_id = data["project"]["id"]
            if data["project"].get("status", "active") != "active":
                continue
            files = {row["id"]: (row, path) for row, path in _files(data) if row["status"] == "active"}
            products = [row for row in data["products"] if row["status"] == "active" and row["file_id"] in files]
            with database.session() as session:
                current = session.get(Project, project_id)
                if current and current.status == "active":
                    result["active"].append(project_id)
                    continue
                inputs = vectors.project_inputs(ai, session, {**data, "products": products})
            try:
                if inputs:
                    vectors.generate(ai, None, model, inputs)
                absolute = safe_join(root, candidate.path)
                verify_files(fs, absolute, data)
                for row in products:
                    f, relative = files[row["file_id"]]
                    try:
                        with fs.open_read(safe_join(absolute, relative)) as stream:
                            render_variants(stream, directory / "thumbs" / "catalog" / str(f["id"]) / f["fingerprint"])
                    except ImageDecodeError:
                        result["thumbnails_failed"].append(f["id"])
                with database.session() as session:
                    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                    if inputs:
                        vectors.publish_gate(ai, session, model, inputs)
                    project = session.get(Project, project_id)
                    if project.status != "ingesting":
                        raise ValueError("该项目不在隔离恢复状态")
                    project.status = "active"
                    OplogService(session).write(None, "system", "灾难恢复向量核验通过", "project", str(project_id),
                                                None, {"model": model.name, "items": len(inputs)}, "成功", "离线恢复")
                result["active"].append(project_id)
            except Exception as exc:
                result["blocked"].append({"project_id": project_id, "error": type(exc).__name__})
        return result
    finally:
        ai.close_clients()
        pool.shutdown()
        database.close()
