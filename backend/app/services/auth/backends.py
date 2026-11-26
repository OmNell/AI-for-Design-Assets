"""开发模式假账号校验。"""
from __future__ import annotations

import hmac

from app.config import DevUser


class FakeAuthBackend:
    def __init__(self, users: list[DevUser]) -> None:
        self._users = {user.username: user.model_copy(deep=True) for user in users}

    def verify(self, username: str, password: str) -> bool:
        user = self._users.get(username)
        expected = user.password.get_secret_value() if user is not None else ""
        matches = hmac.compare_digest(password.encode("utf-8"), expected.encode("utf-8"))
        return user is not None and matches
