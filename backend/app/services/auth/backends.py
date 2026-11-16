"""身份验证不创建工作台登录会话；DSM SID 仅用于立即注销。"""
from __future__ import annotations

import hmac
import re
import ssl
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import DevUser, NasConfig


class AuthUnavailable(Exception):
    """认证服务不可用或协议异常，不视为密码错误。"""


class AuthBackend(Protocol):
    def verify(self, username: str, password: str) -> bool: ...


class FakeAuthBackend:
    def __init__(self, users: list[DevUser]) -> None:
        self._users = {user.username: user.model_copy(deep=True) for user in users}

    def verify(self, username: str, password: str) -> bool:
        user = self._users.get(username)
        expected = user.password.get_secret_value() if user is not None else ""
        matches = hmac.compare_digest(password.encode("utf-8"), expected.encode("utf-8"))
        return user is not None and matches


class DsmReply(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    success: bool
    data: dict[str, object] = Field(default_factory=dict)
    error: dict[str, object] = Field(default_factory=dict)


class DsmAuthBackend:
    def __init__(self, config: NasConfig, transport: httpx.BaseTransport | None = None) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", config.host):
            raise ValueError("DSM 地址无效")
        self._config = config.model_copy(deep=True)
        self._transport = transport

    @staticmethod
    def _post(client: httpx.Client, path: str, data: dict[str, str]) -> DsmReply:
        response = client.post(path, data=data)
        response.raise_for_status()
        return DsmReply.model_validate(response.json())

    def verify(self, username: str, password: str) -> bool:
        config = self._config
        sid: str | None = None
        auth_path = "entry.cgi"
        version = "6"
        try:
            verify: bool | ssl.SSLContext = True
            if config.dsm_ca_file is not None:
                verify = ssl.create_default_context(cafile=str(config.dsm_ca_file))
            with httpx.Client(
                base_url=f"{'https' if config.dsm_https else 'http'}://{config.host}:{config.dsm_port}/webapi/",
                timeout=config.timeout_s, verify=verify, trust_env=False,
                follow_redirects=False, transport=self._transport,
            ) as client:
                try:
                    info = self._post(client, "entry.cgi", {
                        "api": "SYNO.API.Info", "version": "1", "method": "query", "query": "SYNO.API.Auth",
                    })
                    if not info.success:
                        raise AuthUnavailable("DSM 认证接口发现失败")
                    auth = info.data.get("SYNO.API.Auth")
                    if not isinstance(auth, dict):
                        raise AuthUnavailable("DSM 认证接口响应无效")
                    path = auth.get("path")
                    if not isinstance(path, str) or not re.fullmatch(r"(?:[A-Za-z0-9_]+/)*[A-Za-z0-9_]+\.cgi", path):
                        raise AuthUnavailable("DSM 认证接口路径无效")
                    auth_path = path
                    maximum, minimum = auth.get("maxVersion"), auth.get("minVersion")
                    if not isinstance(maximum, int) or not isinstance(minimum, int) or min(maximum, 6) < max(minimum, 3):
                        raise AuthUnavailable("DSM 认证接口版本不兼容")
                    version = str(min(maximum, 6))
                    response = self._post(client, auth_path, {
                        "api": "SYNO.API.Auth", "version": version, "method": "login",
                        "account": username, "passwd": password, "session": "DSM", "format": "sid",
                    })
                    if not response.success:
                        code = response.error.get("code")
                        if isinstance(code, int) and 400 <= code < 500:
                            return False
                        raise AuthUnavailable("DSM 认证服务返回错误")
                    value = response.data.get("sid")
                    if not isinstance(value, str) or not value:
                        raise AuthUnavailable("DSM 未返回有效登录令牌")
                    sid = value
                    return True
                finally:
                    if sid is not None:
                        response = self._post(client, auth_path, {
                            "api": "SYNO.API.Auth", "version": version, "method": "logout",
                            "session": "DSM", "_sid": sid,
                        })
                        if not response.success:
                            raise AuthUnavailable("DSM 注销失败，认证未完成")
                    sid = None
                    client.cookies.clear()
        except AuthUnavailable:
            raise
        except Exception:
            # 不附带上游异常链或请求体，避免凭据进入错误响应与日志。
            raise AuthUnavailable("DSM 认证服务暂时不可用") from None
