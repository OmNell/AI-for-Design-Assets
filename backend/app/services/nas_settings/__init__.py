"""NAS 配置以 YAML 为准，运行池只在进程重新启动时切换。"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
import hmac
import os
from pathlib import Path
from threading import BoundedSemaphore

from sqlalchemy import select
from sqlalchemy.orm import Session
import yaml

from app.config import NasConfig, Settings, load_settings
from app.db.database import Database
from app.db.models import Batch, CatalogFile, ExpectedDeletion, HistoryVersion, Job, Project, User
from app.errors import AppError
from app.schemas.nas_settings import NasApply, NasBrowse, NasCandidate, NasCheck, NasNodes, NasPublic, NasStatus, NasTarget, NasTest, NasValidation
from app.services.auth.backends import AuthUnavailable
from app.services.auth.service import AuthService
from app.services.crypto import CryptoError, SecretCipher
from app.services.jobs import JobQueue
from app.services.nas.errors import NasError, NasNotFound, NasPermissionDenied
from app.services.nas.factory import create_paths, create_system_fs
from app.services.nas.paths import NasPaths, Region, work_path
from app.services.oplog import OplogService
from app.services.upload.fingerprint import fingerprint

from .probe import ADD_DIR, ADD_FILE, ATTRIBUTES, DELETE_CHILD, MUTATE, READ, Connections, endpoint, target_name
from .state import Journal, NasChangeGate, atomic_write, revision

ROOT_BLOCKED = "当前工作台已经存在正式资产。更换存储根目录需要独立资产迁移和一致性验证。"
MANUAL = "生产现场须核对实际继承 ACL。涉及写入的验证仅在已批准的隔离测试目录执行。"
LABELS = {Region.FORMAL: "正式资产区", Region.TEMP: "上传临时区", Region.BACKUP: "备份区"}


def public(nas: NasConfig) -> NasPublic:
    paths = NasPaths(nas.root_path, nas.temp_path, nas.backup_path)
    return NasPublic(host=nas.host, dsm_port=nas.dsm_port, share=nas.share, work_root=nas.work_root,
                     system_user=nas.system_user, has_system_password=bool(nas.system_password_enc.get_secret_value()),
                     certificate="trusted_ca" if nas.dsm_ca_file else "system_ca" if nas.dsm_https else "legacy_http",
                     regions={r.value: "/" + paths.root(r) for r in Region})


def check(name: str, task: Callable[[], str | None]) -> NasCheck:
    try:
        result = task()
    except (AuthUnavailable, NasError) as error:
        return NasCheck(name=name, state="failed", message=str(error))
    return NasCheck(name=name, state="passed", message=result or "检查通过")


def denied(reader, path: str, rights: tuple[int, ...]) -> str:
    for right in rights:
        try:
            reader.check(path, right)
        except NasPermissionDenied:
            continue
        raise NasPermissionDenied("检测到该账号拥有不应获得的目录权限")
    return "目录访问掩码符合预期拒绝"


def system_checks(config: Settings, cipher: SecretCipher, connections: Connections) -> tuple[list[NasCheck], str]:
    nas = config.nas
    paths = create_paths(config)
    secret = nas.system_password_enc.get_secret_value()
    try:
        password = cipher.decrypt(secret) if secret else ""
    except CryptoError:
        return [NasCheck(name="系统账号", state="failed", message="系统账号密文无法解密，请更新密码")], ""
    if not password and config.mode == "prod":
        return [NasCheck(name="系统账号", state="failed", message="请输入系统账号密码")], ""
    try:
        with connections.open(nas, nas.system_user, password, system=True) as fs:
            return [
                check("系统账号 · 正式区读取", lambda: fs.check(paths.formal, READ)),
                check("系统账号 · 正式区写权限拒绝", lambda: denied(fs, paths.formal, MUTATE)),
                check("系统账号 · 临时区访问拒绝", lambda: denied(fs, paths.temp, (READ, ATTRIBUTES, ADD_FILE))),
                check("系统账号 · 备份区读写授权", lambda: fs.check(paths.backup, READ | ADD_FILE | ADD_DIR)),
            ], fs.identity
    except NasError as error:
        return [NasCheck(name="系统账号 · SMB", state="failed", message=str(error))], ""


def retained(session: Session) -> bool:
    return any(session.scalar(select(table.id).limit(1)) is not None for table in
               (Project, HistoryVersion, ExpectedDeletion, Batch))


def conflicts(session: Session, old: NasConfig, new: NasConfig) -> list[str]:
    messages = [f"批次 {row.batch_no} · {row.status}" for row in session.scalars(
        select(Batch).where(Batch.lock_active.is_(True)).order_by(Batch.id).limit(15))]
    messages.extend(f"任务 #{item.id} · {item.kind} · {item.status}" for item in session.scalars(
        select(Job).where(Job.status.in_(("queued", "running"))).order_by(Job.id).limit(15)))
    previous, candidate = public(old), public(new)
    if retained(session) and (previous.share != candidate.share or previous.regions != candidate.regions):
        messages.insert(0, ROOT_BLOCKED)
    return messages


def asset_sample(config: Settings, database: Database) -> None:
    with database.session() as session:
        any_project = session.scalar(select(Project.id).limit(1))
        rows = session.execute(select(Project.current_path, CatalogFile.physical_rel_path, CatalogFile.rel_path,
            CatalogFile.fingerprint, CatalogFile.size).join(Project, CatalogFile.project_id == Project.id)
            .where(CatalogFile.status == "active", CatalogFile.fingerprint_kind == "full",
                   CatalogFile.size <= 16 * 1024 * 1024, Project.status == "active")
            .order_by(CatalogFile.id).limit(3)).all()
    if any_project is not None and not rows:
        raise NasPermissionDenied("缺少可验证的既有资产样本，不能证明地址变更指向原存储")
    if not rows:
        return
    fs = create_system_fs(config)
    try:
        paths = create_paths(config)
        for root, location, relative, digest, size in rows:
            if fingerprint(fs, paths.join(Region.FORMAL, root, location or relative), size) != digest:
                raise NasPermissionDenied("候选 NAS 上的资产样本与原完整 SHA-256 不一致")
    finally:
        fs.close()


class NasSettingsService:
    def __init__(self, source: Path, auth: AuthService, cipher: SecretCipher, queue: JobQueue, gate: NasChangeGate) -> None:
        self.source, self.auth, self.cipher, self.queue, self.gate = source, auth, cipher, queue, gate
        self.config = auth.config
        self.connections = Connections(self.config)
        self.journal = Journal(self.config.paths.data_dir)
        self._slots = BoundedSemaphore(2)

    @contextmanager
    def slot(self):
        if not self._slots.acquire(blocking=False):
            raise AppError("nas_probe_busy", "NAS 检查正在执行，请稍后重试", 429)
        try:
            yield
        finally:
            self._slots.release()

    def _action(self, actor: User, action: str, host: str, result: str, after: dict | None = None) -> None:
        with self.auth.lock, self.auth.database.session() as session:
            OplogService(session).write(actor.id, "admin", action, "settings", "nas", None, after, result, host)

    def _target(self, target: NasTarget) -> NasConfig:
        target_name(target.host, target.share)
        address = endpoint(target.host) if self.config.mode == "prod" else None
        return self.config.nas.model_copy(update={"host": target.host, "share": target.share,
            "dsm_port": target.dsm_port, "dsm_https": True, "verified_address": address})

    def _revision(self, raw: bytes) -> str:
        # 客户端只见加密密钥保护的文件版本，不能用它离线猜测配置中的值。
        return hmac.digest((self.config.paths.data_dir / "secret.key").read_bytes(), raw, "sha256").hex()

    def state(self) -> NasStatus:
        active = public(self.config.nas)
        info = self.journal.read()
        pending = load_settings(self.source) if self.gate.pending else None
        return NasStatus(state="pending_restart" if self.gate.pending else "failed" if info.get("state") == "failed" else "active",
            message="新配置已保存，等待运维重启，业务访问暂时暂停" if pending else info.get("message", "当前运行配置"),
            revision=self._revision(self.source.read_bytes()) if self.source.is_file() else None,
            active=active, pending=public(pending.nas) if pending else None, mode=self.config.mode)

    def check_active(self) -> NasStatus:
        state = self.state()
        if self.gate.pending:
            return state
        nas = self.config.nas
        with self.slot():
            try:
                target_name(nas.host, nas.share)
                if self.config.mode == "prod":
                    if not nas.dsm_https:
                        raise AppError("dsm_https_required", "生产 DSM 必须使用 HTTPS", 422)
                    nas = nas.model_copy(update={"verified_address": nas.verified_address or endpoint(nas.host)})
                checks = [check("当前 DSM HTTPS", lambda: self.connections.dsm(nas))]
                active = self.config.model_copy(update={"nas": nas})
                checks.extend(system_checks(active, self.cipher, self.connections)[0])
            except AppError as error:
                checks = [NasCheck(name="当前连接配置", state="failed", message=error.message)]
        return state.model_copy(update={"connection_checks": checks})

    def test(self, actor: User, body: NasTest, host: str) -> list[NasCheck]:
        nas = self._target(body)
        username = body.username or actor.username
        if username.casefold() == self.config.nas.system_user.casefold():
            raise AppError("test_identity_invalid", "个人连接检查不可使用系统账号", 422)
        password = body.actor_password.get_secret_value()
        checks = []
        def smb() -> None:
            with self.connections.open(nas, username, password) as fs:
                fs.check("", READ | ATTRIBUTES)
                if body.permission_profile:
                    paths = NasPaths.under(body.work_root)
                    fs.check(paths.formal, READ)
                    fs.check(paths.temp, ADD_FILE | ADD_DIR)
                    if body.permission_profile == "employee":
                        denied(fs, paths.formal, MUTATE)
                    else:
                        fs.check(paths.formal, ADD_FILE | ADD_DIR)
        with self.slot():
            if body.protocol == "dsm":
                checks.append(check("DSM HTTPS 认证", lambda: self.connections.dsm(nas, username, password)))
            else:
                checks.append(check("SMB 会话与目录授权", smb))
                if body.permission_profile:
                    checks.append(NasCheck(name="实际写入与权限继承", state="manual", message=MANUAL))
        self._action(actor, "测试 NAS 连接", host, "成功" if checks[0].state == "passed" else "失败", {"protocol": body.protocol})
        return checks

    def browse(self, actor: User, body: NasBrowse, host: str) -> NasNodes:
        nas = self._target(body)
        path = work_path(body.path, allow_share=True)
        if actor.username.casefold() == nas.system_user.casefold():
            raise AppError("browse_identity_invalid", "目录选择必须使用管理员本人账号", 422)
        try:
            with self.slot(), self.connections.open(nas, actor.username, body.actor_password.get_secret_value()) as fs:
                nodes, truncated = fs.nodes(path)
        except NasError:
            self._action(actor, "浏览 NAS 目录", host, "失败")
            raise
        self._action(actor, "浏览 NAS 目录", host, "成功", {"nodes": len(nodes)})
        return NasNodes(path=path, nodes=nodes, truncated=truncated)

    def _candidate(self, body: NasCandidate, actor: User) -> Settings:
        nas = self._target(body)
        paths = NasPaths.under(body.work_root)
        if (body.system_user.casefold() in {actor.username.casefold(), self.config.bootstrap_admin.casefold()}
                or body.system_user != body.system_user.strip() or any(ord(c) < 32 for c in body.system_user)):
            raise AppError("invalid_system_user", "请填写独立的系统账号，不能使用管理员本人", 422)
        if any(row.username.casefold() == body.system_user.casefold() for row in self.config.dev_users):
            raise AppError("invalid_system_user", "系统账号不能是开发模式的人员账号", 422)
        password = body.system_password.get_secret_value() if body.system_password else ""
        old = self.config.nas
        address_changed = nas.host.casefold() != old.host.casefold() or (
            old.verified_address is not None and nas.verified_address != old.verified_address)
        if self.config.mode == "prod" and not password and (body.system_user != old.system_user or address_changed):
            raise AppError("system_password_required", "更换系统账号或地址时，请重新输入该 NAS 系统账号密码", 422)
        secret = self.cipher.encrypt(password) if password else nas.system_password_enc.get_secret_value()
        nas = NasConfig.model_validate({**nas.model_dump(), "system_user": body.system_user, "system_password_enc": secret,
              "work_root": body.work_root, "root_path": "/" + paths.formal, "temp_path": "/" + paths.temp,
              "backup_path": "/" + paths.backup})
        return self.config.model_copy(update={"nas": nas})

    def _validate(self, candidate: Settings, body: NasCandidate, actor: User) -> tuple[NasValidation, str]:
        nas = candidate.nas
        old = self.config.nas
        after, before = public(nas), public(old)
        changes = [title for key, title in {"host":"NAS 地址", "dsm_port":"DSM 端口", "share":"SMB 共享",
                   "work_root":"工作目录", "regions":"业务目录", "system_user":"系统账号", "certificate":"证书策略"}.items()
                   if getattr(after,key) != getattr(before,key)]
        if body.system_password and body.system_password.get_secret_value():
            changes.append("更新系统账号加密密码")
        address_changed = old.host.casefold() != nas.host.casefold() or old.verified_address != nas.verified_address
        with self.auth.database.session() as session:
            blocked = conflicts(session, old, nas)
            has_data = retained(session)
        if any(key.upper().startswith("NASWB_NAS") for key in os.environ):
            blocked.append("本机环境变量覆盖 NAS 配置，请由运维移除覆盖后重试")
        if not self.source.is_file():
            blocked.append("找不到运行中的 YAML 配置文件")
        if not changes and not address_changed:
            blocked.append("配置没有变化")
        checks: list[NasCheck] = []
        new_identity = ""
        if not blocked:
            password = body.actor_password.get_secret_value() if body.actor_password else ""
            paths = create_paths(candidate)
            def operator() -> list[NasCheck]:
                with self.connections.open(nas, actor.username, password) as fs:
                    results = [check("工作根目录", lambda: fs.check(body.work_root, ATTRIBUTES | READ))]
                    results.append(check("临时区存在 · 管理员读取", lambda: fs.check(paths.temp, ATTRIBUTES | READ)))
                    results.append(check("管理员 · 临时区删除子项授权", lambda: fs.check(paths.temp, DELETE_CHILD)))
                    results.append(NasCheck(name="管理员清理实际文件", state="manual", message=MANUAL))
                    return results
            checks.append(check("DSM HTTPS", lambda: self.connections.dsm(nas, actor.username, password)))
            try:
                checks += operator()
            except NasError as error:
                checks.append(NasCheck(name="管理员 SMB 会话", state="failed", message=str(error)))
            rows, new_identity = system_checks(candidate, self.cipher, self.connections)
            checks += rows
            if address_changed and has_data:
                def identity() -> str:
                    previous = old
                    if self.config.mode == "prod":
                        target_name(old.host, old.share)
                        previous = old.model_copy(update={"verified_address": old.verified_address or endpoint(old.host)})
                    previous_secret = old.system_password_enc.get_secret_value()
                    with self.connections.open(previous, old.system_user,
                          self.cipher.decrypt(previous_secret) if previous_secret else "", system=True) as original:
                        if not new_identity or new_identity != original.identity:
                            raise NasPermissionDenied("候选服务器的 SMB 身份与原 NAS 不一致")
                    asset_sample(candidate, self.auth.database)
                    return "原 NAS SMB 身份及既有资产完整指纹相符"
                try:
                    checks.append(check("同一 NAS 与档案身份", identity))
                except (AppError, CryptoError):
                    checks.append(NasCheck(name="同一 NAS 与档案身份", state="failed", message="无法验证原 NAS 身份，保留原配置"))
            checks.append(NasCheck(name="人员 ACL 与实际设备写入", state="manual",
                                   message=MANUAL))
        return NasValidation(eligible=not blocked and not any(row.state == "failed" for row in checks),
                             checks=checks, changes=changes, conflicts=blocked, regions=after.regions), new_identity

    def validate(self, actor: User, body: NasCandidate, host: str) -> NasValidation:
        with self.slot():
            result, _ = self._validate(self._candidate(body, actor), body, actor)
        self._action(actor, "校验 NAS 配置", host, "成功" if result.eligible else "拦截",
                     {"checks": {row.name: row.state for row in result.checks}})
        return result

    def apply(self, actor: User, body: NasApply, host: str) -> NasStatus:
        with self.slot():
            candidate = self._candidate(body, actor)
            result, identity = self._validate(candidate, body, actor)
        if not result.eligible:
            self._action(actor, "保存 NAS 配置", host, "拦截")
            raise AppError("nas_configuration_blocked", "NAS 配置不能启用", 409, result.model_dump())
        with self.auth.lock:
            raw = self.source.read_bytes()
            if not hmac.compare_digest(body.revision, self._revision(raw)) or load_settings(self.source).nas != self.config.nas:
                raise AppError("nas_config_revision_conflict", "配置版本已变化，请重新加载后校验", 409)
            with self.auth.database.session() as session:
                user = session.get(User, actor.id)
                if user is None or not user.enabled or "admin" not in user.roles:
                    raise AppError("forbidden", "只有管理员可以保存 NAS 配置", 403)
                blocked = conflicts(session, self.config.nas, candidate.nas)
                if blocked:
                    raise AppError("nas_configuration_blocked", "还有关联业务，不能切换", 409, blocked)
            doc = yaml.safe_load(raw)
            new_nas = candidate.nas.model_dump(mode="json", exclude={"system_password_enc", "dsm_ca_file"})
            new_nas["system_password_enc"] = candidate.nas.system_password_enc.get_secret_value()
            new_file = yaml.safe_dump({**doc, "nas": {**doc.get("nas", {}), **new_nas}},
                                       allow_unicode=True, sort_keys=False).encode()
            info = {"state": "pending_restart", "before": revision(raw), "after": revision(new_file),
                    "actor_id": actor.id, "saved_at": datetime.now(timezone.utc).isoformat(), "identity": identity,
                    "check_sample": self.config.nas.host.casefold() != candidate.nas.host.casefold()
                        or self.config.nas.verified_address != candidate.nas.verified_address}
            self.gate.freeze()
            if not self.queue.pause_for_nas_change():
                self.gate.unfreeze()
                raise AppError("nas_jobs_in_progress", "还有后台任务正在收尾", 409)
            replaced = False
            try:
                atomic_write(self.journal.backup, raw)
                self.journal.write(info)
                atomic_write(self.source, new_file)
                replaced = True
                self._action(actor, "保存 NAS 配置", host, "待重启",
                             {"before": public(self.config.nas).model_dump(), "after": public(candidate.nas).model_dump(),
                              "changes": result.changes})
            except Exception:
                # 先恢复文件，再允许业务请求。恢复失败时保持阻断并由运维处理。
                if replaced or revision(self.source.read_bytes()) != revision(raw):
                    atomic_write(self.source, raw)
                try:
                    self.journal.write({**info, "state": "failed", "message": "保存失败，原配置已恢复"})
                finally:
                    self.queue.resume_after_nas_change_failure()
                    self.gate.unfreeze()
                raise AppError("nas_config_save_failed", "保存失败，原配置已恢复", 500) from None
        return self.state()


def activate_at_boot(config: Settings, source: Path, database: Database, cipher: SecretCipher) -> Settings:
    """先确认候选，随后才创建 DSM 后端、会话池和后台执行器。"""
    journal = Journal(config.paths.data_dir)
    record = journal.read()
    if record.get("state") != "pending_restart":
        return config
    saved = revision(source.read_bytes())
    if saved not in {record["before"], record["after"]}:
        raise RuntimeError("待启用的 NAS 配置已由外部修改，请核对本机配置与恢复副本")
    old = journal.backup.read_bytes()
    if revision(old) != record["before"]:
        raise RuntimeError("NAS 配置恢复副本校验失败")
    try:
        if saved == record["before"]:
            raise NasPermissionDenied("上次保存未完成")
        if any(key.upper().startswith("NASWB_NAS") for key in os.environ):
            raise NasPermissionDenied("环境变量覆盖了配置")
        connections = Connections(config)
        if config.mode == "prod":
            target_name(config.nas.host, config.nas.share)
            if endpoint(config.nas.host) != config.nas.verified_address:
                raise NasPermissionDenied("候选 DNS 地址已改变")
        connections.dsm(config.nas)
        rows, identity = system_checks(config, cipher, connections)
        if any(row.state == "failed" for row in rows) or not identity or identity != record["identity"]:
            raise NasPermissionDenied("重启时系统账号或 SMB 身份复检失败")
        if record["check_sample"]:
            asset_sample(config, database)
    except (AppError, NasError, AuthUnavailable, ValueError, CryptoError):
        atomic_write(source, old)
        config = load_settings(source)
        outcome, message = "failed", "候选 NAS 启用复检失败，原 YAML 配置已恢复"
    else:
        outcome, message = "active", "新 NAS 配置已通过重启复检并启用"
    journal.write({**record, "state": outcome, "message": message, "completed_at": datetime.now(timezone.utc).isoformat()})
    with database.session() as session:
        actor = session.get(User, record["actor_id"])
        OplogService(session).write(actor.id if actor else None, "system", "启动 NAS 配置复检",
                                    "settings", "nas", None, {"state": outcome}, message, "本机启动")
    return config
