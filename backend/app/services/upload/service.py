"""上传状态机：所有文件操作用上传人的 NasFs，文件内容只经过有界分片。"""
from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager

from sqlalchemy import func, or_, select, true
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app.db.models import Batch, BatchFile, BatchProduct, HCodeSequence, PreflightAck, Project, User, UserSession, utcnow
from app.errors import AppError
from app.schemas.upload import AiProgress, Issue, BatchProductResponse, BatchResponse, CreateBatchRequest, PrecheckRequest, PrecheckResponse, ResumeRequest, UploadFileResponse
from app.services.auth.service import AuthService, LoginContext, is_expired, token_hash
from app.services.nas import NasFs, NasNotFound, NasSessionExpired, NasUnavailable, Region
from app.services.nas.factory import create_paths
from app.services.oplog import OplogService
from app.services.preflight import check, ignored
from app.services.postops.supplement import active_batch_id, catalog_files, classify_file, project_for_supplement
from .fingerprint import HashState, fingerprint
from app.services.thumbs import ThumbService, is_main_image
from app.services.ai.orchestrator import AiService, active_products
from app.services.ai.vision import has_visual_evidence

UPLOADING = "上传中"
INTERRUPTED = "上传中断"
STAGED = "已暂存"
TERMINATED = "已终止"
TERMINATABLE = {UPLOADING, INTERRUPTED, STAGED, "AI 处理中", "AI 处理失败", "待核对", "已驳回", "待初审", "待终审"}
H_CODE = re.compile(r"H([0-9]{4})-([0-9]{4})")
AI = "AI 处理中"


