"""上传状态机：所有文件操作用上传人的 NasFs，文件内容只经过有界分片。"""
from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from datetime import datetime, timezone, timedelta
from collections.abc import AsyncIterator

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import Batch, BatchFile, BatchProduct, PreflightAck, Project, User, utcnow
from app.errors import AppError
from app.schemas.upload import AiProgress, BatchProductResponse, BatchResponse, ConflictChoices, CreateBatchRequest, Issue, PrecheckRequest, PrecheckResponse, ResumeRequest, UploadFileResponse
from app.services.auth.service import AuthService, token_hash
from app.services.nas import NasFs, NasUnavailable, Region
from app.services.nas.factory import create_paths
from app.services.oplog import OplogService
from app.services.preflight import check, ignored
from .fingerprint import HashState, fingerprint
from app.services.thumbs import ThumbService
from app.services.ai.orchestrator import AiService, active_products
from app.services.batch import kind_label, open_batch
from . import supplement

UPLOADING = "上传中"
INTERRUPTED = "上传中断"
STAGED = "已暂存"
WITHDRAWN = "已撤回"
AI = "AI 处理中"


class UploadService:
    def __init__(self, auth: AuthService, thumbs: ThumbService | None = None, ai: AiService | None = None) -> None:
        self.auth = auth
        self.thumbs = thumbs
        self.ai = ai
        self.database = auth.database
        self.lock = auth.lock
        self.paths = create_paths(auth.config)
        self.chunk_bytes = auth.config.upload.chunk_mb * 1024 * 1024
        self._hashes: dict[tuple[int, int], tuple[int, HashState]] = {}
        self._pinned: set[int] = set()
        self._activity: dict[int, float] = {}
        self._stop = threading.Event()
        # 重启只能保留清单/偏移，不能假装仍有浏览器和 NAS 会话。
        with self.database.session() as session:
            for batch in session.scalars(select(Batch).where(Batch.status == UPLOADING)):
                batch.status = INTERRUPTED
                batch.payload = {**batch.payload, "queued": False}
                self._log(session, batch, "上传中断", "system", "服务重启")
        self._thread = threading.Thread(target=self._sweep, daemon=True, name="upload-idle-sweeper")
        self._thread.start()

    def _log(self, session: Session, batch: Batch, action: str, role: str, host: str) -> None:
        batch.updated_at = utcnow()
        if batch.status == UPLOADING and not batch.payload.get("queued"):
            self._activity[batch.id] = time.monotonic()
        OplogService(session).write(batch.uploader_id, role, action, "batch", str(batch.id), None,
                                   {"status": batch.status, "queued": bool(batch.payload.get("queued"))}, "成功", host)

    def _batch(self, session: Session, batch_id: int, user: User) -> Batch:
        batch = session.get(Batch, batch_id)
        if batch is None:
            raise AppError("batch_not_found", "批次不存在", 404)
        if batch.uploader_id != user.id:
            raise AppError("forbidden", "只能操作本人的上传批次", 403)
        current = session.get(User, user.id)
        if current is None or not current.enabled or not set(current.roles) & {"uploader", "first_reviewer", "final_reviewer"}:
            raise AppError("forbidden", "当前账号没有上传权限", 403)
        return batch

    def authorize(self, batch_id: int, token: str | None, cookie: str | None, host: str) -> User:
        if token:
            with self.lock, self.database.session() as session:
                batch = session.get(Batch, batch_id)
                expected = batch.payload.get("upload_token_hash") if batch else None
                if not isinstance(expected, str) or not secrets.compare_digest(expected, token_hash(token)):
                    # 最后完成响应丢失或取消与完成竞态时，只能回退到仍有效的本人登录会话。
                    if batch is not None and expected is None and cookie:
                        return self.auth.current(cookie, host)
                    raise AppError("upload_token_invalid", "上传令牌无效，请重新登录后继续上传", 401)
                user = session.get(User, batch.uploader_id)
                if user is None:
                    raise AppError("login_required", "请重新登录", 401)
                self._batch(session, batch_id, user)
                return user
        return self.auth.current(cookie, host)

    def precheck(self, body: PrecheckRequest, exclude_batch: int | None = None) -> PrecheckResponse:
        parts = body.folder_name.split("_")
        parts = parts if len(parts) == 3 else ["", "", ""]
        root_name = self.paths.root(Region.TEMP)
        # 浏览器只接收前缀字符数，不接收真实 NAS 路径。
        prefix = len(f"\\\\{self.auth.config.nas.host}\\{self.auth.config.nas.share}\\{root_name}\\B00000000-000000\\")
        check_folder = ("H0000-0000" + body.folder_name[1:]) if parts[0] == "H" else body.folder_name
        with self.database.session() as session:
            project = session.scalar(select(Project).where(Project.project_code == parts[0])) if parts[0] else None
            issues = check(check_folder, body.files, project is not None, prefix)
            if project is not None:
                # 编号已在库中：转入补充流程，只认编号。
                pending = open_batch(session, project, exclude_batch)
                if project.status != "active":
                    issues.insert(0, Issue(rule_code="P02", level="block", path=body.folder_name, message="该项目已作废或已删除，不能补充"))
                elif pending is not None:
                    issues.insert(0, Issue(rule_code="P02", level="block", path=body.folder_name,
                                           message=f"该项目有未完成的操作批次（{pending.batch_no}，{kind_label(pending)}），请等它结束或撤回后再补充"))
                elif parts[2] != project.project_name:
                    issues.insert(0, Issue(rule_code="P02", level="warn", path=body.folder_name,
                                           message=f"项目名与库中不一致（库中为「{project.project_name}」），将按立项编号补充到原项目"))
        return PrecheckResponse(project_code=parts[0], company_name=parts[1], project_name=parts[2],
                                code_status="exists" if project is not None else "need_h_code" if parts[0] == "H" else "new",
                                issues=issues, path_prefix_length=prefix)

    def _validate(self, body: PrecheckRequest, acks: list[str], exclude_batch: int | None = None) -> PrecheckResponse:
        result = self.precheck(body, exclude_batch)
        blocks = [issue.model_dump() for issue in result.issues if issue.level == "block"]
        if blocks:
            raise AppError("preflight_blocked", "预检未通过，请修改后重新拖入", 422, blocks)
        missing = {issue.rule_code for issue in result.issues if issue.level == "warn"} - set(acks)
        if missing:
            raise AppError("acks_required", "请逐类勾选我已知晓", 422, sorted(missing))
        return result

    def _snapshot(self, session: Session, batch: Batch, token: str | None = None,
                  differences: dict[str, list[str]] | None = None) -> BatchResponse:
        session.flush()
        products = active_products(session, batch.id)
        counts = None
        if batch.kind == "supplement":
            rows = [file for file in session.scalars(select(BatchFile).where(BatchFile.batch_id == batch.id)) if file.conflict and not file.removed]
            counts = {name: sum(file.conflict == name for file in rows) for name in ("new", "duplicate", "conflict")}
            counts["unresolved"] = len(supplement.unresolved(session, batch))
        return BatchResponse(kind=batch.kind, kind_label=kind_label(batch), project_id=batch.project_id, conflict_counts=counts,
            id=batch.id, batch_no=batch.batch_no, project_code=batch.project_code,
            folder_name=str(batch.payload["folder_name"]), original_folder_name=str(batch.payload["original_folder_name"]),
            status=batch.status, queued=bool(batch.payload.get("queued")), won=batch.won, updated_at=batch.updated_at,
            files=[UploadFileResponse.model_validate(file) for file in session.scalars(select(BatchFile).where(BatchFile.batch_id == batch.id).order_by(BatchFile.id))],
            directories=list(batch.payload.get("directories", [])), chunk_bytes=self.chunk_bytes,
            parallel_files=self.auth.config.upload.parallel_files, upload_token=token, differences=differences,
            owner_id=batch.owner_id, uploader_id=batch.uploader_id, recommended_path=batch.recommended_path,
            tag_labels={field["key"]: field["label"] for field in batch.payload.get("ai_fields", [])},
            products=[BatchProductResponse.model_validate(product) for product in products],
            ai_progress=AiProgress(items_total=sum(p.kind == "item" for p in products), items_done=sum(p.kind == "item" and p.ai_status == "done" for p in products),
                sets_total=sum(p.kind == "set" for p in products), sets_done=sum(p.kind == "set" and p.ai_status == "done" for p in products),
                failed=sum(p.ai_status == "failed" for p in products)))

    def view(self, batch_id: int, actor: User) -> BatchResponse:
        with self.lock, self.database.session() as session:
            batch = session.get(Batch, batch_id)
            if batch is None:
                raise AppError("batch_not_found", "批次不存在", 404)
            user = session.get(User, actor.id)
            if user is None or not user.enabled or not ("admin" in user.roles or user.id in (batch.owner_id, batch.uploader_id)
                or user.id == batch.first_reviewer_id and "first_reviewer" in user.roles
                or user.id == batch.final_reviewer_id and "final_reviewer" in user.roles):
                raise AppError("forbidden", "无权查看该批次", 403)
            return self._snapshot(session, batch)

    def retry_ai(self, batch_id: int, actor: User, host: str) -> BatchResponse:
        with self.lock, self.database.session() as session:
            batch = session.get(Batch, batch_id)
            if batch is None:
                raise AppError("batch_not_found", "批次不存在", 404)
            user = session.get(User, actor.id)
            if user is None or not user.enabled or not ("admin" in user.roles or user.id == batch.uploader_id and set(user.roles) & {"uploader", "first_reviewer", "final_reviewer"}):
                raise AppError("forbidden", "只有上传人或管理员可以重试 AI", 403)
            if batch.status not in (AI, "待核对", "AI 处理失败") or self.ai is None:
                raise AppError("invalid_batch_state", "该批次当前不能重试 AI", 409)
            self.ai.start_batch(session, batch, user, host)
            OplogService(session).write(user.id, ",".join(user.roles), "重试整批 AI", "batch", str(batch.id), None, {"status": AI}, "成功", host)
            return self._snapshot(session, batch)

    def _fs(self, batch: Batch) -> NasFs:
        return self.auth.pool.get(batch.uploader_id)

    def _root(self, batch: Batch) -> str:
        return self.paths.join(Region.TEMP, batch.batch_no, str(batch.payload["folder_name"]))

    def _file_path(self, batch: Batch, file: BatchFile) -> str:
        return self.paths.join(Region.TEMP, batch.batch_no, str(batch.payload["folder_name"]), file.rel_path)

    def _busy(self, session: Session, batch: Batch) -> bool:
        return any(other.id != batch.id and (not other.payload.get("queued") or other.id < batch.id) for other in
                   session.scalars(select(Batch).where(Batch.uploader_id == batch.uploader_id, Batch.status == UPLOADING)))

    def _activate(self, session: Session, batch: Batch, host: str) -> None:
        if batch.id not in self._pinned:
            self.auth.pool.pin(batch.uploader_id)
            self._pinned.add(batch.id)
        self._activity[batch.id] = time.monotonic()
        if self._busy(session, batch):
            if not batch.payload.get("queued"):
                batch.payload = {**batch.payload, "queued": True}
                self._log(session, batch, "排队等待上传", "uploader", host)
            return
        fs = self._fs(batch)
        root = self._root(batch)
        if not fs.exists(root):
            fs.mkdir(root, recursive=True)
        for path in batch.payload.get("directories", []):
            target = root + "/" + path
            if not fs.exists(target):
                fs.mkdir(target, recursive=True)
        batch.status = UPLOADING
        batch.payload = {**batch.payload, "queued": False}
        self._activity[batch.id] = time.monotonic()
        self._log(session, batch, "开始上传", "uploader", host)
        self._finish_if_ready(session, batch, host)

    def _release(self, batch: Batch) -> None:
        self._activity.pop(batch.id, None)
        if batch.id in self._pinned:
            self._pinned.remove(batch.id)
            self.auth.pool.unpin(batch.uploader_id)

    def _finish_if_ready(self, session: Session, batch: Batch, host: str) -> None:
        session.flush()
        unfinished = session.scalar(select(BatchFile.id).where(BatchFile.batch_id == batch.id, BatchFile.upload_status != "done").limit(1))
        if unfinished is None:
            batch.status = STAGED
            batch.payload = {**batch.payload, "upload_token_hash": None, "queued": False}
            self._log(session, batch, "上传已暂存", "uploader", host)
            if batch.kind == "supplement":
                counts = supplement.classify(session, batch)
                OplogService(session).write(batch.uploader_id, "uploader", "补充包逐文件判定", "batch", str(batch.id), None, counts, "成功", host)
            self._release(batch)

    def create(self, body: CreateBatchRequest, user: User, host: str) -> BatchResponse:
        with self.lock:
            result = self._validate(body, body.acks)
            with self.database.session() as session:
                code = result.project_code
                if code == "H":
                    year = body.historical_year or datetime.now(timezone(timedelta(hours=8))).year
                    pattern = re.compile(rf"H{year:04d}-(\d{{4}})$")
                    codes = list(session.scalars(select(Project.project_code))) + list(session.scalars(select(Batch.project_code)))
                    sequence = max([int(match[1]) for value in codes if (match := pattern.fullmatch(value))] or [0]) + 1
                    if sequence > 9999:
                        raise AppError("h_code_exhausted", "该年份历史编号已用完", 409)
                    code = f"H{year:04d}-{sequence:04d}"
                folder = f"{code}_{result.company_name}_{result.project_name}"
                project = session.scalar(select(Project).where(Project.project_code == code))
                if any(issue.level == "block" for issue in check(folder, body.files, project is not None)):
                    raise AppError("preflight_blocked", "新编号后的路径不符合预检要求", 422)
                if project is not None and (project.status != "active" or open_batch(session, project) is not None):
                    raise AppError("project_busy", "该项目已作废，或有未完成的操作批次，暂不能补充", 409)
                token = secrets.token_urlsafe(32)
                batch = Batch(batch_no=secrets.token_hex(16), project_code=code, kind="supplement" if project is not None else "ingest", status=UPLOADING,
                    project_id=project.id if project is not None else None,
                    owner_id=user.id, uploader_id=user.id, temp_path="", won=project.won if project is not None else body.won,
                    payload={"folder_name": folder, "original_folder_name": body.folder_name, "queued": False,
                             "directories": [item.rel_path for item in body.files if item.is_dir], "upload_token_hash": token_hash(token)})
                session.add(batch); session.flush()
                batch.batch_no = f"B{utcnow():%Y%m%d}-{batch.id:06d}"
                batch.temp_path = batch.batch_no + "/" + folder
                for item in body.files:
                    if not item.is_dir and not ignored(item):
                        session.add(BatchFile(batch_id=batch.id, rel_path=item.rel_path, size=item.size, mtime=item.mtime))
                for code_ack in sorted({issue.rule_code for issue in result.issues if issue.level == "warn"}):
                    session.add(PreflightAck(batch_id=batch.id, rule_code=code_ack))
                self._log(session, batch, "创建上传批次", ",".join(user.roles), host)
                try:
                    self._activate(session, batch, host)
                    response = self._snapshot(session, batch, token if batch.status != STAGED else None)
                except Exception:
                    self._release(batch)
                    raise
            return response

    def state(self, batch_id: int, user: User, host: str) -> BatchResponse:
        with self.lock, self.database.session() as session:
            batch = self._batch(session, batch_id, user)
            if batch.payload.get("queued"):
                self._activate(session, batch, host)
            if batch.status == UPLOADING:
                self._activity[batch.id] = time.monotonic()
            return self._snapshot(session, batch)

    def list(self, user: User) -> list[BatchResponse]:
        with self.lock, self.database.session() as session:
            return [self._snapshot(session, batch) for batch in session.scalars(select(Batch).where(or_(Batch.uploader_id == user.id, Batch.owner_id == user.id)).order_by(Batch.id.desc()))]

    def _receiving(self, session: Session, batch_id: int, user: User, file_id: int) -> tuple[Batch, BatchFile, NasFs]:
        batch = self._batch(session, batch_id, user)
        if batch.status != UPLOADING or batch.payload.get("queued"):
            raise AppError("batch_not_uploading", "批次当前不在上传中", 409)
        file = session.get(BatchFile, file_id)
        if file is None or file.batch_id != batch_id:
            raise AppError("file_not_found", "文件不存在于该批次", 404)
        if batch.id not in self._pinned:
            self.auth.pool.pin(batch.uploader_id)
            self._pinned.add(batch.id)
        self._activity[batch.id] = time.monotonic()
        return batch, file, self._fs(batch)

    async def read_body(self, stream: AsyncIterator[bytes]) -> bytes:
        data = bytearray()
        async for block in stream:
            if len(data) + len(block) > self.chunk_bytes:
                raise AppError("chunk_too_large", "分片超过配置大小", 413)
            data.extend(block)
        return bytes(data)

    def _repair(self, session: Session, batch: Batch, file: BatchFile, fs: NasFs, host: str) -> None:
        path = self._file_path(batch, file)
        actual = fs.stat(path).size if fs.exists(path) else 0
        expected = file.bytes_received
        if actual != expected:
            if actual < expected:
                file.bytes_received = min(actual // self.chunk_bytes * self.chunk_bytes, file.size)
                file.upload_status = "partial" if file.bytes_received else "pending"
                file.fingerprint = None
                file.thumb_status = None
                file.thumb_error = None
                if self.thumbs is not None:
                    self.thumbs.invalidate(batch.id, file.id)
            if actual:
                with fs.open_write(path, append=True) as stream:
                    stream.truncate(file.bytes_received)
            self._hashes.pop((batch.id, file.id), None)
            self._log(session, batch, "校正分片偏移", "uploader", host)

    def chunk(self, batch_id: int, file_id: int, index: int, data: bytes, user: User, host: str) -> dict[str, int]:
        with self.lock:
            conflict: int | None = None
            with self.database.session() as session:
                batch, file, fs = self._receiving(session, batch_id, user, file_id)
                self._repair(session, batch, file, fs, host)
                offset = index * self.chunk_bytes
                length = min(self.chunk_bytes, file.size - offset)
                if index < 0 or length <= 0 or len(data) != length:
                    raise AppError("invalid_chunk", "分片长度或序号不正确", 422)
                path = self._file_path(batch, file)
                if offset < file.bytes_received and offset + len(data) <= file.bytes_received:
                    with fs.open_read(path, offset=offset) as stream:
                        identical = stream.read(len(data)) == data
                    if identical:
                        return {"bytes_received": file.bytes_received}
                    conflict = file.bytes_received
                elif offset != file.bytes_received:
                    conflict = file.bytes_received
                else:
                    parent = path.rsplit("/", 1)[0]
                    if not fs.exists(parent):
                        fs.mkdir(parent, recursive=True)
                    with fs.open_write(path, append=True) as stream:
                        if stream.write(data) != len(data):
                            raise NasUnavailable("NAS 分片写入不完整，请按偏移重试")
                    cached = self._hashes.get((batch_id, file_id))
                    digest = cached[1] if cached and cached[0] == offset else hashlib.sha256() if offset == 0 else None
                    if digest is not None:
                        digest.update(data)
                        self._hashes[(batch_id, file_id)] = (offset + len(data), digest)
                    file.bytes_received += len(data)
                    file.upload_status = "partial"
                    self._log(session, batch, "接收上传分片", "uploader", host)
                received = file.bytes_received
            if conflict is not None:
                raise AppError("upload_offset_conflict", "分片偏移不一致", 409, {"bytes_received": conflict})
            return {"bytes_received": received}

    def complete(self, batch_id: int, file_id: int, user: User, host: str) -> BatchResponse:
        with self.lock, self.database.session() as session:
            batch = self._batch(session, batch_id, user)
            file = session.get(BatchFile, file_id)
            if file is None or file.batch_id != batch_id:
                raise AppError("file_not_found", "文件不存在于该批次", 404)
            if batch.status == STAGED and file.upload_status == "done":
                if self.thumbs is not None:
                    self.thumbs.enqueue(session, batch, file)
                return self._snapshot(session, batch)
            batch, file, fs = self._receiving(session, batch_id, user, file_id)
            if file.upload_status == "done":
                if self.thumbs is not None:
                    self.thumbs.enqueue(session, batch, file)
                return self._snapshot(session, batch)
            path = self._file_path(batch, file)
            if file.size == 0 and not fs.exists(path):
                parent = path.rsplit("/", 1)[0]
                if not fs.exists(parent):
                    fs.mkdir(parent, recursive=True)
                with fs.open_write(path):
                    pass
            if file.bytes_received != file.size or not fs.exists(path) or fs.stat(path).size != file.size:
                raise AppError("file_size_mismatch", "文件尚未完整到达，请继续上传", 409, {"bytes_received": file.bytes_received})
            cache = self._hashes.get((batch_id, file_id))
            digest, kind = fingerprint(fs, path, file.size, self.auth.config.upload.full_fingerprint_max_mb * 1024 * 1024,
                                       cache[1] if cache and cache[0] == file.size else None)
            file.fingerprint = digest
            file.upload_status = "done"
            batch.payload = {**batch.payload, "fingerprint_kinds": {**batch.payload.get("fingerprint_kinds", {}), str(file_id): kind}}
            self._hashes.pop((batch_id, file_id), None)
            self._log(session, batch, "文件上传完成", "uploader", host)
            if self.thumbs is not None:
                self.thumbs.enqueue(session, batch, file)
            self._finish_if_ready(session, batch, host)
            return self._snapshot(session, batch)

    def pause(self, batch_id: int, user: User, host: str) -> BatchResponse:
        with self.lock, self.database.session() as session:
            batch = self._batch(session, batch_id, user)
            if batch.status == UPLOADING:
                batch.status = INTERRUPTED
                batch.payload = {**batch.payload, "queued": False}
                self._log(session, batch, "暂停上传", "uploader", host)
                self._release(batch)
            return self._snapshot(session, batch)

    def cancel(self, batch_id: int, user: User, host: str) -> BatchResponse:
        with self.lock, self.database.session() as session:
            batch = self._batch(session, batch_id, user)
            if batch.status == WITHDRAWN:
                return self._snapshot(session, batch)
            if batch.status not in (UPLOADING, INTERRUPTED, STAGED, AI, "已驳回"):
                raise AppError("invalid_batch_state", "该状态不能取消上传", 409)
            fs = self._fs(batch)
            root = self.paths.join(Region.TEMP, batch.batch_no)
            if fs.exists(root):
                fs.delete(root, recursive=True)
            batch.status = WITHDRAWN
            if self.ai is not None:
                self.ai.cancel_jobs(session, batch, "批次已撤回", user.id, host)
            if self.thumbs is not None:
                self.thumbs.invalidate(batch.id)
            batch.payload = {**batch.payload, "upload_token_hash": None, "queued": False}
            self._log(session, batch, "取消上传", "uploader", host)
            self._release(batch)
            for key in tuple(self._hashes):
                if key[0] == batch.id:
                    self._hashes.pop(key)
            return self._snapshot(session, batch)

    def confirm(self, batch_id: int, user: User, host: str) -> BatchResponse:
        with self.lock, self.database.session() as session:
            batch = self._batch(session, batch_id, user)
            if batch.status != STAGED:
                raise AppError("invalid_batch_state", "只有已暂存批次可以确认", 409)
            if batch.kind == "supplement":
                pending = supplement.unresolved(session, batch)
                if pending:
                    raise AppError("conflicts_unresolved", "还有冲突文件没有选择「替换」或「另存为新文件」", 409,
                                   {"files": [file.rel_path for file in pending]})
            batch.status = AI
            if self.ai is not None:
                self.ai.start_batch(session, batch, user, host)
            self._log(session, batch, "确认开始 AI 处理", "uploader", host)
            return self._snapshot(session, batch)

    def set_conflicts(self, batch_id: int, body: ConflictChoices, user: User, host: str) -> BatchResponse:
        """冲突文件逐个选择替换或另存；另存时立即把临时区里的文件改名为「原名_v2」（已存在则递增）。"""
        with self.lock, self.database.session() as session:
            batch = self._batch(session, batch_id, user)
            if batch.kind != "supplement" or batch.status != STAGED:
                raise AppError("invalid_batch_state", "只有已暂存的补充批次可以处理冲突文件", 409)
            fs = self._fs(batch)
            originals = dict(batch.payload.get("conflict_originals", {}))
            for file_id, choice in body.choices.items():
                file = session.get(BatchFile, file_id)
                if file is None or file.batch_id != batch.id or file.conflict != "conflict":
                    raise AppError("file_not_found", "该文件不是本批次的冲突文件", 404)
                if file.conflict_choice == choice:
                    continue
                before = {"rel_path": file.rel_path, "choice": file.conflict_choice}
                original = originals.get(str(file.id), file.rel_path)
                if choice == "keep_both":
                    taken = set(supplement.catalog_files(session, batch.project_id)) | {
                        other.rel_path.casefold() for other in session.scalars(select(BatchFile).where(BatchFile.batch_id == batch.id)) if other.id != file.id}
                    target = supplement.versioned_name(original, taken)
                    originals[str(file.id)] = original
                else:
                    target = original
                    originals.pop(str(file.id), None)
                if target != file.rel_path:
                    fs.rename(self._file_path(batch, file), self.paths.join(Region.TEMP, batch.batch_no, str(batch.payload["folder_name"]), target))
                    file.rel_path = target
                    # 主图换了名字：旧缩略图作废，按新路径重新生成。
                    file.thumb_status, file.thumb_error = None, None
                    if self.thumbs is not None:
                        self.thumbs.invalidate(batch.id, file.id)
                        session.flush()
                        self.thumbs.enqueue(session, batch, file)
                file.conflict_choice = choice
                OplogService(session).write(user.id, ",".join(user.roles), "选择冲突文件处理方式", "batch_file", str(file.id), before,
                                           {"rel_path": file.rel_path, "choice": choice}, "成功", host)
            batch.payload = {**batch.payload, "conflict_originals": originals}
            return self._snapshot(session, batch)

    def resume(self, batch_id: int, body: ResumeRequest, user: User, host: str) -> BatchResponse:
        with self.lock, self.database.session() as session:
            batch = self._batch(session, batch_id, user)
            if batch.status not in (UPLOADING, INTERRUPTED):
                raise AppError("invalid_batch_state", "该批次不能继续上传", 409)
            if body.folder_name not in (batch.payload["folder_name"], batch.payload["original_folder_name"]):
                raise AppError("folder_mismatch", "请重新拖入同一个项目文件夹", 422)
            self._validate(body, body.acks, batch.id)
            old = {file.rel_path: file for file in session.scalars(select(BatchFile).where(BatchFile.batch_id == batch.id))}
            new = {file.rel_path: file for file in body.files if not file.is_dir and not ignored(file)}
            differences = {"added": sorted(new.keys() - old.keys()), "missing": sorted(old.keys() - new.keys()),
                           "changed": sorted(path for path in old.keys() & new.keys() if old[path].size != new[path].size or old[path].mtime != new[path].mtime),
                           "directories": [] if sorted(batch.payload.get("directories", [])) == sorted(item.rel_path for item in body.files if item.is_dir) else ["空目录或目录清单变化"]}
            if any(differences.values()) and not body.accept_changes:
                return self._snapshot(session, batch, differences=differences)
            fs = self._fs(batch)
            if any(differences.values()):
                for path in differences["missing"] + differences["changed"]:
                    target = self._file_path(batch, old[path])
                    if fs.exists(target):
                        fs.delete(target)
                    self._hashes.pop((batch.id, old[path].id), None)
                    if self.thumbs is not None:
                        self.thumbs.invalidate(batch.id, old[path].id)
                    if path in differences["missing"]:
                        session.delete(old.pop(path))
                    else:
                        old[path].size = new[path].size
                        old[path].mtime = new[path].mtime
                        old[path].bytes_received = 0
                        old[path].upload_status = "pending"
                        old[path].fingerprint = None
                        old[path].thumb_status = None
                        old[path].thumb_error = None
                for path in differences["added"]:
                    file = BatchFile(batch_id=batch.id, rel_path=path, size=new[path].size, mtime=new[path].mtime)
                    session.add(file)
                    old[path] = file
                directories = [item.rel_path for item in body.files if item.is_dir]
                for directory in sorted(batch.payload.get("directories", []), key=len, reverse=True):
                    if directory not in directories and not any(path.startswith(directory + "/") for path in new):
                        target = self._root(batch) + "/" + directory
                        if fs.exists(target):
                            fs.delete(target, recursive=True)
                batch.payload = {**batch.payload, "directories": directories}
            session.flush()
            for file in old.values():
                if file.upload_status != "done":
                    self._repair(session, batch, file, fs, host)
                elif not fs.exists(self._file_path(batch, file)) or fs.stat(self._file_path(batch, file)).size != file.size:
                    file.upload_status = "pending"; file.bytes_received = 0; file.fingerprint = None
                    file.thumb_status = None; file.thumb_error = None
                    if self.thumbs is not None:
                        self.thumbs.invalidate(batch.id, file.id)
            present_acks = set(session.scalars(select(PreflightAck.rule_code).where(PreflightAck.batch_id == batch.id)))
            for code in set(body.acks) - present_acks:
                if code in {"P02", "P07", "P08", "P09"}:
                    session.add(PreflightAck(batch_id=batch.id, rule_code=code))
            self._release(batch)
            token = secrets.token_urlsafe(32)
            batch.payload = {**batch.payload, "upload_token_hash": token_hash(token)}
            self._activate(session, batch, host)
            self._log(session, batch, "继续上传", "uploader", host)
            return self._snapshot(session, batch, token if batch.status != STAGED else None, differences)

    def _sweep(self) -> None:
        while not self._stop.wait(5):
            self.expire_idle()

    def expire_idle(self) -> None:
        with self.lock, self.database.session() as session:
            for batch_id, last in tuple(self._activity.items()):
                if time.monotonic() - last > 60:
                    batch = session.get(Batch, batch_id)
                    if batch and batch.status == UPLOADING:
                        batch.status = INTERRUPTED
                        batch.payload = {**batch.payload, "queued": False}
                        self._log(session, batch, "上传中断", "system", "上传请求静默超时")
                        self._release(batch)

    def shutdown(self) -> None:
        self._stop.set()
        self._thread.join()
        with self.lock:
            with self.database.session() as session:
                for batch in session.scalars(select(Batch).where(Batch.status == UPLOADING)):
                    if batch.id in self._pinned:
                        batch.status = INTERRUPTED
                        batch.payload = {**batch.payload, "queued": False}
                        self._log(session, batch, "上传中断", "system", "服务停止")
                        self._release(batch)
            self._hashes.clear()
