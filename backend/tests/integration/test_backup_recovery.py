"""以独立目标库演练 NAS 备份验证、迁移、会话废止及访问。"""
from hashlib import sha256
import json
import struct
from zipfile import ZipFile

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.config import load_settings
from app.db.models import Link, Product, ProductEmbedding, Setting, UserSession
from app.main import create_app
from app.services.auth.service import COOKIE_NAME
from app.services.backup.recovery import list_candidates, stage_backup, stage_from_nas
from app.services.nas import Region
from app.services.nas.factory import create_paths
from test_maintenance import maintained


def test_isolated_backup_staging_preserves_assets_and_rejects_old_login(maintained, tmp_path, monkeypatch):
    flow, project, settings, source_config = maintained
    app = flow.client.app
    cipher = app.state.cipher
    with app.state.database.session() as session:
        session.add(Setting(key="ai", value={"api_key_enc": cipher.encrypt("模型测试密钥")}, encrypted=True))
        product = session.scalar(select(Product).where(Product.project_id == project.id))
        link = session.scalar(select(Link.link_id).where(Link.kind == "product", Link.target_id == product.id))
    backup = flow.call("admin", "POST", "/api/admin/maintenance/backup/run")
    assert backup.status_code == 202, backup.text
    assert app.state.jobs.wait_idle(8)
    existing = flow.call("admin", "GET", "/api/admin/backups").json()[0]
    fs = app.state.auth.pool.system()
    root = create_paths(settings).root(Region.BACKUP)
    broken = settings.paths.dev_nas_dir / root / "wb-backup-20990101-0000.zip"
    broken.write_bytes(b"corrupt")
    candidates = list_candidates(fs, root, verify_manifest=True)
    assert [(item["name"], item["format"], item["validation"]) for item in candidates] == [
        (broken.name, None, "invalid"), (existing["name"], 1, "manifest_checked"),
    ]
    assert candidates[1]["size"] == existing["size"]
    assert candidates[1]["modified_at"]
    target = tmp_path / "backup-stage-environment"
    with pytest.raises(ValueError, match="备份文件"):
        stage_from_nas(fs, root, "../bad.zip", target, destination_config=settings)
    report = stage_from_nas(fs, root, existing["name"], target, destination_config=settings)
    assert report["inventory"] == report["source_inventory"] and report["sessions_revoked"] >= 1
    assert report["inventory"]["projects"]["count"] == 1 and report["inventory"]["product_embeddings"]["count"] >= 1
    assert (target / "source-config.yaml").read_bytes() == source_config.read_bytes()
    staged = load_settings(target / "config.yaml")
    assert staged.recovery.read_only is True
    assert staged.paths.data_dir != settings.paths.data_dir and staged.paths.dev_nas_dir == settings.paths.dev_nas_dir
    with TestClient(create_app(staged, config_file=target / "config.yaml")) as recovered:
        assert recovered.get("/api/auth/me", headers={"Cookie": f"{COOKIE_NAME}={flow.cookies['one']}"}).status_code == 401
        assert recovered.post("/api/auth/login", json={"username": "one", "password": "fake"}).status_code == 200
        detail = recovered.get(f"/api/products/{product.id}").json()
        assert detail["id"] == product.id and len(detail["tags"]) == 11
        assert recovered.get("/l/" + link).json()["target_id"] == product.id
        assert recovered.get(f"/api/files/{product.file_id}/preview").status_code == 200
        assert recovered.get(f"/api/files/{product.file_id}/download").status_code == 200
        quick = recovered.post("/api/search", json={"query": product.product_code, "mode": "quick"}).json()
        assert quick["products"][0]["id"] == product.id and quick["usage"] == []
        with recovered.app.state.database.session() as session:
            vector = session.scalar(select(ProductEmbedding).where(ProductEmbedding.product_id == product.id))
            expected = list(struct.unpack(f"<{vector.dimension}f", vector.embedding))
        model = recovered.app.state.ai.client()
        monkeypatch.setattr(model, "embed", lambda *_: expected)
        monkeypatch.setattr(model, "search_chat", lambda task, _: json.dumps(
            {"fields": {}, "keywords": [], "exclude": []} if task == "query"
            else {"items": [{"index": 1, "reason": "接近已恢复的向量"}]}))
        ai = recovered.post("/api/search", json={"query": "查找已确认的杯类商品", "mode": "semantic"}).json()
        assert not ai["exact"] and ai["vector_candidates"] >= 1, ai
        assert any(row["id"] == product.id for row in ai["products"]), ai
        with recovered.app.state.database.session() as session:
            rows = list(session.scalars(select(UserSession)))
            assert sum(row.revoked_at is None for row in rows) == 1
            key = session.scalar(select(Setting).where(Setting.key == "ai")).value["api_key_enc"]
            assert recovered.app.state.cipher.decrypt(key) == "模型测试密钥"


def test_backup_with_matching_member_hashes_but_wrong_crypto_key_is_rejected(maintained, tmp_path):
    flow, _, _, _ = maintained
    app = flow.client.app
    with app.state.database.session() as session:
        session.add(Setting(key="ai", value={"api_key_enc": app.state.cipher.encrypt("源模型密钥")}, encrypted=True))
    assert flow.call("admin", "POST", "/api/admin/maintenance/backup/run").status_code == 202
    assert app.state.jobs.wait_idle(8)
    record = flow.call("admin", "GET", "/api/admin/backups").json()[0]
    backup = flow.call("admin", "GET", f"/api/admin/backups/{record['id']}/download")
    assert backup.status_code == 200
    original = tmp_path / "original.zip"
    original.write_bytes(backup.content)
    swapped = tmp_path / "different-key.zip"
    with ZipFile(original) as source, ZipFile(swapped, "w") as target:
        manifest = json.loads(source.read("manifest.json"))
        key = Fernet.generate_key()
        manifest["files"]["secret.key"] = {"size": len(key), "sha256": sha256(key).hexdigest()}
        target.writestr("manifest.json", json.dumps(manifest))
        for name in ("app.db", "config.yaml"):
            target.writestr(name, source.read(name))
        target.writestr("secret.key", key)
    isolated = tmp_path / "failed-stage"
    with pytest.raises(ValueError, match="原密钥"):
        stage_backup(swapped, isolated)
    assert not isolated.exists()
