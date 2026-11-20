from __future__ import annotations

import shutil
import socket
from pathlib import Path

import pytest
from fastapi import Query
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from app import config as config_module
from app.config import MAC_LOCAL_DSM_TEST_ENV, Settings, load_settings
from app.main import create_app


def test_health_success_and_database_failure(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(settings, log_dir=tmp_path / "logs")
    with TestClient(app) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"version": "0.1.0", "mode": "dev", "database_available": True, "dsm_verify_ssl": True}
        monkeypatch.setattr(app.state.database, "is_available", lambda: False)
        failed = client.get("/api/health")
        assert failed.status_code == 503
        assert failed.json() == {"code": "database_unavailable", "message": "数据库暂时不可用", "detail": None}
    assert "工作台启动完成" in (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")


def test_mac_tls_exception_requires_local_launcher_and_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(config_module, "RELEASE_MANIFEST", tmp_path / "RELEASE.json")
    monkeypatch.setenv(MAC_LOCAL_DSM_TEST_ENV, "1")
    config_file = tmp_path / "config.yaml"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen()
        config_file.write_text(
            f"mode: prod\nserver: {{host: 127.0.0.1, port: {port}}}\nnas: {{dsm_verify_ssl: false}}\n",
            encoding="utf-8",
        )
        config = load_settings(config_file)
        with pytest.raises(ValueError, match="python -m app"):
            with TestClient(create_app(config_file=config_file)):
                pytest.fail("不应启动")
        assert not config.paths.data_dir.exists()

        with TestClient(create_app(config_file=config_file, mac_local_dsm_listener=listener)) as client:
            response = client.get("/api/health")
            assert response.status_code == 200
            assert response.json()["mode"] == "prod"
            assert response.json()["dsm_verify_ssl"] is False
    assert "本机测试环境：DSM 证书验证已关闭" in (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")


def test_static_spa_fallback_and_api_boundary(settings: Settings, tmp_path: Path) -> None:
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<h1>工作台</h1>", encoding="utf-8")
    (static / "assets" / "app.js").write_text("console.log('测试')", encoding="utf-8")
    app = create_app(settings, static_dir=static, log_dir=tmp_path / "logs")
    with TestClient(app) as client:
        for url in ("/", "/future/page"):
            assert client.get(url).text == "<h1>工作台</h1>"
        assert "javascript" in client.get("/assets/app.js").headers["content-type"]
        for url in ("/api", "/api/unknown", "/assets/missing.js", "/%2E%2E/secret.key"):
            assert client.get(url).status_code == 404
            assert set(client.get(url).json()) == {"code", "message", "detail"}
        assert client.post("/api/health").status_code == 405


def test_error_format_and_no_input_disclosure(settings: Settings, tmp_path: Path) -> None:
    app = create_app(settings, log_dir=tmp_path / "logs")
    @app.get("/api/test-validation")
    def validation(number: int = Query()) -> dict[str, int]:
        return {"number": number}
    @app.get("/api/test-error")
    def broken() -> None:
        raise RuntimeError("不应泄露的机密")
    # 后添加的测试路由需排在 SPA 回退路由之前。
    for _ in range(2):
        app.router.routes.insert(0, app.router.routes.pop())
    with TestClient(app, raise_server_exceptions=False) as client:
        invalid = client.get("/api/test-validation?number=不应回显的输入")
        assert invalid.status_code == 422
        assert "不应回显的输入" not in invalid.text
        failed = client.get("/api/test-error")
        assert failed.status_code == 500
        assert failed.json()["code"] == "internal_error"
        assert "不应泄露" not in failed.text
    assert "不应泄露" not in (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")


def test_deleted_data_rebuilt_on_next_start(settings: Settings, tmp_path: Path) -> None:
    app = create_app(settings, log_dir=tmp_path / "logs")
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        old_key = (settings.paths.data_dir / "secret.key").read_bytes()
    shutil.rmtree(settings.paths.data_dir)
    with TestClient(app) as client:
        assert client.get("/api/health").json()["database_available"]
        assert (settings.paths.data_dir / "secret.key").read_bytes() != old_key
        assert "oplog" in inspect(app.state.database.engine).get_table_names()
    assert (settings.paths.data_dir / "app.db").is_file()


def test_missing_build_gives_structured_error(settings: Settings, tmp_path: Path) -> None:
    with TestClient(create_app(settings, static_dir=tmp_path / "missing", log_dir=tmp_path / "logs")) as client:
        assert client.get("/").status_code == 404
        assert client.get("/api/health").status_code == 200
