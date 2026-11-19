"""已提交版本的 NAS 恢复凭据。凭据只能在 SQLite 发布提交后写入。"""
from __future__ import annotations

import hashlib
import json

from app.db.models import Batch, Project, User, UserSession
from app.errors import AppError
from app.services import archive
from app.services.auth.service import AuthService, LoginContext
from app.services.jobs import JobContext
from app.services.nas import NasFs, safe_join
from app.services.oplog import OplogService
from app.services.upload.service import UploadService

RECEIPT = f"{archive.HISTORY_DIR}/_发布凭据.json"


def digest(data: archive.Manifest) -> str:
    return hashlib.sha256(json.dumps(archive._canonical(data), ensure_ascii=False,
                                     sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def receipt(data: archive.Manifest) -> dict:
    project = data["project"]
    return {"format": 1, "project_id": project["id"], "project_code": project["project_code"],
            "asset_version": project.get("asset_version", 0), "root": project["current_path"],
            "manifest_sha256": digest(data)}


def confirmed(fs: NasFs, root: str, data: archive.Manifest, relative: str) -> bool:
    path = safe_join(root, RECEIPT)
    if data["project"]["current_path"] != relative or not fs.exists(path) or fs.stat(path).size > 8192:
        return False
    try:
        return json.loads(archive._read(fs, path)) == receipt(data)
    except (UnicodeError, json.JSONDecodeError):
        return False


def seal(auth: AuthService, context: JobContext, batch_id: int, root: str) -> None:
    """凭据失败不回退已提交版本，恢复工具会将缺证据的版本留待核实。"""
    job = context.job
    with auth.lock, auth.database.session() as session:
        batch = session.get(Batch, batch_id)
        user = session.get(User, job.run_as_user_id) if job.run_as_user_id else None
        login = session.get(UserSession, job.run_as_session_id) if job.run_as_session_id else None
        if not batch or batch.status != "已入库" or not user or user.id != batch.final_reviewer_id:
            return
        project = session.get(Project, batch.project_id)
        try:
            if not login or not user.enabled or "final_reviewer" not in user.roles or context.fs is None:
                raise AppError("publication_receipt_unavailable", "终审会话或角色已失效", 409)
            UploadService._check_login(session, LoginContext(user, login.id))
            if auth.pool.get(login.id) is not context.fs:
                raise AppError("publication_receipt_unavailable", "终审 NAS 会话已失效", 409)
            data = archive.from_catalog(session, project)
            if archive.verify(session, project, context.fs, root):
                raise archive.ArchiveError("档案与正式版本不一致")
            content = json.dumps(receipt(data), ensure_ascii=False, sort_keys=True) + "\n"
            archive.write(context.fs, root, {RECEIPT: content}, replace=batch.kind == "archive_repair")
        except Exception as exc:
            OplogService(session).write(user.id, "final_reviewer", "恢复发布凭据待核实", "batch",
                str(batch_id), None, {"异常类型": type(exc).__name__}, "异常", "后台发布")
            return
        OplogService(session).write(user.id, "final_reviewer", "登记恢复发布凭据", "batch",
            str(batch_id), None, {"asset_version": project.asset_version}, "成功", "后台发布")
