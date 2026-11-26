from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import load_settings
from app.services.crypto import CryptoError, SecretCipher


def test_config_example_and_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    example = Path(__file__).resolve().parents[2] / "config.example.yaml"
    config_file = tmp_path / "config.yaml"
    config_file.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("NASWB_SERVER__PORT", "8090")
    monkeypatch.setenv("NASWB_BOOTSTRAP_ADMIN", "测试管理员")
    settings = load_settings(config_file)
    assert settings.mode == "dev" and settings.server.port == 8090
    assert settings.bootstrap_admin == "测试管理员"
    assert settings.paths.data_dir == tmp_path / "data"
    assert settings.nas.root_path == "/设计资产"
    assert settings.ai.timeout_s == 60 and settings.upload.chunk_mb == 8


def test_config_env_file_and_secret_repr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text('mode: dev\nnas:\n  system_password_enc: ciphertext-not-plaintext\n', encoding="utf-8")
    monkeypatch.setenv("NASWB_CONFIG", str(config_file))
    settings = load_settings()
    assert "ciphertext-not-plaintext" not in repr(settings)
    assert settings.nas.system_password_enc.get_secret_value() == "ciphertext-not-plaintext"


@pytest.mark.parametrize("contents", ["mode: invalid", "server:\n  port: 0", "unknown: true"])
def test_bad_config_is_rejected(tmp_path: Path, contents: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(ValidationError):
        load_settings(path)


def test_missing_config_does_not_silently_start(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="配置文件不存在"):
        load_settings(tmp_path / "missing.yaml")


def test_crypto_roundtrip_key_reuse_and_permissions(tmp_path: Path) -> None:
    cipher = SecretCipher(tmp_path / "data")
    key = tmp_path / "data" / "secret.key"
    original = key.read_bytes()
    ciphertext = cipher.encrypt("测试机密🔒")
    assert "测试机密" not in ciphertext
    assert SecretCipher(key.parent).decrypt(ciphertext) == "测试机密🔒"
    assert key.read_bytes() == original
    if os.name != "nt":
        assert key.stat().st_mode & 0o777 == 0o600
    with pytest.raises(CryptoError, match="解密失败"):
        cipher.decrypt("invalid")
    with pytest.raises(CryptoError):
        SecretCipher(tmp_path / "other").decrypt(ciphertext)


def test_broken_existing_key_is_not_overwritten(tmp_path: Path) -> None:
    key = tmp_path / "secret.key"
    key.write_bytes(b"broken")
    with pytest.raises(CryptoError):
        SecretCipher(tmp_path)
    assert key.read_bytes() == b"broken"


def test_read_only_does_not_create_key_or_rewrite_acl(tmp_path, monkeypatch):
    from app.services import crypto

    target = tmp_path / "new"
    with pytest.raises(CryptoError):
        SecretCipher(target, read_only=True)
    assert not target.exists()
    token = SecretCipher(target).encrypt("测试密文")

    def unexpected_acl_change(path):
        pytest.fail("只读操作修改了密钥 ACL")

    monkeypatch.setattr(crypto, "restrict_key_permissions", unexpected_acl_change)
    assert SecretCipher(target, read_only=True).decrypt(token) == "测试密文"