class UploadService:
    def __init__(self, auth: AuthService, thumbs: ThumbService | None = None, ai: AiService | None = None) -> None:
        self.auth = auth
        self.thumbs = thumbs
        self.ai = ai
        self.database = auth.database
        self.lock = auth.lock
        self.chunk_bytes = auth.config.upload.chunk_mb * 1024 * 1024
        self.paths = create_paths(auth.config)
        self._hashes: dict[tuple[int, int], tuple[int, HashState]] = {}
        self._pinned: dict[int, int] = {}
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

    def authorize(self, batch_id: int, token: str | None, cookie: str | None, host: str) -> LoginContext:
        login = self.auth.context(cookie, host)
        with self.lock, self.database.session() as session:
            batch = self._batch(session, batch_id, login.user)
            self._check_login(session, login, batch)
            expected = batch.payload.get("upload_token_hash")
            if token is not None and expected is not None and not secrets.compare_digest(expected, token_hash(token)):
                raise AppError("upload_token_invalid", "上传凭据已变更，请重新继续上传", 409)
        return login

    @staticmethod
    def _check_login(session: Session, login: LoginContext, batch: Batch | None = None) -> None:
        row = session.get(UserSession, login.session_id)
        if row is None or row.user_id != login.user.id or row.revoked_at is not None or is_expired(row.expires_at):
            raise AppError("session_expired", "登录已过期，请重新登录", 401)
        if batch is not None and batch.upload_session_id != login.session_id:
            raise AppError("upload_session_mismatch", "上传绑定到其他登录会话，请重新选择文件夹继续上传", 409)

    def precheck(self, body: PrecheckRequest, resume_id: int | None = None,
                 project: Project | None = None) -> PrecheckResponse:
        parts = body.folder_name.split("_")
        parts = parts if len(parts) == 3 else ["", "", ""]
        if project is None and resume_id is not None:
            with self.database.session() as session:
                previous = session.get(Batch, resume_id)
                if previous is not None and previous.kind == "supplement":
                    project = project_for_supplement(session, previous.project_id)
        if project is None:
            with self.database.session() as session:
                project_id = session.scalar(select(Project.id).where(Project.project_code == parts[0]))
                exists = project_id is not None
                busy = self._active_ingest(session, parts[0], resume_id)
        else:
            exists, busy = False, False
            project_id = project.id
        root_name = self.paths.root(Region.TEMP)
        # 浏览器只接收前缀字符数，不接收真实 NAS 路径。
        prefix = len(f"\\\\{self.auth.config.nas.host}\\{self.auth.config.nas.share}\\{root_name}\\B00000000-000000\\")
        check_folder = ("H0000-0000" + body.folder_name[1:]) if parts[0] == "H" else body.folder_name
        issues = check(check_folder, body.files, exists, prefix)
        if project is not None:
            issues = [issue for issue in issues if issue.rule_code not in {"P07", "P08", "P09"}]
            known = {(issue.rule_code, issue.path) for issue in issues}
            issues.extend(issue for issue in check(project.folder_name, body.files, prefix_length=prefix)
                          if issue.level == "block" and issue.rule_code not in {"P07", "P08", "P09"}
                          and (issue.rule_code, issue.path) not in known)
            if parts[0] != project.project_code:
                issues.append(Issue(rule_code="project_mismatch", level="block", path=body.folder_name,
                                    message="文件夹中的立项编号与选择的正式项目不一致"))
            if not any(not item.is_dir and not ignored(item) for item in body.files):
                issues.append(Issue(rule_code="empty_supplement", level="block", path=body.folder_name,
                                    message="请选择至少一个要补充的文件"))
            parts = [project.project_code, project.company_name, project.project_name]
        if busy and not exists:
            issues.append(Issue(rule_code="P02", level="block", path=body.folder_name, message="此立项编号已有进行中的首次入库批次"))
        return PrecheckResponse(project_code=parts[0], company_name=parts[1], project_name=parts[2],
                                code_status="exists" if exists else "need_h_code" if parts[0] == "H" else "new",
                                issues=issues, path_prefix_length=prefix, project_id=project_id)

    def _validate(self, body: PrecheckRequest, acks: list[str], resume_id: int | None = None,
                  project: Project | None = None) -> PrecheckResponse:
        result = self.precheck(body, resume_id, project)
        if result.code_status == "new" and any(issue.rule_code == "P02" for issue in result.issues):
            raise AppError("project_busy", "此立项编号已有进行中的首次入库批次", 409)
        blocks = [issue.model_dump() for issue in result.issues if issue.level == "block"]
        if blocks:
            raise AppError("preflight_blocked", "预检未通过，请修改后重新拖入", 422, blocks)
        missing = {issue.rule_code for issue in result.issues if issue.level == "warn"} - set(acks)
        if missing:
            raise AppError("acks_required", "请逐类勾选我已知晓", 422, sorted(missing))
        return result

    def _chunk_size(self, batch: Batch) -> int:
        return int(batch.payload.get("chunk_bytes", self.chunk_bytes))

    def body_limit(self, batch_id: int, login: LoginContext) -> int:
        with self.database.session() as session:
            batch = self._batch(session, batch_id, login.user)
            self._check_login(session, login, batch)
            return self._chunk_size(batch)

    def _snapshot(self, session: Session, batch: Batch, token: str | None = None,
                  differences: dict[str, list[str]] | None = None) -> BatchResponse:
        session.flush()
        files = list(session.scalars(select(BatchFile).where(BatchFile.batch_id == batch.id).order_by(BatchFile.id)))
        file_by_id = {file.id: file for file in files}
        products = active_products(session, batch.id)
        proven = {product.id for product in products if product.batch_file_id in file_by_id
                  and has_visual_evidence(product, file_by_id[product.batch_file_id])}
        return BatchResponse(id=batch.id, batch_no=batch.batch_no, project_code=batch.project_code,
            folder_name=str(batch.payload["folder_name"]), original_folder_name=str(batch.payload["original_folder_name"]),
            status=batch.status, queued=bool(batch.payload.get("queued")), won=batch.won, updated_at=batch.updated_at,
            terminated_at=batch.terminated_at, termination_reason=batch.termination_reason,
            files=[UploadFileResponse.model_validate(file) for file in files], kind=batch.kind, project_id=batch.project_id,
            directories=list(batch.payload.get("directories", [])), chunk_bytes=self._chunk_size(batch),
            parallel_files=batch.payload.get("parallel_files", self.auth.config.upload.parallel_files), upload_token=token, differences=differences,
            owner_id=batch.owner_id, uploader_id=batch.uploader_id, recommended_path=batch.recommended_path,
            tag_labels={field["key"]: field["label"] for field in batch.payload.get("ai_fields", [])},
            products=[BatchProductResponse.model_validate(product).model_copy(update={"visual_ai_succeeded":
                product.id in proven}) for product in products],
            ai_progress=AiProgress(items_total=sum(p.kind == "item" for p in products), items_done=sum(p.kind == "item" and p.id in proven for p in products),
                sets_total=sum(p.kind == "set" for p in products), sets_done=sum(p.kind == "set" and p.id in proven for p in products),
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

    def retry_ai(self, batch_id: int, login: LoginContext, host: str) -> BatchResponse:
        with self._transfer(login, host) as session:
            batch = session.get(Batch, batch_id)
            if batch is None:
                raise AppError("batch_not_found", "批次不存在", 404)
            user = session.get(User, login.user.id)
            if user is None or not user.enabled or not ("admin" in user.roles or user.id == batch.owner_id and set(user.roles) & {"uploader", "first_reviewer", "final_reviewer"}):
                raise AppError("forbidden", "只有上传人或管理员可以重试 AI", 403)
            if batch.kind not in {"ingest", "supplement"} or batch.status not in (AI, "待核对", "AI 处理失败") or self.ai is None:
                raise AppError("invalid_batch_state", "该批次当前不能重试 AI", 409)
            self._check_login(session, login)
            if batch.status == "待核对" and (user.id != batch.owner_id or not set(user.roles) & {"uploader", "first_reviewer", "final_reviewer"}):
                raise AppError("forbidden", "只有当前责任人可以重新识别核对中的批次", 403)
            self._verify_for_ai(session, batch, login, host)
            if batch.kind == "supplement" and not any(
                is_main_image(file) and not file.removed and file.conflict != "duplicate"
                for file in session.scalars(select(BatchFile).where(BatchFile.batch_id == batch.id))
            ):
                raise AppError("main_image_required", "补充批次没有待识别主图", 409)
            self.ai.start_batch(session, batch, user, host)
            OplogService(session).write(user.id, ",".join(user.roles), "重试整批 AI", "batch", str(batch.id), None, {"status": AI}, "成功", host)
            return self._snapshot(session, batch)

    def _fs(self, batch: Batch) -> NasFs:
        if batch.upload_session_id is None:
            raise NasSessionExpired("请重新登录后继续上传")
        return self.auth.pool.get(batch.upload_session_id)

    def _root(self, batch: Batch) -> str:
        return self.paths.join(Region.TEMP, batch.batch_no, str(batch.payload["folder_name"]))

    def _file_path(self, batch: Batch, file: BatchFile) -> str:
        return self.paths.join(Region.TEMP, batch.batch_no, str(batch.payload["folder_name"]), file.rel_path)

    def _busy(self, session: Session, batch: Batch) -> bool:
        return any(other.id != batch.id and (not other.payload.get("queued") or other.id < batch.id) for other in
                   session.scalars(select(Batch).where(Batch.uploader_id == batch.uploader_id, Batch.status == UPLOADING)))

    def _activate(self, session: Session, batch: Batch, host: str) -> None:
        if batch.id not in self._pinned:
            if batch.upload_session_id is None:
                raise NasSessionExpired("请重新登录后继续上传")
            self.auth.pool.pin(batch.upload_session_id)
            self._pinned[batch.id] = batch.upload_session_id
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
        pinned = self._pinned.pop(batch.id, None)
        if pinned is not None:
            self.auth.pool.unpin(pinned)

    def _finish_if_ready(self, session: Session, batch: Batch, host: str) -> None:
        session.flush()
        unfinished = session.scalar(select(BatchFile.id).where(BatchFile.batch_id == batch.id, BatchFile.upload_status != "done").limit(1))
        if unfinished is None:
            batch.status = STAGED
            batch.payload = {**batch.payload, "upload_token_hash": None, "queued": False}
            self._log(session, batch, "上传已暂存", "uploader", host)
            self._release(batch)

    @staticmethod
    def _active_ingest(session: Session, code: str, exclude: int | None = None) -> bool:
        query = select(Batch.id).where(Batch.op_lock_key == f"ingest:{code}", Batch.lock_active == true())
        if exclude is not None:
            query = query.where(Batch.id != exclude)
        return session.scalar(query.limit(1)) is not None

    @staticmethod
    def _next_h_code(session: Session, year: int) -> str:
        stmt = insert(HCodeSequence).values(year=year, last_sequence=1)
        number = session.scalar(stmt.on_conflict_do_update(index_elements=[HCodeSequence.year],
                                set_={"last_sequence": HCodeSequence.last_sequence + 1}).returning(HCodeSequence.last_sequence))
        if number is None or number > 9999:
            raise AppError("h_code_exhausted", "该年份历史编号已用完", 409)
        return f"H{year:04d}-{number:04d}"

    def create(self, body: CreateBatchRequest, login: LoginContext, host: str,
               project_id: int | None = None) -> BatchResponse:
        with self.lock:
            with self.database.session() as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                user = session.get(User, login.user.id)
                self._check_login(session, login)
                if user is None or not user.enabled or not set(user.roles) & {"uploader", "first_reviewer", "final_reviewer"}:
                    raise AppError("forbidden", "当前账号没有上传权限", 403)
                project = project_for_supplement(session, project_id) if project_id is not None else None
                if project is not None and active_batch_id(session, project.id) is not None:
                    raise AppError("project_busy", "此项目已有未完成的操作批次", 409)
                if project is not None and not self.auth.pool.get(login.session_id).stat(
                        self.paths.join(Region.FORMAL, project.current_path)).is_dir:
                    raise NasNotFound("正式项目目录不存在")
                result = self._validate(body, body.acks, project=project)
                code = result.project_code
                if project is None and code == "H":
                    if body.historical_year is None:
                        raise AppError("historical_year_required", "请选择历史项目实际所属年份", 422)
                    code = self._next_h_code(session, body.historical_year)
                elif project is None and (match := H_CODE.fullmatch(code)):
                    # 用户可能重新提交已经改名的 H 目录，计数器不回退。
                    statement = insert(HCodeSequence).values(year=int(match[1]), last_sequence=int(match[2]))
                    session.execute(statement.on_conflict_do_update(index_elements=[HCodeSequence.year],
                        set_={"last_sequence": func.max(HCodeSequence.last_sequence, statement.excluded.last_sequence)}))
                if project is None and self._active_ingest(session, code):
                    raise AppError("project_busy", "此立项编号已有进行中的首次入库批次", 409)
                if project is None and session.scalar(select(Project.id).where(Project.project_code == code)) is not None:
                    raise AppError("project_exists", "该项目已入库，请从项目页发起补充", 409)
                folder = project.folder_name if project is not None else f"{code}_{result.company_name}_{result.project_name}"
                if project is None and any(issue.level == "block" for issue in check(folder, body.files)):
                    raise AppError("preflight_blocked", "新编号后的路径不符合预检要求", 422)
                token = secrets.token_urlsafe(32)
                batch = Batch(batch_no=secrets.token_hex(16), project_code=code, kind="supplement" if project is not None else "ingest", status=UPLOADING,
                    project_id=project_id, owner_id=user.id, uploader_id=user.id,
                    upload_session_id=login.session_id, temp_path="", won=project.won if project is not None else body.won,
                    payload={"folder_name": folder, "original_folder_name": body.folder_name, "queued": False,
                             "directories": [item.rel_path for item in body.files if item.is_dir], "upload_token_hash": token_hash(token),
                             "historical_year": body.historical_year if result.project_code == "H" else None},
                    recommended_path=project.current_path if project is not None else None)
                session.add(batch); session.flush()
                batch.batch_no = f"B{utcnow():%Y%m%d}-{batch.id:06d}"
                batch.temp_path = batch.batch_no + "/" + folder
                batch.payload = {**batch.payload, "chunk_bytes": self.chunk_bytes,
                                 "parallel_files": self.auth.config.upload.parallel_files}
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

    def state(self, batch_id: int, login: LoginContext, host: str) -> BatchResponse:
        with self._transfer(login, host) as session:
            batch = self._batch(session, batch_id, login.user)
            self._check_login(session, login, batch)
            if batch.payload.get("queued"):
                self._activate(session, batch, host)
            if batch.status == UPLOADING:
                self._activity[batch.id] = time.monotonic()
            return self._snapshot(session, batch)

    def list(self, user: User) -> list[BatchResponse]:
        with self.lock, self.database.session() as session:
            return [self._snapshot(session, batch) for batch in session.scalars(select(Batch).where(or_(Batch.uploader_id == user.id, Batch.owner_id == user.id)).order_by(Batch.id.desc()))]

    def _receiving(self, session: Session, batch_id: int, login: LoginContext, file_id: int) -> tuple[Batch, BatchFile, NasFs]:
        batch = self._batch(session, batch_id, login.user)
        self._check_login(session, login, batch)
        if batch.status != UPLOADING or batch.payload.get("queued"):
            raise AppError("batch_not_uploading", "批次当前不在上传中", 409)
        file = session.get(BatchFile, file_id)
        if file is None or file.batch_id != batch_id:
            raise AppError("file_not_found", "文件不存在于该批次", 404)
        if batch.id not in self._pinned:
            if batch.upload_session_id is None:
                raise NasSessionExpired("请重新登录后继续上传")
            self.auth.pool.pin(batch.upload_session_id)
            self._pinned[batch.id] = batch.upload_session_id
        self._activity[batch.id] = time.monotonic()
        return batch, file, self._fs(batch)

    async def read_body(self, stream: AsyncIterator[bytes], limit: int) -> bytes:
        data = bytearray()
        async for block in stream:
            if len(data) + len(block) > limit:
                raise AppError("chunk_too_large", "分片超过配置大小", 413)
            data.extend(block)
        return bytes(data)

    def _repair(self, session: Session, batch: Batch, file: BatchFile, fs: NasFs, host: str) -> None:
        path = self._file_path(batch, file)
        actual = fs.stat(path).size if fs.exists(path) else 0
        expected = file.bytes_received
        if actual != expected:
            if actual < expected:
                file.bytes_received = min(actual // self._chunk_size(batch) * self._chunk_size(batch), file.size)
                file.upload_status = "partial" if file.bytes_received else "pending"
                file.fingerprint = None
                file.fingerprint_kind = None
                file.thumb_fingerprint = None
                file.thumb_status = None
                file.thumb_error = None
                if self.thumbs is not None:
                    self.thumbs.invalidate(batch.id, file.id)
            if actual:
                with fs.open_write(path, append=True) as stream:
                    stream.truncate(file.bytes_received)
            self._hashes.pop((batch.id, file.id), None)
            self._log(session, batch, "校正分片偏移", "uploader", host)

    @contextmanager
    def _transfer(self, login: LoginContext, host: str) -> Iterator[Session]:
        with self.lock:
            try:
                with self.database.session() as session:
                    yield session
            except NasSessionExpired:
                # 文件写失败则事务回滚，保留已确认的偏移，由重登录后的续传校正残留分片。
                self.auth.disconnect(login.session_id, host)
                raise

    def chunk(self, batch_id: int, file_id: int, index: int, data: bytes, login: LoginContext, host: str) -> dict[str, int]:
        with self.lock:
            conflict: int | None = None
            with self._transfer(login, host) as session:
                batch, file, fs = self._receiving(session, batch_id, login, file_id)
                self._repair(session, batch, file, fs, host)
                chunk_size = self._chunk_size(batch)
                offset = index * chunk_size
                length = min(chunk_size, file.size - offset)
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

    def complete(self, batch_id: int, file_id: int, login: LoginContext, host: str) -> BatchResponse:
        with self._transfer(login, host) as session:
            batch = self._batch(session, batch_id, login.user)
            self._check_login(session, login, batch)
            file = session.get(BatchFile, file_id)
            if file is None or file.batch_id != batch_id:
                raise AppError("file_not_found", "文件不存在于该批次", 404)
            if batch.status == STAGED and file.upload_status == "done":
                if self.thumbs is not None and file.conflict != "duplicate":
                    self.thumbs.enqueue(session, batch, file)
                return self._snapshot(session, batch)
            batch, file, fs = self._receiving(session, batch_id, login, file_id)
            if file.upload_status == "done":
                if self.thumbs is not None and file.conflict != "duplicate":
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
            digest = fingerprint(fs, path, file.size, cache[1] if cache and cache[0] == file.size else None)
            self._record_fingerprint(batch, file, digest)
            file.upload_status = "done"
            if batch.kind == "supplement":
                classify_file(file, catalog_files(session, batch.project_id).get(file.rel_path.casefold()))
            self._hashes.pop((batch_id, file_id), None)
            self._log(session, batch, "文件上传完成", "uploader", host)
            if self.thumbs is not None and file.conflict != "duplicate":
                self.thumbs.enqueue(session, batch, file)
            self._finish_if_ready(session, batch, host)
            return self._snapshot(session, batch)

    def pause(self, batch_id: int, login: LoginContext, host: str) -> BatchResponse:
        with self.lock, self.database.session() as session:
            batch = self._batch(session, batch_id, login.user)
            self._check_login(session, login, batch)
            if batch.status == UPLOADING:
                batch.status = INTERRUPTED
                batch.payload = {**batch.payload, "queued": False}
                self._log(session, batch, "暂停上传", "uploader", host)
                self._release(batch)
            return self._snapshot(session, batch)

    def terminate(self, batch_id: int, login: LoginContext, reason: str, host: str, *,
                  confirmed: bool = False) -> BatchResponse:
        with self.lock, self.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            batch = self._batch(session, batch_id, login.user)
            self._check_login(session, login)
            if batch.status == TERMINATED:
                return self._snapshot(session, batch)
            if batch.kind != "ingest" and not confirmed:
                raise AppError("termination_confirmation_required", "请二次确认终止整个操作批次", 422)
            if batch.status not in TERMINATABLE:
                raise AppError("invalid_batch_state", "该状态不能终止批次", 409)
            before = {"status": batch.status}
            batch.status, batch.terminated_at, batch.termination_reason = TERMINATED, utcnow(), reason
            if self.ai is not None:
                self.ai.cancel_jobs(session, batch, "批次已终止", login.user.id, host)
            batch.payload = {**batch.payload, "upload_token_hash": None, "queued": False}
            OplogService(session).write(login.user.id, ",".join(login.user.roles), "终止批次", "batch", str(batch.id),
                                       before, {"status": TERMINATED, "reason": reason}, "成功", host)
            if self.thumbs is not None:
                self.thumbs.invalidate(batch.id)
            self._release(batch)
            for key in tuple(self._hashes):
                if key[0] == batch.id:
                    self._hashes.pop(key)
            return self._snapshot(session, batch)

    @staticmethod
    def _record_fingerprint(batch: Batch, file: BatchFile, digest: str) -> None:
        if file.fingerprint is not None and file.fingerprint_kind != "full":
            previous = dict(batch.payload.get("legacy_fingerprints", {}))
            previous[str(file.id)] = {"value": file.fingerprint, "kind": file.fingerprint_kind or "legacy_unknown"}
            batch.payload = {**batch.payload, "legacy_fingerprints": previous}
        file.fingerprint, file.fingerprint_kind = digest, "full"
        batch.payload = {**batch.payload, "fingerprint_kinds": {**batch.payload.get("fingerprint_kinds", {}), str(file.id): "full"}}

    def _upgrade_fingerprint(self, session: Session, batch: Batch, file: BatchFile, fs: NasFs, host: str) -> None:
        if file.fingerprint_kind == "full" and file.fingerprint:
            return
        path = self._file_path(batch, file)
        if not fs.exists(path) or fs.stat(path).size != file.size:
            raise AppError("fingerprint_unverified", "临时文件已变化，无法核实完整指纹", 409)
        old_digest = file.fingerprint
        self._record_fingerprint(batch, file, fingerprint(fs, path, file.size))
        if self.thumbs is not None and old_digest != file.fingerprint:
            file.thumb_status, file.thumb_error, file.thumb_fingerprint = None, None, None
            self.thumbs.enqueue(session, batch, file)
        self._log(session, batch, "补算文件完整 SHA-256", "uploader", host)

    def _verify_for_ai(self, session: Session, batch: Batch, login: LoginContext, host: str) -> None:
        files = list(session.scalars(select(BatchFile).where(BatchFile.batch_id == batch.id)))
        if batch.kind == "ingest" and not any(is_main_image(file) and not file.removed for file in files):
            raise AppError("main_image_required", "首次入库必须保留至少一张主图", 409)
        for file in files:
            if file.removed:
                continue
            if file.upload_status != "done":
                raise AppError("file_not_completed", "须完成所有文件上传后再确认", 409)
            if file.fingerprint_kind != "full" or not file.fingerprint:
                if batch.uploader_id != login.user.id:
                    raise AppError("fingerprint_unverified", "历史批次须由上传人补算完整指纹后才能重试 AI", 409)
                batch.upload_session_id = login.session_id
                self._upgrade_fingerprint(session, batch, file, self.auth.pool.get(login.session_id), host)
            if is_main_image(file) and file.thumb_status == "done" and file.thumb_fingerprint != file.fingerprint:
                if batch.uploader_id != login.user.id:
                    raise AppError("thumb_unverified", "旧送审图须由上传人重新核实后才能重试 AI", 409)
                batch.upload_session_id = login.session_id

    def confirm(self, batch_id: int, login: LoginContext, host: str) -> BatchResponse:
        with self._transfer(login, host) as session:
            batch = self._batch(session, batch_id, login.user)
            self._check_login(session, login)
            if batch.status != STAGED:
                raise AppError("invalid_batch_state", "只有已暂存批次可以确认", 409)
            self._verify_for_ai(session, batch, login, host)
            if batch.kind == "supplement":
                current = catalog_files(session, batch.project_id)
                files = list(session.scalars(select(BatchFile).where(BatchFile.batch_id == batch.id)))
                for file in files:
                    classify_file(file, current.get(file.rel_path.casefold()))
                unresolved = [file.id for file in files if file.conflict == "conflict" and file.conflict_choice is None]
                if unresolved:
                    raise AppError("conflict_choice_required", "请明确选择替换或另存冲突文件", 409, {"file_ids": unresolved})
                if not any(is_main_image(file) and not file.removed and file.conflict != "duplicate" for file in files):
                    batch.status = "待核对"
                    self._log(session, batch, "确认补充资料直接核对", "uploader", host)
                    return self._snapshot(session, batch)
            batch.status = AI
            if self.ai is not None:
                self.ai.start_batch(session, batch, login.user, host)
            self._log(session, batch, "确认开始 AI 处理", "uploader", host)
            return self._snapshot(session, batch)

    def resume(self, batch_id: int, body: ResumeRequest, login: LoginContext, host: str) -> BatchResponse:
        with self._transfer(login, host) as session:
            batch = self._batch(session, batch_id, login.user)
            self._check_login(session, login)
            if batch.status not in (UPLOADING, INTERRUPTED):
                raise AppError("invalid_batch_state", "该批次不能继续上传", 409)
            if batch.status == UPLOADING and batch.upload_session_id != login.session_id:
                raise AppError("upload_session_mismatch", "批次正在其他设备上传，请先在原设备暂停", 409)
            if body.folder_name not in (batch.payload["folder_name"], batch.payload["original_folder_name"]):
                raise AppError("folder_mismatch", "请重新拖入同一个项目文件夹", 422)
            project = project_for_supplement(session, batch.project_id) if batch.kind == "supplement" else None
            self._validate(body, body.acks, batch.id, project)
            old = {file.rel_path: file for file in session.scalars(select(BatchFile).where(BatchFile.batch_id == batch.id))}
            new = {file.rel_path: file for file in body.files if not file.is_dir and not ignored(file)}
            differences = {"added": sorted(new.keys() - old.keys()), "missing": sorted(old.keys() - new.keys()),
                           "changed": sorted(path for path in old.keys() & new.keys() if old[path].size != new[path].size or old[path].mtime != new[path].mtime),
                           "directories": [] if sorted(batch.payload.get("directories", [])) == sorted(item.rel_path for item in body.files if item.is_dir) else ["空目录或目录清单变化"]}
            if any(differences.values()) and not body.accept_changes:
                return self._snapshot(session, batch, differences=differences)
            fs = self.auth.pool.get(login.session_id)
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
                        old[path].fingerprint_kind = None
                        old[path].thumb_fingerprint = None
                        old[path].thumb_status = None
                        old[path].thumb_error = None
                        old[path].conflict = old[path].conflict_choice = None
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
            self._release(batch)
            batch.upload_session_id = login.session_id
            session.flush()
            for file in old.values():
                if file.upload_status != "done":
                    self._repair(session, batch, file, fs, host)
                elif not fs.exists(self._file_path(batch, file)) or fs.stat(self._file_path(batch, file)).size != file.size:
                    file.upload_status = "pending"; file.bytes_received = 0; file.fingerprint = None
                    file.fingerprint_kind = None
                    file.thumb_status = None; file.thumb_error = None; file.thumb_fingerprint = None
                    file.conflict = file.conflict_choice = None
                    if self.thumbs is not None:
                        self.thumbs.invalidate(batch.id, file.id)
                else:
                    self._upgrade_fingerprint(session, batch, file, fs, host)
            present_acks = set(session.scalars(select(PreflightAck.rule_code).where(PreflightAck.batch_id == batch.id)))
            for code in set(body.acks) - present_acks:
                if code in {"P08", "P09"}:
                    session.add(PreflightAck(batch_id=batch.id, rule_code=code))
            token = secrets.token_urlsafe(32)
            batch.payload = {**batch.payload, "upload_token_hash": token_hash(token)}
            self._activate(session, batch, host)
            self._log(session, batch, "继续上传", "uploader", host)
            return self._snapshot(session, batch, token if batch.status != STAGED else None, differences)

    def session_closed(self, session_id: int, host: str) -> None:
        with self.lock, self.database.session() as session:
            for batch in session.scalars(select(Batch).where(Batch.upload_session_id == session_id, Batch.status == UPLOADING)):
                self._interrupt(session, batch, host)

    def _interrupt(self, session: Session, batch: Batch, host: str) -> None:
        batch.status = INTERRUPTED
        batch.payload = {**batch.payload, "queued": False, "upload_token_hash": None}
        self._log(session, batch, "上传中断", "system", host)
        self._release(batch)

    def _sweep(self) -> None:
        while not self._stop.wait(5):
            self.expire_idle()

    def expire_idle(self) -> None:
        with self.lock, self.database.session() as session:
            for batch_id, last in tuple(self._activity.items()):
                batch = session.get(Batch, batch_id)
                if batch is None or batch.status != UPLOADING:
                    continue
                login = session.get(UserSession, batch.upload_session_id) if batch.upload_session_id is not None else None
                if login is None or login.revoked_at is not None or is_expired(login.expires_at) or time.monotonic() - last > 60:
                    self._interrupt(session, batch, "上传请求或登录超时")

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
