"""核对正式资产与本地卡片柜，按项目原子恢复有发布凭据的缺失或更新数据。"""
from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.database import Database
from app.db.models import Batch, CatalogFile, HistoryVersion, IngestPlan, Link, PostopPlan, Product, Project, Setting, User
from app.errors import AppError
from app.services import archive
from app.services.ai.client import AiCallError
from app.services.ai.orchestrator import AiService
from app.services.archive import publication
from app.services.crypto import CryptoError
from app.services.ingest import vectors
from app.services.jobs import JobFailure
from app.services.nas import NasFs, NasNotFound, NasPermissionDenied, NasSessionExpired, NasUnavailable, safe_join
from app.services.oplog import OplogService
from app.services.postops import embeddings, manifest

from . import Candidate, reserve_identities, scan, verify_files

RECOVERABLE = frozenset({"missing", "incomplete", "newer"})
ROW_KEYS = {
    "files": ("id",), "products": ("id",), "tags": ("product_id", "field_key"),
    "relations": ("set_product_id", "item_product_id"), "history": ("id",), "links": ("kind", "target_id"),
}
IDENTITY_MODELS = {"files": CatalogFile, "products": Product, "history": HistoryVersion}


def _rows(data: archive.Manifest, name: str) -> dict[tuple, dict]:
    return {tuple(row[key] for key in ROW_KEYS[name]): row for row in data[name]}


def _subset(local: archive.Manifest, source: archive.Manifest) -> bool:
    if local["project"] != source["project"]:
        return False
    for name in ROW_KEYS:
        remote = _rows(source, name)
        if any(remote.get(key) != row for key, row in _rows(local, name).items()):
            return False
    return True


def _stable_lineage(before: archive.Manifest, after: archive.Manifest) -> bool:
    """新版本必须保留旧对象；版本变化可以改变文件所在目录、主图和标签。"""
    immutable = {"files": ("size", "fingerprint", "fingerprint_kind"),
                 "products": ("product_code", "kind"), "links": ("link_id",),
                 "history": ("file_id", "product_id", "old_fingerprint", "old_rel_path", "new_rel_path", "reason")}
    if before["project"]["id"] != after["project"]["id"] or before["project"]["project_code"] != after["project"]["project_code"]:
        return False
    if before["project"].get("status") == "deleted" and after["project"].get("status") != "deleted":
        return False
    for name, fields in immutable.items():
        current = _rows(after, name)
        for key, old in _rows(before, name).items():
            newer = current.get(key)
            if newer is None or any(old[field] != newer[field] for field in fields):
                return False
            if old.get("status") == "deleted" and newer.get("status") != "deleted":
                return False
    return all(_rows(before, name).keys() <= _rows(after, name).keys() for name in ("relations", "tags"))


def _identities(data: archive.Manifest) -> dict[str, set[int]]:
    return {"project": {data["project"]["id"]}, **{
        name: {row["id"] for row in data[name]} for name in IDENTITY_MODELS}}


def _conflict(session: Session, data: archive.Manifest, local: Project | None) -> str | None:
    project = data["project"]
    pid, code, path = project["id"], project["project_code"], project["current_path"]
    if session.scalar(select(Batch.id).where(Batch.lock_active.is_(True),
            or_(Batch.project_id == pid, func.lower(Batch.project_code) == code.lower())).limit(1)) is not None:
        return "此项目或编号已有未完成业务批次，保留原批次"
    if session.scalar(select(Project.id).where(Project.id != pid,
            or_(func.lower(Project.project_code) == code.lower(), func.lower(Project.current_path) == path.lower())).limit(1)) is not None:
        return "项目编号或正式路径已被其他项目登记"
    identities = _identities(data)
    all_ids = set.union(*identities.values())
    if sum(len(ids) for ids in identities.values()) != len(all_ids):
        return "档案中项目、商品、文件或历史记录的稳定 ID 重复"
    if local is not None and local.project_code != code:
        return "项目稳定 ID 已被其他项目使用"
    for name, model in IDENTITY_MODELS.items():
        own = identities[name]
        if session.scalar(select(model.id).where(model.id.in_(all_ids),
                or_(model.project_id != pid, model.id.not_in(own))).limit(1)) is not None:
            return "文件、商品或历史记录的稳定 ID 已占用"
    if session.scalar(select(Project.id).where(Project.id.in_(all_ids - {pid})).limit(1)) is not None:
        return "档案对象稳定 ID 已被其他项目使用"
    for row in data["products"]:
        if session.scalar(select(Product.id).where(func.lower(Product.product_code) == row["product_code"].lower(),
                Product.id != row["id"]).limit(1)) is not None:
            return "商品编号已被其他商品使用"
    for row in data["links"]:
        mapped = session.scalar(select(Link).where(Link.link_id == row["link_id"]))
        if mapped is not None and (mapped.kind, mapped.target_id) != (row["kind"], row["target_id"]):
            return "原永久链接已经登记给其他对象"
    for model in (IngestPlan, PostopPlan):
        for pending in session.scalars(select(model).join(Batch, Batch.id == model.batch_id).where(Batch.lock_active.is_(True))):
            reserved = _identities(pending.manifest)
            if all_ids & set.union(*reserved.values()):
                return "稳定 ID 已被其他在途业务预留"
    return None


