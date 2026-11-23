"""初审与终审：领取、钉图意见、清单、通过、驳回、终审改位置。审核本身不写 NAS。"""
from __future__ import annotations

import hashlib
import secrets
import json

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import (Batch, BatchProduct, CompanyIndustryMap, Oplog, Project, ReviewPin, ReviewRound,
                           User, utcnow)
from app.errors import AppError
from app.schemas.review import ReviewResponse
from app.schemas.audit import (CHECKLIST, CHECKLIST_LABELS, AuditResponse, AuditStats, FolderCount, PathInfo, PersonView,
                               PinInput, QueueItem, RelationSummary)
from app.services.admin.company_map import batch_company, company_path, match_company, recommend
from app.services.auth.service import AuthService, LoginContext
from app.services.upload.service import UploadService
from app.services.notify import NotifyService, stage_since
from app.services.ingest import IngestService
from app.services.nas import Region
from app.services.postops.executor import PostopExecutor
from app.services.oplog import OplogService
from app.services.preflight import REQUIRED
from app.services.review import ReviewService
from app.services.review.history import pin_views, round_history
from app.services.review.policy import recused, self_review_allowed

FIRST, FINAL, REJECTED, INGESTING = "待初审", "待终审", "已驳回", "入库中"
# 状态 → (阶段名, 需要的角色, 批次上记录审核人的字段)
STAGES = {FIRST: ("first", "first_reviewer", "first_reviewer_id"), FINAL: ("final", "final_reviewer", "final_reviewer_id")}
ACK_TEXT = {"P07": "套装和单品均为空", "P09": "项目名包含不建议的空格或标点"}


def _name(user: User | None) -> str:
    return (user.display_name or user.username) if user else ""


