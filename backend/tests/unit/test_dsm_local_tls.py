import socket
import ssl
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import ValidationError

from app import __main__ as launcher
from app import config as config_module
from app.config import MAC_LOCAL_DSM_TEST_ENV, NasConfig, Settings, load_settings, require_dsm_test_listener
from app.services.auth.backends import DsmAuthBackend
from app.services.nas.factory import create_auth_backend
from app.services.nas_settings import public
from app.services.nas_settings.probe import Connections


@pytest.fixture
def mac_test(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config_module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(config_module, "RELEASE_MANIFEST", tmp_path / "RELEASE.json")
    monkeypatch.setattr(config_module, "RELEASE_MARKER", tmp_path / "_release.txt")
    monkeypatch.setenv(MAC_LOCAL_DSM_TEST_ENV, "1")


def local_settings(port: int = 8081, **nas_fields: bool) -> Settings:
    return Settings.model_validate({"mode": "prod", "server": {"host": "127.0.0.1", "port": port},
                                    "nas": {"dsm_verify_ssl": False, **nas_fields}})


def test_default_tls_validates_certificate_and_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MAC_LOCAL_DSM_TEST_ENV, raising=False)
    nas = NasConfig()
    assert nas.dsm_verify_ssl is True
    contexts: list[ssl.SSLContext] = []
    client_type = httpx.Client

    def mock_client(**options):
        contexts.append(options["verify"])
        return client_type(**options)

    monkeypatch.setattr("app.services.auth.backends.httpx.Client", mock_client)
    with DsmAuthBackend(nas)._client() as client:
        assert client.base_url.scheme == "https"
    context, = contexts
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


@pytest.mark.parametrize("value", [None, "true", "0"])
def test_mac_tls_exception_requires_explicit_environment_authorization(mac_test, monkeypatch, value):
    if value is None:
        monkeypatch.delenv(MAC_LOCAL_DSM_TEST_ENV)
    else:
        monkeypatch.setenv(MAC_LOCAL_DSM_TEST_ENV, value)
    with pytest.raises(ValidationError, match=MAC_LOCAL_DSM_TEST_ENV + "=1"):
        local_settings()


@pytest.mark.parametrize("host", ["0.0.0.0", "localhost", "::1", "192.168.0.23"])
def test_mac_tls_exception_rejects_non_loopback_config(mac_test, host):
    with pytest.raises(ValidationError, match="127.0.0.1"):
        Settings.model_validate({"mode": "prod", "server": {"host": host}, "nas": {"dsm_verify_ssl": False}})


@pytest.mark.parametrize("system", ["Linux", "Windows"])
def test_tls_exception_rejects_other_platforms(mac_test, monkeypatch, system):
    monkeypatch.setattr(config_module.platform, "system", lambda: system)
    with pytest.raises(ValidationError, match="只有 Mac"):
        local_settings()


@pytest.mark.parametrize("marker", ["RELEASE_MANIFEST", "RELEASE_MARKER"])
def test_tls_exception_rejects_release_and_windows_template_is_strict(mac_test, marker):
    getattr(config_module, marker).touch()
    with pytest.raises(ValidationError, match="只有 Mac 本机的源码测试"):
        local_settings()
    production = Path(__file__).resolve().parents[2] / "config.production.example.yaml"
    assert load_settings(production).nas.dsm_verify_ssl is True


def test_mac_tls_exception_requires_prod_and_https(mac_test):
    with pytest.raises(ValidationError, match="必须以 prod 模式"):
        Settings.model_validate({"server": {"host": "127.0.0.1"}, "nas": {"dsm_verify_ssl": False}})
    with pytest.raises(ValidationError, match="必须使用 HTTPS"):
        local_settings(dsm_https=False)
    invalid_nas = local_settings().nas.model_copy(update={"dsm_https": False})
    with pytest.raises(ValueError, match="必须使用 HTTPS"):
        DsmAuthBackend(invalid_nas)._client()


def test_mac_tls_exception_requires_bound_loopback_tcp_socket(mac_test):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        with pytest.raises(ValueError, match="python -m app"):
            require_dsm_test_listener(local_settings(), listener)
        listener.bind(("127.0.0.1", 0))
        config = local_settings(port=listener.getsockname()[1])
        with pytest.raises(ValueError, match="python -m app"):
            require_dsm_test_listener(local_settings(port=1), listener)
        require_dsm_test_listener(config, listener)
    with pytest.raises(ValueError, match="python -m app"):
        require_dsm_test_listener(config, listener)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as wildcard:
        wildcard.bind(("0.0.0.0", 0))
        with pytest.raises(ValueError, match="127.0.0.1"):
            require_dsm_test_listener(local_settings(port=wildcard.getsockname()[1]), wildcard)


def test_mac_launcher_passes_the_actual_loopback_socket(mac_test, monkeypatch):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as available:
        available.bind(("127.0.0.1", 0))
        config = local_settings(port=available.getsockname()[1])
    monkeypatch.setenv("UVICORN_HOST", "0.0.0.0")
    monkeypatch.setattr(launcher, "load_settings", lambda: config)
    used = []

    def run(server, sockets):
        listener, = sockets
        require_dsm_test_listener(config, listener)
        assert (server.config.host, server.config.proxy_headers, server.config.lifespan) == ("127.0.0.1", False, "on")
        used.append(listener)

    monkeypatch.setattr(launcher.uvicorn.Server, "run", run)
    launcher.main()
    assert len(used) == 1 and used[0].fileno() == -1


def test_login_discovery_and_nas_settings_share_mac_tls_policy(mac_test, monkeypatch):
    config = local_settings()
    calls = []
    contexts = []

    def request(req: httpx.Request) -> httpx.Response:
        assert req.url.scheme == "https" and req.url.port == 5001
        body = parse_qs(req.content.decode())
        method, = body["method"]
        calls.append(method)
        match method:
            case "query":
                data = {"SYNO.API.Auth": {"path": "auth.cgi", "minVersion": 3, "maxVersion": 7}}
            case "login":
                assert (body["account"], body["passwd"]) == (["test-user"], ["test-password"])
                data = {"sid": "test-sid"}
            case "logout":
                assert body["_sid"] == ["test-sid"]
                data = {}
            case _:
                pytest.fail(f"未知 DSM 方法 {method}")
        return httpx.Response(200, json={"success": True, "data": data})

    client = httpx.Client

    def mock_client(**options):
        contexts.append(options["verify"])
        assert options["trust_env"] is False and options["follow_redirects"] is False
        options["transport"] = httpx.MockTransport(request)
        return client(**options)

    monkeypatch.setattr("app.services.auth.backends.httpx.Client", mock_client)
    assert create_auth_backend(config).verify("test-user", "test-password")
    Connections(config).dsm(config.nas, "test-user", "test-password")
    assert calls == ["query", "login", "logout", "query", "query", "login", "logout"]
    assert len(contexts) == 3
    assert all(ctx.verify_mode == ssl.CERT_NONE and not ctx.check_hostname for ctx in contexts)
    assert public(config.nas).certificate == "mac_test_disabled"