def _local_snapshot(session: Session, data: archive.Manifest) -> tuple[Project | None, archive.Manifest | None, str | None]:
    project = session.get(Project, data["project"]["id"])
    if project is None:
        return None, None, None
    current = archive.from_catalog(session, project)
    stamp = sha256(f"{project.status}:{publication.digest(current)}".encode()).hexdigest()
    return project, current, stamp


def _prove_prior(fs: NasFs, root: str, current: archive.Manifest, target: archive.Manifest) -> bool:
    path = current["project"]["current_path"]
    try:
        absolute = safe_join(root, path)
        older = archive.parse(fs, absolute)
        return publication.confirmed(fs, absolute, older, path) and _subset(current, older) and _stable_lineage(older, target)
    except (archive.ArchiveError, NasNotFound, NasPermissionDenied):
        return False


def _classify(session: Session, candidate: Candidate, fs: NasFs, root: str) -> dict:
    data = candidate.data
    local, current, baseline = _local_snapshot(session, data)
    item = {**candidate.report(), "baseline": baseline}
    if reason := _conflict(session, data, local):
        return {**item, "kind": "conflict", "reason": reason}
    if local is None:
        return {**item, "kind": "missing", "reason": "本地尚未登记此项目"}
    if local.status not in {"active", "voided", "deleted"}:
        return {**item, "kind": "conflict", "reason": "本地项目处于未发布状态，请先核实原流程或继续原隔离恢复"}
    if local.asset_version == candidate.version:
        if local.status == data["project"].get("status", "active") and _subset(current, data):
            same = publication.digest(current) == candidate.sha
            return {**item, "kind": "same" if same else "incomplete",
                    "reason": "本地资产与已发布档案一致" if same else "本地部分记录缺失，其余记录与档案一致"}
        return {**item, "kind": "conflict", "reason": "同版本的本地记录与 NAS 档案不一致"}
    if local.asset_version < candidate.version:
        if _prove_prior(fs, root, current, data):
            return {**item, "kind": "newer", "reason": "NAS 有较新版本，已核实原目录凭据和稳定身份"}
        return {**item, "kind": "conflict", "reason": "NAS 版本较新，但无法核实本地原版本与新版本的连续性"}
    return {**item, "kind": "conflict", "reason": "本地版本较新，禁止从较旧档案覆盖"}


def plan_incremental(database: Database, fs: NasFs, root: str) -> dict:
    report, candidates = scan(fs, root)
    with database.session() as session:
        items = [_classify(session, row, fs, root) for row in candidates]
        seen = {row["code"].casefold() for row in items} | {row["project_code"].casefold() for row in report["unresolved"]}
        for local in session.scalars(select(Project).order_by(Project.id)):
            if local.project_code.casefold() not in seen:
                items.append({"kind": "local_only", "code": local.project_code, "project_id": local.id, "version": local.asset_version,
                              "path": local.current_path, "reason": "本轮未找到 NAS 对应档案，保留本地记录"})
    return {"items": items, "scanned": report["scanned"], "unresolved": report["unresolved"],
            "inaccessible": report["inaccessible"], "not_recoverable": report["not_recoverable"]}


class _VectorCheckpoint:
    def __init__(self, database: Database, manifest_sha: str) -> None:
        self.database = database
        self.prefix = f"archive_restore_vector:{manifest_sha}:"

    def load(self) -> dict:
        prepared: dict = {}
        with self.database.session() as session:
            for row in session.scalars(select(Setting).where(Setting.key.startswith(self.prefix, autoescape=True))):
                generation, version, pid = row.key.removeprefix(self.prefix).split(":")
                item = prepared.setdefault(f"{generation}:{version}", {"version": version, "items": {}})
                item["items"][pid] = row.value
        return prepared

    def save(self, model: vectors.Model, product_id: int, payload: dict) -> None:
        key = f"{self.prefix}{model.generation}:{model.version}:{product_id}"
        with self.database.session() as session:
            row = session.scalar(select(Setting).where(Setting.key == key))
            if row is None:
                session.add(Setting(key=key, value=payload, encrypted=False))
            else:
                row.value = payload

    def clear(self, session: Session) -> None:
        for row in session.scalars(select(Setting).where(Setting.key.startswith(self.prefix, autoescape=True))):
            session.delete(row)


