"""初审与终审：领取、钉图意见、清单、通过、驳回、终审改位置。审核本身不写 NAS。"""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import (Batch, BatchFile, BatchProduct, CompanyIndustryMap, Oplog, PreflightAck, ReviewPin, ReviewRound,
                           User, utcnow)
from app.errors import AppError
from app.schemas.audit import (CHECKLIST, CHECKLIST_LABELS, AuditResponse, AuditStats, FolderCount, PathInfo, PersonView,
                               PinInput, QueueItem, RelationSummary)
from app.services.admin.company_map import batch_company, company_path, match_company
from app.services.ai.orchestrator import active_products
from app.services.auth.service import AuthService
from app.services.notify import NotifyService, stage_since
from app.services.oplog import OplogService
from app.services.preflight import REQUIRED
from app.services.review import ReviewService
from app.services.thumbs import is_main_image
from app.services.review.history import pin_views, round_history

FIRST, FINAL, REJECTED, INGESTING = "待初审", "待终审", "已驳回", "入库中"
# 状态 → (阶段名, 需要的角色, 批次上记录审核人的字段)
STAGES = {FIRST: ("first", "first_reviewer", "first_reviewer_id"), FINAL: ("final", "final_reviewer", "final_reviewer_id")}
ACK_TEXT = {"P07": "套装和单品均为空", "P09": "项目名包含不建议的空格或标点"}


def _name(user: User | None) -> str:
    return (user.display_name or user.username) if user else ""


