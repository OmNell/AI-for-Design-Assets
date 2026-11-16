"""永久链接：对外只暴露短 ID，目标由卡片柜编号确定，不含任何 NAS 路径。"""
from __future__ import annotations

import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Link

ALPHABET = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
KINDS = ("product", "file", "project")


def new_link_id(length: int = 10) -> str:
    """去掉易混字符的随机短 ID；约 58 位熵，碰撞由唯一约束兜底。"""
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def ensure_link(session: Session, kind: str, target_id: int) -> Link:
    """同一目标只有一条链接：已存在则原样返回，重复执行不会新增。"""
    if kind not in KINDS:
        raise ValueError("链接类型不支持")
    link = session.scalar(select(Link).where(Link.kind == kind, Link.target_id == target_id))
    if link is None:
        link_id = new_link_id()
        while session.scalar(select(Link.id).where(Link.link_id == link_id)) is not None:
            link_id = new_link_id()
        link = Link(link_id=link_id, kind=kind, target_id=target_id)
        session.add(link)
        session.flush()
    return link


def resolve(session: Session, link_id: str) -> tuple[str, int] | None:
    """返回 (目标类型, 目标 ID)；不存在返回 None。页面跳转在 WP10 接入。"""
    link = session.scalar(select(Link).where(Link.link_id == link_id))
    return (link.kind, link.target_id) if link is not None else None
