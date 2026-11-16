"""企业→行业对照表与目录树；序号只增不重排，不扫描 NAS。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Batch, CompanyIndustryMap, Industry, User
from app.errors import AppError
from app.schemas.audit import CompanyItem, CompanyMap, IndustryItem, TreeCompany, TreeIndustry, TreeRoot
from app.services.auth.service import AuthService
from app.services.nas import NasPermissionDenied, safe_join
from app.services.oplog import OplogService

WON_FOLDERS = {True: "01已中标项目", False: "02未中标项目"}
# 位置仍可能变化的状态：对照表更新后重新计算这些批次的推荐位置。
OPEN_STATUSES = ("AI 处理中", "待核对", "已驳回", "待初审", "待终审")


def folder(seq: int, name: str) -> str:
    return f"{seq:02d}{name}"


def company_path(won: bool, row: CompanyIndustryMap) -> str:
    return safe_join(WON_FOLDERS[won], folder(row.industry_seq, row.industry_name), folder(row.company_seq, row.company_name))


def batch_company(batch: Batch) -> str:
    parts = str(batch.payload.get("folder_name", "")).split("_")
    return parts[1] if len(parts) == 3 else ""


def match_company(session: Session, name: str) -> CompanyIndustryMap | None:
    """标准名或别名唯一命中才算识别；歧义按查不到处理。"""
    matches = [row for row in session.scalars(select(CompanyIndustryMap)) if name and (row.company_name == name or name in row.aliases)]
    return matches[0] if len(matches) == 1 else None


def recommend(session: Session, batch: Batch) -> str | None:
    row = match_company(session, batch_company(batch))
    if row is None:
        return None
    try:
        return company_path(batch.won, row)
    except NasPermissionDenied:
        return None


def _segment(value: str, what: str) -> str:
    name = value.strip()
    try:
        if not name or "/" in name or "\\" in name or "_" in name or safe_join(name) != name:
            raise NasPermissionDenied("名称非法")
    except NasPermissionDenied:
        raise AppError("invalid_name", f"{what}名称含非法字符", 422, {"name": value}) from None
    return name


class CompanyMapService:
    def __init__(self, auth: AuthService) -> None:
        self.auth = auth

    @staticmethod
    def _admin(session: Session, actor: User) -> User:
        user = session.get(User, actor.id)
        if user is None or not user.enabled or "admin" not in user.roles:
            raise AppError("forbidden", "只有管理员可以维护对照表", 403)
        return user

    @staticmethod
    def _read(session: Session) -> CompanyMap:
        industries = list(session.scalars(select(Industry).order_by(Industry.seq)))
        ids = {row.name: row.id for row in industries}
        companies = list(session.scalars(select(CompanyIndustryMap).order_by(CompanyIndustryMap.industry_seq, CompanyIndustryMap.company_seq)))
        return CompanyMap(industries=[IndustryItem(id=row.id, name=row.name, seq=row.seq) for row in industries],
            companies=[CompanyItem(id=row.id, name=row.company_name, aliases=list(row.aliases), seq=row.company_seq,
                                   industry_id=ids.get(row.industry_name), industry_name=row.industry_name) for row in companies])

    def read(self, actor: User) -> CompanyMap:
        with self.auth.lock, self.auth.database.session() as session:
            self._admin(session, actor)
            return self._read(session)

    def save(self, actor: User, body: CompanyMap, host: str) -> CompanyMap:
        """带 id 的条目是修改，不带 id 的是新增并发放下一个序号；未出现的条目保持不变，不提供删除。"""
        with self.auth.lock, self.auth.database.session() as session:
            admin = self._admin(session, actor)
            before = self._read(session).model_dump()
            industries = {row.id: row for row in session.scalars(select(Industry))}
            for item in body.industries:
                name = _segment(item.name, "行业")
                if any(value.name == name and value.id != item.id for value in industries.values()):
                    raise AppError("duplicate_name", f"行业「{name}」已存在", 409)
                if item.id is None:
                    row = Industry(name=name, seq=max((value.seq for value in industries.values()), default=0) + 1)
                    session.add(row)
                    session.flush()
                    industries[row.id] = row
                else:
                    row = industries.get(item.id)
                    if row is None:
                        raise AppError("industry_not_found", "行业不存在", 404)
                    if row.name != name:
                        for company in session.scalars(select(CompanyIndustryMap).where(CompanyIndustryMap.industry_name == row.name)):
                            company.industry_name = name
                        row.name = name
            session.flush()
            by_name = {row.name: row for row in industries.values()}
            companies = {row.id: row for row in session.scalars(select(CompanyIndustryMap))}
            for item in body.companies:
                name = _segment(item.name, "企业")
                aliases = list(dict.fromkeys(alias.strip() for alias in item.aliases if alias.strip() and alias.strip() != name))
                industry = industries.get(item.industry_id) if item.industry_id is not None else by_name.get((item.industry_name or "").strip())
                if industry is None:
                    raise AppError("industry_not_found", f"企业「{name}」所属的行业不存在，请先录入行业", 422)
                row = companies.get(item.id) if item.id is not None else None
                if item.id is not None and row is None:
                    raise AppError("company_not_found", "企业不存在", 404)
                taken = {value for other in companies.values() if other is not row for value in (other.company_name, *other.aliases)}
                clash = sorted(taken & {name, *aliases})
                if clash:
                    raise AppError("duplicate_name", f"「{clash[0]}」已是其他企业的标准名或别名", 409)
                if row is None or row.industry_name != industry.name:
                    # 序号在行业内独立编；新录入或换行业时接在该行业末尾，原有序号不重排。
                    seq = max((other.company_seq for other in companies.values() if other.industry_name == industry.name and other is not row), default=0) + 1
                else:
                    seq = row.company_seq
                if row is None:
                    row = CompanyIndustryMap(company_name=name, aliases=aliases, company_seq=seq,
                                             industry_name=industry.name, industry_seq=industry.seq)
                    session.add(row)
                    session.flush()
                    companies[row.id] = row
                else:
                    row.company_name, row.aliases, row.company_seq = name, aliases, seq
                    row.industry_name, row.industry_seq = industry.name, industry.seq
            session.flush()
            after = self._read(session)
            refreshed = []
            for batch in session.scalars(select(Batch).where(Batch.status.in_(OPEN_STATUSES), Batch.final_path.is_(None))):
                path = recommend(session, batch)
                if path != batch.recommended_path:
                    refreshed.append({"batch_id": batch.id, "before": batch.recommended_path, "after": path})
                    batch.recommended_path = path
            OplogService(session).write(admin.id, ",".join(admin.roles), "修改企业行业对照表", "company_map", None, before,
                                       {**after.model_dump(), "recommended_paths": refreshed}, "成功", host)
            return after

    def tree(self, actor: User) -> list[TreeRoot]:
        with self.auth.lock, self.auth.database.session() as session:
            user = session.get(User, actor.id)
            if user is None or not user.enabled:
                raise AppError("forbidden", "当前账号没有此操作权限", 403)
            data = self._read(session)
            industries = [TreeIndustry(id=industry.id, name=industry.name, folder=folder(industry.seq, industry.name),
                companies=[TreeCompany(id=company.id, name=company.name, folder=folder(company.seq, company.name))
                           for company in data.companies if company.industry_name == industry.name]) for industry in data.industries]
            return [TreeRoot(won=won, folder=WON_FOLDERS[won], industries=industries) for won in (True, False)]