def _confirm(fs: NasFs, root: str, candidate: Candidate) -> None:
    absolute = safe_join(root, candidate.path)
    current = archive.parse(fs, absolute)
    if publication.digest(current) != candidate.sha or not publication.confirmed(fs, absolute, current, candidate.path):
        raise archive.ArchiveError("恢复计划生成后，档案内容或正式发布凭据已变化")
    verify_files(fs, absolute, current)


def _with_progress(callback: Callable[[dict], None] | None, phase: str, item: dict, **details: object) -> None:
    if callback is not None:
        callback({"phase": phase, "project_id": item["project_id"], "code": item["code"], **details})


def apply_incremental(ai: AiService, fs: NasFs, root: str, plan: dict, *, actor_id: int | None = None,
                      host: str = "后台恢复", on_progress: Callable[[dict], None] | None = None,
                      on_publish: Callable[[Session], None] | None = None) -> dict:
    """每个项目先验证当前版本，再原子写入卡片柜及必需向量。"""
    database = ai.auth.database
    report, candidates = scan(fs, root, verify_contents=False)
    current = {row.data["project"]["id"]: row for row in candidates}
    result: dict = {"recovered": [], "skipped": [], "blocked": [], "unresolved": report["unresolved"],
                    "inaccessible": report["inaccessible"]}
    for item in plan["items"]:
        if item["kind"] not in RECOVERABLE:
            continue
        pid = item["project_id"]
        candidate = current.get(pid)
        try:
            if candidate is None or candidate.sha != item["manifest_sha256"] or candidate.path != item["path"]:
                raise archive.ArchiveError("当前正式发布版本与恢复计划不同，请重新扫描")
            with database.session() as session:
                now = _classify(session, candidate, fs, root)
                if now["kind"] == "same":
                    result["skipped"].append(pid)
                    continue
                if now["kind"] != item["kind"] or now["baseline"] != item["baseline"]:
                    raise archive.ArchiveError(now["reason"] if now["kind"] == "conflict" else "本地数据已变化，请重新扫描")
                texts = embeddings.inputs(ai, session, candidate.data)
            _with_progress(on_progress, "indexing", item, products=len(texts))
            checkpoint = _VectorCheckpoint(database, candidate.sha)
            staged = embeddings.prepare(ai, candidate.data, None, checkpoint.load(), checkpoint=checkpoint.save) if texts else None
            _with_progress(on_progress, "validating", item)
            _confirm(fs, root, candidate)
            with ai.auth.lock, database.session() as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                if actor_id is not None:
                    operator = session.get(User, actor_id)
                    if operator is None or not operator.enabled or "admin" not in operator.roles:
                        raise AppError("recovery_actor_disabled", "发起管理员的权限已失效", 403)
                project, _, baseline = _local_snapshot(session, candidate.data)
                if baseline != item["baseline"]:
                    raise archive.ArchiveError("恢复时本地数据已变化，请重新扫描")
                if reason := _conflict(session, candidate.data, project):
                    raise archive.ArchiveError(reason)
                _apply_catalog(session, project, candidate.data)
                reserve_identities(session, candidate.data)
                if staged is not None:
                    embeddings.publish(ai, session, candidate.data, None, staged)
                checkpoint.clear(session)
                OplogService(session).write(actor_id, "admin" if actor_id is not None else "system", "从 NAS 正式档案恢复资产",
                    "project", str(pid), None, {"asset_version": candidate.version, "manifest_sha256": candidate.sha}, "成功", host)
                if on_publish is not None:
                    on_publish(session)
            result["recovered"].append(pid)
            _with_progress(on_progress, "project_complete", item)
        except (archive.ArchiveError, AppError, JobFailure, AiCallError) as exc:
            result["blocked"].append({"project_id": pid, "reason": str(exc) if isinstance(exc, archive.ArchiveError) else exc.message})
        except (NasNotFound, NasPermissionDenied, NasSessionExpired, NasUnavailable, IntegrityError, CryptoError) as exc:
            result["blocked"].append({"project_id": pid, "reason": _failure_reason(exc)})
    return result


def _apply_catalog(session: Session, project: Project | None, data: archive.Manifest) -> None:
    if project is None:
        project = archive.register(session, data)
        project.status = data["project"].get("status", "active")
        if not archive.matches_catalog(session, project, data):
            raise archive.ArchiveError("新登记的正式资产与 NAS 档案不一致")
    else:
        manifest.apply_catalog(session, project, data, batch_id=None)


def _failure_reason(error: Exception) -> str:
    if isinstance(error, NasNotFound):
        return "复核时 NAS 档案或正式文件已不存在"
    if isinstance(error, NasPermissionDenied):
        return "系统账号无权读取所需档案或文件"
    if isinstance(error, (NasSessionExpired, NasUnavailable)):
        return "NAS 连接中断，当前项目未提交"
    if isinstance(error, CryptoError):
        return "现有加密配置无法解密，请核对原密钥或重新配置"
    return "SQLite 约束未通过，当前项目未提交"