class AuditService:
    def __init__(self, auth: AuthService, review: ReviewService, notify: NotifyService,
                 ingest: IngestService, postop: PostopExecutor) -> None:
        self.auth, self.review, self.notify, self.ingest, self.postop = auth, review, notify, ingest, postop

    # ---- 取数与权限 ----
    @staticmethod
    def _user(session: Session, actor: User) -> User:
        user = session.get(User, actor.id)
        if user is None or not user.enabled:
            raise AppError("forbidden", "当前账号没有此操作权限", 403)
        return user

    @staticmethod
    def _batch(session: Session, batch_id: int) -> Batch:
        batch = session.get(Batch, batch_id)
        if batch is None:
            raise AppError("batch_not_found", "批次不存在", 404)
        return batch

    @staticmethod
    def _stage(batch: Batch) -> tuple[str, str, str]:
        stage = STAGES.get(batch.status)
        if stage is None:
            raise AppError("invalid_batch_state", f"批次当前为「{batch.status}」，不在待审状态", 409)
        return stage

    def _claimed(self, session: Session, batch_id: int, actor: User) -> tuple[Batch, User, str]:
        """审核动作的统一入口：批次在待审状态、操作人具备该级角色且是领取人。"""
        user = self._user(session, actor)
        batch = self._batch(session, batch_id)
        stage, role, field = self._stage(batch)
        if role not in user.roles:
            raise AppError("forbidden", "当前账号没有这一级的审核权限", 403)
        if recused(session, self.auth.config.review, batch, user):
            raise AppError("reviewer_conflict", "当前账号须回避本次审核", 403)
        if getattr(batch, field) != user.id:
            raise AppError("not_claimed", "请先领取该批次再审核", 403)
        return batch, user, stage

    def _draft(self, session: Session, batch: Batch, stage: str, reviewer: User) -> ReviewRound:
        row = session.scalar(select(ReviewRound).where(ReviewRound.batch_id == batch.id, ReviewRound.round == batch.round,
                                                       ReviewRound.stage == stage))
        if row is None:
            row = ReviewRound(snapshot_id=self.review.submitted_snapshot(session, batch).id, batch_id=batch.id, round=batch.round, stage=stage, reviewer_id=reviewer.id)
            session.add(row)
            session.flush()
        if row.reviewer_id != reviewer.id and row.decision is None:
            # 换人领取：上一位未发出的钉图意见作废。
            session.execute(delete(ReviewPin).where(ReviewPin.review_round_id == row.id))
            row.reviewer_id = reviewer.id
        return row

    def _log(self, session: Session, user: User, action: str, batch: Batch, before: object, after: object, host: str) -> None:
        OplogService(session).write(user.id, ",".join(user.roles), action, "batch", str(batch.id), before, after, "成功", host)

    @staticmethod
    def _operation_params(session: Session, batch: Batch) -> dict:
        if batch.kind == "ingest" or batch.review_snapshot_hash is None:
            return {}
        return ReviewService.submitted_snapshot(session, batch).content["operation"]["params"]

    @classmethod
    def _can_change_path(cls, session: Session, batch: Batch) -> bool:
        return batch.kind in ("ingest", "rename", "move") or (
            batch.kind == "restore" and cls._operation_params(session, batch).get("project", False))

    @classmethod
    def _path(cls, session: Session, batch: Batch) -> PathInfo:
        company = batch_company(batch)
        recommended = recommend(session, batch)
        if batch.kind != "ingest":
            project = session.get(Project, batch.project_id)
            params = cls._operation_params(session, batch)
            if batch.kind == "move":
                mapped = session.get(CompanyIndustryMap, params.get("company_id")) if params.get("company_id") else None
                recommended = company_path(params["won"], mapped) if mapped else None
            elif project is not None:
                restore = batch.kind == "restore" and params.get("project")
                path = project.restore_path if restore else (project.canonical_path or project.current_path)
                recommended = (path.rpartition("/")[0] or path) if path else None
        if batch.status in (INGESTING, "入库异常", "已入库"):
            return PathInfo(recommended=batch.recommended_path, final=batch.final_path, effective=batch.final_path,
                            company_name=company, company_in_tree=recommended is not None)
        selection = batch.payload.get("review_location")
        chosen = None
        if selection:
            row = session.get(CompanyIndustryMap, selection["company_id"])
            if row and company_path(selection["won"], row) == selection["path"]:
                chosen = selection["path"]
        effective = chosen if selection else recommended
        return PathInfo(recommended=recommended, final=chosen, effective=effective if recommended or batch.kind != "ingest" else None,
                        company_name=company, company_in_tree=recommended is not None)

    def _version(self, session: Session, batch: Batch) -> str:
        path = self._path(session, batch).effective
        parts = [batch.id, batch.round, batch.review_snapshot_hash, batch.status, batch.payload.get("claim_nonce"), path]
        return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()

    def _check_version(self, session: Session, batch: Batch, version: str) -> None:
        if self._version(session, batch) != version:
            raise AppError("review_changed", "审核轮次、领取状态或目录已变化，请刷新后重新核对", 409)

    # ---- 队列与领取 ----
    def queue(self, stage: str, actor: User) -> list[QueueItem]:
        with self.auth.lock, self.auth.database.session() as session:
            user = self._user(session, actor)
            status = FIRST if stage == "first" else FINAL
            _, role, field = STAGES[status]
            if role not in user.roles and "admin" not in user.roles:
                raise AppError("forbidden", "当前账号没有查看该队列的权限", 403)
            now = utcnow()
            rows = sorted(session.scalars(select(Batch).where(Batch.status == status)), key=lambda row: (stage_since(row), row.id))
            items: list[QueueItem] = []
            for batch in rows:
                snapshot = self.review.submitted_snapshot(session, batch)
                files = snapshot.content["files"]
                claimer = session.get(User, getattr(batch, field)) if getattr(batch, field) else None
                since = stage_since(batch)
                items.append(QueueItem(id=batch.id, kind=batch.kind, batch_no=batch.batch_no, folder_name=str(batch.payload["folder_name"]),
                    round=batch.round, uploader_name=_name(session.get(User, batch.uploader_id)),
                    products=len(snapshot.content["review"]["products"]), files=len(files), total_size=sum(file["size"] for file in files),
                    waiting_since=since, waiting_seconds=max(0, int((now - since).total_seconds())),
                    claimed_by=PersonView(id=claimer.id, name=_name(claimer)) if claimer else None,
                    claimed_by_me=claimer is not None and claimer.id == user.id,
                    blocked=recused(session, self.auth.config.review, batch, user)))
            return items

    def claim(self, batch_id: int, actor: User, host: str, version: str) -> AuditResponse:
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            user = self._user(session, actor)
            batch = self._batch(session, batch_id)
            stage, role, field = self._stage(batch)
            self._check_version(session, batch, version)
            if role not in user.roles:
                raise AppError("forbidden", "当前账号没有这一级的审核权限", 403)
            if recused(session, self.auth.config.review, batch, user):
                raise AppError("reviewer_conflict", "提交人及发起人须回避，同轮初审和终审须由不同账号担任", 403)
            current = getattr(batch, field)
            if current is not None and current != user.id:
                raise AppError("already_claimed", f"该批次已由 {_name(session.get(User, current))} 领取", 409,
                               {"claimed_by": _name(session.get(User, current))})
            if current != user.id:
                setattr(batch, field, user.id)
                self._draft(session, batch, stage, user).claimed_at = utcnow()
                batch.payload = {**batch.payload, "claim_nonce": secrets.token_hex(16)}
                self._log(session, user, "领取审核", batch, {field: None}, {field: user.id, "stage": stage, "round": batch.round}, host)
            return self._response(session, batch, user)

    def release(self, batch_id: int, actor: User, host: str, version: str) -> AuditResponse:
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            user = self._user(session, actor)
            batch = self._batch(session, batch_id)
            stage, _, field = self._stage(batch)
            self._check_version(session, batch, version)
            current = getattr(batch, field)
            if current is None:
                raise AppError("not_claimed", "该批次尚未被领取", 409)
            if "admin" not in user.roles and current != user.id:
                raise AppError("forbidden", "只有管理员或领取人可以释放领取", 403)
            if "admin" not in user.roles and STAGES[batch.status][1] not in user.roles:
                raise AppError("forbidden", "审核角色已撤销", 403)
            setattr(batch, field, None)
            batch.payload = {**batch.payload, "claim_nonce": secrets.token_hex(16)}
            batch.payload.pop("review_location", None)
            batch.final_path = None
            draft = session.scalar(select(ReviewRound).where(ReviewRound.batch_id == batch.id, ReviewRound.round == batch.round,
                                                             ReviewRound.stage == stage, ReviewRound.decision.is_(None)))
            if draft is not None:
                draft.path_change = None
                session.execute(delete(ReviewPin).where(ReviewPin.review_round_id == draft.id))
            self._log(session, user, "释放审核领取", batch, {field: current}, {field: None, "stage": stage}, host)
            return self._response(session, batch, user)

    # ---- 审核页数据 ----
    def _response(self, session: Session, batch: Batch, user: User) -> AuditResponse:
        session.flush()
        stage_info = STAGES.get(batch.status)
        stage = stage_info[0] if stage_info else None
        claimer_id = getattr(batch, stage_info[2]) if stage_info else None
        claimer = session.get(User, claimer_id) if claimer_id else None
        has_role = stage_info is not None and stage_info[1] in user.roles
        blocked = recused(session, self.auth.config.review, batch, user)
        mine = claimer is not None and claimer.id == user.id and has_role and not blocked
        snapshot = self.review.submitted_snapshot(session, batch)
        review = ReviewResponse.model_validate(snapshot.content["review"])
        review.batch.editable = False
        review.batch.status = batch.status
        review.batch.review_snapshot_hash = snapshot.digest
        products = review.products
        names = {p.id: p.display_name for p in products}
        tags = [tag for p in products for tag in p.tags.values()]
        relations = []
        for p in products:
            if p.kind == "set":
                relations.append(RelationSummary(set_id=p.id, set_name=p.display_name,
                    items=[names[r.batch_product_id] for r in p.relation_items], confirmed=p.relation_confirmed,
                    ai_count=sum(r.source == "ai" for r in p.relation_items), manual_count=sum(r.source != "ai" for r in p.relation_items)))
        roots = {path.split("/")[0] for path in snapshot.content["directories"]}
        roots.update(f["rel_path"].split("/")[0] for f in snapshot.content["files"])
        acks = [("缺少 " + "、".join(name for name in REQUIRED if name not in roots) if code == "P08" else ACK_TEXT.get(code, code))
                + " · 上传人已知晓" for code in snapshot.content["preflight_acks"]]
        # 包含核对时已移除的商品；提交时间截断，历史统计不随下一轮修改。
        actions = list(session.scalars(select(Oplog.action).where(Oplog.object_type == "batch_product",
            Oplog.object_id.in_([str(pid) for pid in session.scalars(select(BatchProduct.id).where(BatchProduct.batch_id == batch.id))]),
            Oplog.ts <= snapshot.submitted_at)))
        draft = session.scalar(select(ReviewRound).where(ReviewRound.batch_id == batch.id, ReviewRound.round == batch.round,
            ReviewRound.stage == stage, ReviewRound.decision.is_(None))) if stage else None
        return AuditResponse(version=self._version(session, batch), review=review, stage=stage,
            claimed_by=PersonView(id=claimer.id, name=_name(claimer)) if claimer else None, claimed_by_me=mine,
            can_claim=has_role and claimer is None and not blocked, blocked=blocked, can_edit_path=mine and stage == "final" and self._can_change_path(session, batch),
            path=self._path(session, batch), acks=acks,
            stats=AuditStats(manual_edits=sum(tag.source == "manual" for tag in tags),
                ai_confirmed=sum(tag.source == "ai_confirmed" for tag in tags),
                confirmed_unknown=sum(tag.confirmation_status == "confirmed_unknown" for tag in tags), appended=actions.count("追加主图"),
                removed=actions.count("移除主图")),
            relations=relations, note=review.batch.note_to_reviewer,
            folder_counts=[FolderCount(name=folder.name, count=len(folder.files)) for folder in review.folders],
            history=round_history(session, batch.id),
            pins=pin_views(session, draft.id) if draft is not None and mine else [], checklist=dict(CHECKLIST_LABELS),
            operation={"kind": batch.kind, **snapshot.content["operation"]} if "operation" in snapshot.content else None)

    def read(self, batch_id: int, actor: User) -> AuditResponse:
        with self.auth.lock, self.auth.database.session() as session:
            user = self._user(session, actor)
            batch = self._batch(session, batch_id)
            stage = STAGES.get(batch.status)
            allowed = ("admin" in user.roles or stage is not None and stage[1] in user.roles
                       or user.id == batch.first_reviewer_id and "first_reviewer" in user.roles
                       or user.id == batch.final_reviewer_id and "final_reviewer" in user.roles)
            if not allowed:
                raise AppError("forbidden", "无权查看该批次的审核页", 403)
            return self._response(session, batch, user)

    # ---- 审核动作 ----
    def _replace_pins(self, session: Session, batch: Batch, draft: ReviewRound, user: User,
                      pins: list[PinInput], host: str) -> None:
        valid = {p["id"] for p in self.review.submitted_snapshot(session, batch).content["review"]["products"]}
        if any(pin.batch_product_id not in valid for pin in pins):
            raise AppError("product_not_found", "钉图意见指向的主图不在本批次中", 422)
        before = len(list(session.scalars(select(ReviewPin.id).where(ReviewPin.review_round_id == draft.id))))
        session.execute(delete(ReviewPin).where(ReviewPin.review_round_id == draft.id))
        for pin in pins:
            session.add(ReviewPin(review_round_id=draft.id, batch_product_id=pin.batch_product_id, x=pin.x, y=pin.y, text=pin.text.strip()))
        self._log(session, user, "暂存钉图意见", batch, {"pins": before}, {"pins": len(pins), "stage": draft.stage}, host)
        session.flush()

    def pins(self, batch_id: int, pins: list[PinInput], actor: User, host: str, version: str) -> AuditResponse:
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            batch, user, stage = self._claimed(session, batch_id, actor)
            self._check_version(session, batch, version)
            self._replace_pins(session, batch, self._draft(session, batch, stage, user), user, pins, host)
            return self._response(session, batch, user)

    def change_path(self, batch_id: int, won: bool, company_id: int, actor: User, host: str, version: str) -> AuditResponse:
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            user = self._user(session, actor)
            batch = self._batch(session, batch_id)
            if batch.status != FINAL or "final_reviewer" not in user.roles:
                raise AppError("forbidden", "只有终审在终审阶段可以修改存放位置", 403)
            batch, user, _ = self._claimed(session, batch_id, actor)
            if not self._can_change_path(session, batch):
                raise AppError("operation_path_locked", "此操作的审核不能调整项目位置", 409)
            self._check_version(session, batch, version)
            company = session.get(CompanyIndustryMap, company_id)
            if company is None:
                raise AppError("company_not_found", "只能从现有目录树中选择企业", 422)
            before = {"path": batch.final_path or batch.recommended_path, "won": batch.won}
            batch.final_path = company_path(won, company)
            selection = {"won": won, "company_id": company.id, "path": batch.final_path}
            batch.payload = {**batch.payload, "review_location": selection}
            self._draft(session, batch, "final", user).path_change = {"before": before, "after": selection}
            self._log(session, user, "修改存放位置", batch, before, {"path": batch.final_path, "won": won}, host)
            return self._response(session, batch, user)

    def dispatch_ingest(self, session: Session, batch: Batch, reviewer: User, login_id: int) -> None:
        if batch.kind == "ingest":
            self.ingest.enqueue(session, batch, reviewer.id, login_id)
        elif batch.kind != "purge":
            self.postop.enqueue(session, batch, reviewer.id, login_id)

    def approve(self, batch_id: int, checklist: dict[str, bool], login: LoginContext, host: str, version: str) -> AuditResponse:
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            UploadService._check_login(session, login)
            fs = self.auth.pool.get(login.session_id)
            batch, user, stage = self._claimed(session, batch_id, login.user)
            self._check_version(session, batch, version)
            missing = [CHECKLIST_LABELS[key] for key in CHECKLIST if checklist.get(key) is not True]
            if missing or set(checklist) - set(CHECKLIST):
                raise AppError("checklist_incomplete", "审核清单必须全部勾选才能通过", 422, {"missing": missing})
            before = {"status": batch.status, "round": batch.round}
            row = self._draft(session, batch, stage, user)
            self_review = self_review_allowed(session, self.auth.config.review, batch, user)
            if stage == "final":
                path = self._path(session, batch).effective
                if not path:
                    info = self._path(session, batch)
                    raise AppError("path_pending", "位置待定，请先从目录树选择存放位置" if info.company_in_tree
                                   else "位置待定：等待管理员在对照表中录入该企业", 409, {"company_in_tree": info.company_in_tree})
                if batch.kind == "restore" and self._operation_params(session, batch).get("project") and not row.path_change:
                    project = session.get(Project, batch.project_id)
                    mapped = session.get(CompanyIndustryMap, project.company_id) if project.company_id else None
                    if mapped is None or company_path(project.won, mapped) != path or not fs.exists(
                            self.ingest.paths.join(Region.FORMAL, path)):
                        raise AppError("path_pending", "原恢复位置已变化，请终审从目录树核定有效位置", 409)
                first = session.scalar(select(ReviewRound).where(ReviewRound.snapshot_id == row.snapshot_id,
                    ReviewRound.stage == "first", ReviewRound.decision == "pass"))
                if (first is None or first.reviewer_id != batch.first_reviewer_id
                        or (first.reviewer_id == user.id and not self_review)):
                    raise AppError("first_review_required", "本轮缺少有效的初审通过记录", 409)
                row.execution_session_id = login.session_id
                if not row.path_change:
                    params = self._operation_params(session, batch)
                    matched = match_company(session, batch_company(batch))
                    row.path_change = {"after": {"path": path, "won": params.get("won", batch.won),
                                                "company_id": params.get("company_id") if batch.kind == "move"
                                                else (matched.id if matched else None)}}
                batch.status, batch.final_path, batch.final_reviewer_id = INGESTING, path, user.id
            else:
                batch.status = FINAL
                batch.payload = {**batch.payload, "stage_since": utcnow().isoformat()}
            # 通过时钉图意见作废。
            session.execute(delete(ReviewPin).where(ReviewPin.review_round_id == row.id))
            row.decision, row.checklist, row.comment, row.decided_at = "pass", dict(checklist), None, utcnow()
            result = {"status": batch.status, "round": batch.round, "final_path": batch.final_path, "checklist": dict(checklist)}
            if self_review:
                result["self_review_exception"] = True
            self._log(session, user, "初审通过" if stage == "first" else "终审通过", batch, before, result, host)
            if stage == "final":
                self.dispatch_ingest(session, batch, user, login.session_id)
            self.notify.emit(session, "first_passed" if stage == "first" else "final_passed", batch)
            return self._response(session, batch, user)

    def reject(self, batch_id: int, comment: str, submitted_pins: list[PinInput] | None, actor: User, host: str, version: str) -> AuditResponse:
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            batch, user, stage = self._claimed(session, batch_id, actor)
            self._check_version(session, batch, version)
            text = comment.strip()
            if not text:
                raise AppError("comment_required", "驳回必须填写总体意见", 422)
            before = {"status": batch.status, "round": batch.round}
            row = self._draft(session, batch, stage, user)
            if submitted_pins is not None:
                self._replace_pins(session, batch, row, user, submitted_pins, host)
            row.decision, row.comment, row.decided_at = "reject", text, utcnow()
            session.flush()
            pins = len(list(session.scalars(select(ReviewPin.id).where(ReviewPin.review_round_id == row.id))))
            batch.status = REJECTED
            self._log(session, user, "初审驳回" if stage == "first" else "终审驳回", batch, before,
                      {"status": batch.status, "round": batch.round, "comment": text, "pins": pins}, host)
            self.notify.emit(session, "rejected", batch)
            return self._response(session, batch, user)
