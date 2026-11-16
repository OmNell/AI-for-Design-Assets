"""历次审核意见的只读视图；核对页与审核页共用。"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import BatchProduct, ReviewPin, ReviewRound, User
from app.schemas.review import PinView, RoundView


def pin_views(session: Session, round_id: int) -> list[PinView]:
    views: list[PinView] = []
    for pin in session.scalars(select(ReviewPin).where(ReviewPin.review_round_id == round_id).order_by(ReviewPin.id)):
        product = session.get(BatchProduct, pin.batch_product_id)
        views.append(PinView(batch_product_id=pin.batch_product_id, product_name=product.display_name if product else "",
                             x=pin.x, y=pin.y, text=pin.text))
    return views


def round_history(session: Session, batch_id: int) -> list[RoundView]:
    """只返回已作出决定的记录，最新的在前；未决定的草稿不对上传人可见。"""
    rows = session.scalars(select(ReviewRound).where(ReviewRound.batch_id == batch_id, ReviewRound.decision.is_not(None))
                           .order_by(ReviewRound.decided_at.desc(), ReviewRound.id.desc()))
    result: list[RoundView] = []
    for row in rows:
        reviewer = session.get(User, row.reviewer_id)
        result.append(RoundView(round=row.round, stage=row.stage, decision=str(row.decision), comment=row.comment,
            reviewer_name=(reviewer.display_name or reviewer.username) if reviewer else "", decided_at=row.decided_at,
            pins=pin_views(session, row.id) if row.decision == "reject" else []))
    return result
