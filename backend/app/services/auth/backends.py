"""身份验证不创建工作台登录会话；DSM SID 仅用于立即注销。"""
from __future__ import annotations

import hmac
import re
import ssl
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import DevUser, NasConfig, require_mac_dsm_test


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

    def _post(self, client: httpx.Client, path: str, data: dict[str, str]) -> DsmReply:
        extra = {"sni_hostname": self._config.host} if self._config.verified_address else {}
        response = client.post(path, data=data, extensions=extra)
        response.raise_for_status()
        return DsmReply.model_validate(response.json())

    def _client(self) -> httpx.Client:
        config = self._config
        if config.dsm_verify_ssl:
            verify = ssl.create_default_context(cafile=str(config.dsm_ca_file) if config.dsm_ca_file else None)
        else:
            require_mac_dsm_test(config.dsm_https)
            verify = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            verify.check_hostname = False
            verify.verify_mode = ssl.CERT_NONE
        return httpx.Client(
            base_url=f"{'https' if config.dsm_https else 'http'}://{config.verified_address or config.host}:{config.dsm_port}/webapi/",
            headers={"Host": f"{config.host}:{config.dsm_port}"},
            timeout=config.timeout_s, verify=verify, trust_env=False,
            follow_redirects=False, transport=self._transport,
        )

    def _discover(self, client: httpx.Client) -> tuple[str, str]:
        info = self._post(client, "entry.cgi", {
            "api": "SYNO.API.Info", "version": "1", "method": "query", "query": "SYNO.API.Auth",
        })
        auth = info.data.get("SYNO.API.Auth")
        if not info.success or not isinstance(auth, dict):
            raise AuthUnavailable("DSM 认证接口发现失败")
        path = auth.get("path")
        if not isinstance(path, str) or not re.fullmatch(r"(?:[A-Za-z0-9_]+/)*[A-Za-z0-9_]+\.cgi", path):
            raise AuthUnavailable("DSM 认证接口路径无效")
        maximum, minimum = auth.get("maxVersion"), auth.get("minVersion")
        if not isinstance(maximum, int) or not isinstance(minimum, int) or min(maximum, 6) < max(minimum, 3):
            raise AuthUnavailable("DSM 认证接口版本不兼容")
        return path, str(min(maximum, 6))

    def discover(self) -> None:
        try:
            with self._client() as client:
                self._discover(client)
        except AuthUnavailable:
            raise
        except httpx.HTTPStatusError as error:
            raise AuthUnavailable("DSM HTTPS 返回重定向，已拒绝" if error.response.is_redirect else
                                  f"DSM HTTPS 接口返回 HTTP {error.response.status_code}") from None
        except httpx.TimeoutException:
            raise AuthUnavailable("DSM HTTPS 连接超时") from None
        except httpx.ConnectError as error:
            cause = error.__cause__
            if isinstance(cause, Exception) and isinstance(cause.__cause__, ssl.SSLCertVerificationError):
                raise AuthUnavailable("DSM HTTPS 证书验证失败，请核对证书及 CA") from None
            if "CERTIFICATE_VERIFY_FAILED" in str(error):
                raise AuthUnavailable("DSM HTTPS 证书验证失败，请核对证书及 CA") from None
            raise AuthUnavailable("DSM HTTPS 无法连接，请核对地址及端口") from None
        except Exception:
            raise AuthUnavailable("DSM HTTPS 响应无效或 CA 配置不可用") from None

    def verify(self, username: str, password: str) -> bool:
        sid: str | None = None
        try:
            with self._client() as client:
                auth_path, version = self._discover(client)
                try:
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