class AuditService:
    def __init__(self, auth: AuthService, review: ReviewService, notify: NotifyService) -> None:
        self.auth, self.review, self.notify = auth, review, notify

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
        if getattr(batch, field) != user.id:
            raise AppError("not_claimed", "请先领取该批次再审核", 403)
        return batch, user, stage

    @staticmethod
    def _draft(session: Session, batch: Batch, stage: str, reviewer: User, create: bool = True) -> ReviewRound | None:
        row = session.scalar(select(ReviewRound).where(ReviewRound.batch_id == batch.id, ReviewRound.round == batch.round,
                                                       ReviewRound.stage == stage))
        if row is None and create:
            row = ReviewRound(batch_id=batch.id, round=batch.round, stage=stage, reviewer_id=reviewer.id)
            session.add(row)
            session.flush()
        if row is not None and row.reviewer_id != reviewer.id and row.decision is None:
            # 换人领取：上一位未发出的钉图意见作废。
            session.execute(delete(ReviewPin).where(ReviewPin.review_round_id == row.id))
            row.reviewer_id = reviewer.id
        return row

    def _log(self, session: Session, user: User, action: str, batch: Batch, before: object, after: object, host: str) -> None:
        OplogService(session).write(user.id, ",".join(user.roles), action, "batch", str(batch.id), before, after, "成功", host)

    @staticmethod
    def _path(session: Session, batch: Batch) -> PathInfo:
        company = batch_company(batch)
        return PathInfo(recommended=batch.recommended_path, final=batch.final_path,
                        effective=batch.final_path or batch.recommended_path, company_name=company,
                        company_in_tree=match_company(session, company) is not None)

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
                files = [file for file in session.scalars(select(BatchFile).where(BatchFile.batch_id == batch.id)) if not file.removed]
                claimer = session.get(User, getattr(batch, field)) if getattr(batch, field) else None
                since = stage_since(batch)
                items.append(QueueItem(id=batch.id, batch_no=batch.batch_no, folder_name=str(batch.payload["folder_name"]),
                    round=batch.round, uploader_name=_name(session.get(User, batch.uploader_id)),
                    products=len(active_products(session, batch.id)), files=len(files), total_size=sum(file.size for file in files),
                    waiting_since=since, waiting_seconds=max(0, int((now - since).total_seconds())),
                    claimed_by=PersonView(id=claimer.id, name=_name(claimer)) if claimer else None,
                    claimed_by_me=claimer is not None and claimer.id == user.id,
                    blocked=status == FINAL and batch.first_reviewer_id == user.id))
            return items

    def claim(self, batch_id: int, actor: User, host: str) -> AuditResponse:
        with self.auth.lock, self.auth.database.session() as session:
            user = self._user(session, actor)
            batch = self._batch(session, batch_id)
            stage, role, field = self._stage(batch)
            if role not in user.roles:
                raise AppError("forbidden", "当前账号没有这一级的审核权限", 403)
            if stage == "final" and batch.first_reviewer_id == user.id:
                raise AppError("reviewer_conflict", "你是本批次本轮的初审人，不能再担任终审", 403)
            current = getattr(batch, field)
            if current is not None and current != user.id:
                raise AppError("already_claimed", f"该批次已由 {_name(session.get(User, current))} 领取", 409,
                               {"claimed_by": _name(session.get(User, current))})
            if current != user.id:
                setattr(batch, field, user.id)
                self._draft(session, batch, stage, user, create=False)
                self._log(session, user, "领取审核", batch, {field: None}, {field: user.id, "stage": stage, "round": batch.round}, host)
            return self._response(session, batch, user)

    def release(self, batch_id: int, actor: User, host: str) -> AuditResponse:
        with self.auth.lock, self.auth.database.session() as session:
            user = self._user(session, actor)
            batch = self._batch(session, batch_id)
            stage, _, field = self._stage(batch)
            current = getattr(batch, field)
            if current is None:
                raise AppError("not_claimed", "该批次尚未被领取", 409)
            if "admin" not in user.roles and current != user.id:
                raise AppError("forbidden", "只有管理员或领取人可以释放领取", 403)
            setattr(batch, field, None)
            draft = session.scalar(select(ReviewRound).where(ReviewRound.batch_id == batch.id, ReviewRound.round == batch.round,
                                                             ReviewRound.stage == stage, ReviewRound.decision.is_(None)))
            if draft is not None:
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
        blocked = stage == "final" and batch.first_reviewer_id == user.id
        mine = claimer is not None and claimer.id == user.id and has_role
        review = self.review._response(session, batch, user)
        keys = {field.key for field in review.fields}
        products = active_products(session, batch.id)
        names = {product.id: product.display_name for product in products}
        tags = [tag for product in products for key, tag in product.tags.items() if key in keys and isinstance(tag, dict)]
        files = list(session.scalars(select(BatchFile).where(BatchFile.batch_id == batch.id)))
        product_ids = [str(row) for row in session.scalars(select(BatchProduct.id).where(BatchProduct.batch_id == batch.id))]
        appended = len(list(session.scalars(select(Oplog.id).where(Oplog.action == "追加主图", Oplog.object_type == "batch_product",
                                                                  Oplog.object_id.in_(product_ids))))) if product_ids else 0
        relations = []
        for product in products:
            if product.kind != "set":
                continue
            valid = [item for item in product.relation_items if isinstance(item, dict) and item.get("batch_product_id") in names]
            relations.append(RelationSummary(set_id=product.id, set_name=product.display_name,
                items=[names[item["batch_product_id"]] for item in valid], confirmed=product.relation_confirmed,
                ai_count=sum(item.get("source") == "ai" for item in valid), manual_count=sum(item.get("source") != "ai" for item in valid)))
        roots = {file.rel_path.split("/")[0] for file in files if "/" in file.rel_path}
        roots |= {str(path).split("/")[0] for path in batch.payload.get("directories", [])}
        acks = []
        for code in session.scalars(select(PreflightAck.rule_code).where(PreflightAck.batch_id == batch.id).order_by(PreflightAck.rule_code)):
            text = "缺少 " + "、".join(name for name in REQUIRED if name not in roots) if code == "P08" else ACK_TEXT.get(code, code)
            acks.append(f"{text} · 上传人已知晓")
        draft = session.scalar(select(ReviewRound).where(ReviewRound.batch_id == batch.id, ReviewRound.round == batch.round,
            ReviewRound.stage == stage, ReviewRound.decision.is_(None))) if stage else None
        return AuditResponse(review=review, stage=stage,
            claimed_by=PersonView(id=claimer.id, name=_name(claimer)) if claimer else None, claimed_by_me=mine,
            can_claim=has_role and claimer is None and not blocked, blocked=blocked, can_edit_path=mine and stage == "final",
            path=self._path(session, batch), acks=acks,
            stats=AuditStats(manual_edits=sum(tag.get("source") == "manual" for tag in tags),
                ai_confirmed=sum(tag.get("source") == "ai_confirmed" for tag in tags),
                manual_filled=sum(product.ai_status == "failed" for product in products), appended=appended,
                removed=sum(file.removed and is_main_image(file) for file in files)),
            relations=relations, note=batch.note_to_reviewer,
            folder_counts=[FolderCount(name=folder.name, count=len(folder.files)) for folder in review.folders],
            history=round_history(session, batch.id),
            pins=pin_views(session, draft.id) if draft is not None and mine else [], checklist=dict(CHECKLIST_LABELS))

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
    def pins(self, batch_id: int, pins: list[PinInput], actor: User, host: str) -> AuditResponse:
        with self.auth.lock, self.auth.database.session() as session:
            batch, user, stage = self._claimed(session, batch_id, actor)
            valid = {product.id for product in active_products(session, batch.id)}
            if any(pin.batch_product_id not in valid for pin in pins):
                raise AppError("product_not_found", "钉图意见指向的主图不在本批次中", 422)
            draft = self._draft(session, batch, stage, user)
            before = len(list(session.scalars(select(ReviewPin.id).where(ReviewPin.review_round_id == draft.id))))
            session.execute(delete(ReviewPin).where(ReviewPin.review_round_id == draft.id))
            for pin in pins:
                session.add(ReviewPin(review_round_id=draft.id, batch_product_id=pin.batch_product_id, x=pin.x, y=pin.y, text=pin.text.strip()))
            self._log(session, user, "暂存钉图意见", batch, {"pins": before}, {"pins": len(pins), "stage": stage}, host)
            return self._response(session, batch, user)

    def change_path(self, batch_id: int, won: bool, company_id: int, actor: User, host: str) -> AuditResponse:
        with self.auth.lock, self.auth.database.session() as session:
            user = self._user(session, actor)
            batch = self._batch(session, batch_id)
            if batch.status != FINAL or "final_reviewer" not in user.roles:
                raise AppError("forbidden", "只有终审在终审阶段可以修改存放位置", 403)
            batch, user, _ = self._claimed(session, batch_id, actor)
            company = session.get(CompanyIndustryMap, company_id)
            if company is None:
                raise AppError("company_not_found", "只能从现有目录树中选择企业", 422)
            before = {"path": batch.final_path or batch.recommended_path, "won": batch.won}
            batch.final_path, batch.won = company_path(won, company), won
            self._log(session, user, "修改存放位置", batch, before, {"path": batch.final_path, "won": batch.won}, host)
            return self._response(session, batch, user)

    def dispatch_ingest(self, session: Session, batch: Batch, reviewer: User) -> None:
        """终审通过后的入库任务派发点。WP09 在这里以终审本人身份入队 ingest 任务；本包只停在「入库中」。"""
        return None

    def approve(self, batch_id: int, checklist: dict[str, bool], actor: User, host: str) -> AuditResponse:
        with self.auth.lock, self.auth.database.session() as session:
            batch, user, stage = self._claimed(session, batch_id, actor)
            missing = [CHECKLIST_LABELS[key] for key in CHECKLIST if checklist.get(key) is not True]
            if missing or set(checklist) - set(CHECKLIST):
                raise AppError("checklist_incomplete", "审核清单必须全部勾选才能通过", 422, {"missing": missing})
            before = {"status": batch.status, "round": batch.round}
            row = self._draft(session, batch, stage, user)
            if stage == "final":
                path = batch.final_path or batch.recommended_path
                if not path:
                    info = self._path(session, batch)
                    raise AppError("path_pending", "位置待定，请先从目录树选择存放位置" if info.company_in_tree
                                   else "位置待定：等待管理员在对照表中录入该企业", 409, {"company_in_tree": info.company_in_tree})
                batch.status, batch.final_path, batch.final_reviewer_id = INGESTING, path, user.id
            else:
                batch.status = FINAL
                batch.payload = {**batch.payload, "stage_since": utcnow().isoformat()}
            # 通过时钉图意见作废。
            session.execute(delete(ReviewPin).where(ReviewPin.review_round_id == row.id))
            row.decision, row.checklist, row.comment, row.decided_at = "pass", dict(checklist), None, utcnow()
            self._log(session, user, "初审通过" if stage == "first" else "终审通过", batch, before,
                      {"status": batch.status, "round": batch.round, "final_path": batch.final_path, "checklist": dict(checklist)}, host)
            if stage == "final":
                self.dispatch_ingest(session, batch, user)
            self.notify.emit(session, "first_passed" if stage == "first" else "final_passed", batch)
            return self._response(session, batch, user)

    def reject(self, batch_id: int, comment: str, actor: User, host: str) -> AuditResponse:
        with self.auth.lock, self.auth.database.session() as session:
            batch, user, stage = self._claimed(session, batch_id, actor)
            text = comment.strip()
            if not text:
                raise AppError("comment_required", "驳回必须填写总体意见", 422)
            before = {"status": batch.status, "round": batch.round}
            row = self._draft(session, batch, stage, user)
            row.decision, row.comment, row.decided_at = "reject", text, utcnow()
            session.flush()
            pins = len(list(session.scalars(select(ReviewPin.id).where(ReviewPin.review_round_id == row.id))))
            batch.status = REJECTED
            self._log(session, user, "初审驳回" if stage == "first" else "终审驳回", batch, before,
                      {"status": batch.status, "round": batch.round, "comment": text, "pins": pins}, host)
            self.notify.emit(session, "rejected", batch)
            return self._response(session, batch, user)
