"""同一轮的初审与终审由不同账号完成。"""
from app.db.models import Batch, User


def recused(batch: Batch, user: User) -> bool:
    return batch.status == "待终审" and batch.first_reviewer_id == user.id
