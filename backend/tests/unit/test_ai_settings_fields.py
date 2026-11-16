from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select

from app.config import Settings
from app.db.database import Database
from app.db.models import Batch, BatchFile, BatchProduct, Oplog, Setting, TagField, User
from app.errors import AppError
from app.schemas.ai import AiSettingsWrite, TagFieldItem, TagFieldsWrite
from app.services.ai.fields import TagFieldService
from app.services.ai.settings import AiSettingsService
from app.services.auth.backends import FakeAuthBackend
from app.services.auth.service import AuthService
from app.services.crypto import SecretCipher
from app.services.nas.factory import create_session_pool


@pytest.fixture
def services(database: Database, settings: Settings, tmp_path: Path) -> Iterator[tuple[AiSettingsService, TagFieldService, User]]:
    with database.session() as session:
        actor = User(username="admin", roles=["admin"])
        session.add(actor)
        session.flush()
    settings.paths.dev_nas_dir = tmp_path / "nas"
    with create_session_pool(settings) as pool:
        auth = AuthService(database, pool, FakeAuthBackend([]), settings)
        yield AiSettingsService(auth, SecretCipher(settings.paths.data_dir)), TagFieldService(auth), actor


def ai_body(**changes: object) -> AiSettingsWrite:
    return AiSettingsWrite.model_validate({"base_url": "https://example.test/compatible-mode/v1", "api_key": "test-secret-key",
        "model_vision": "vision", "model_text": "text", "model_embed": "embed", "concurrency": 3,
        "timeout_s": 60, "retries": 3, "stall_alert_min": 30, **changes})


def test_seed_fields_match_business_hints_and_have_no_vocabularies(services: tuple[AiSettingsService, TagFieldService, User]) -> None:
    _, fields, actor = services
    state = fields.read(actor)
    assert state.version == 1 and len(state.fields) == 11
    assert [(field.key, field.label, field.hint) for field in state.fields] == [
        ("category", "品类", "这是什么东西"), ("material", "材质", "主要材料"), ("color", "主色", "主要颜色"),
        ("style", "风格", "设计风格"), ("festival", "节日场景", "适用的具体节日；不针对节日就写「通用」"),
        ("audience", "受众群体", "给谁用、送给谁"), ("usage_scene", "使用场景", "用途和使用的场合"),
        ("size", "尺寸", "大小的描述"), ("craft", "工艺", "制作或装饰工艺"),
        ("material_name", "物料名称", "物料的称呼"), ("structure", "结构", "组成部分和形态结构")]
    assert all(field.enabled for field in state.fields)
    assert not any("options" in field.model_dump() or "required" in field.model_dump() for field in state.fields)


@pytest.mark.parametrize("retained", [None, ""])
def test_settings_encrypt_and_keep_key_without_public_or_oplog_leak(services: tuple[AiSettingsService, TagFieldService, User], retained: str | None) -> None:
    ai, _, actor = services
    initial = ai.read(actor)
    assert not initial.api_key_set
    saved = ai.save(actor, ai_body(), "本机")
    assert saved.api_key_set
    assert "test-secret-key" not in saved.model_dump_json() and "api_key_enc" not in saved.model_dump()
    with ai.auth.database.session() as session:
        row = session.scalar(select(Setting).where(Setting.key == "ai"))
        assert row.encrypted and isinstance(row.value, dict)
        encrypted = row.value["api_key_enc"]
        assert encrypted != "test-secret-key" and ai.cipher.decrypt(encrypted) == "test-secret-key"
        entries = list(session.scalars(select(Oplog)))
        assert "test-secret-key" not in json.dumps([(entry.before, entry.after) for entry in entries])
        assert encrypted not in json.dumps([(entry.before, entry.after) for entry in entries])
    ai.save(actor, ai_body(api_key=retained, concurrency=2), "本机")
    effective = ai.effective()
    assert effective.concurrency == 2 and ai.cipher.decrypt(effective.api_key_enc.get_secret_value()) == "test-secret-key"
    assert effective.model_vision == "vision" and effective.timeout_s == 60


def test_config_fallback_and_database_override(services: tuple[AiSettingsService, TagFieldService, User]) -> None:
    ai, _, actor = services
    ai.auth.config.ai.concurrency = 7
    ai.auth.config.ai.model_vision = "config-vision"
    ai.auth.config.ai.api_key_enc = SecretStr(ai.cipher.encrypt("config-key"))
    assert ai.effective().concurrency == 7 and ai.read(actor).api_key_set
    ai.save(actor, ai_body(api_key=None), "本机")
    assert ai.effective().concurrency == 3
    assert ai.cipher.decrypt(ai.effective().api_key_enc.get_secret_value()) == "config-key"


def test_fields_save_version_order_disable_and_keep_historical_values(services: tuple[AiSettingsService, TagFieldService, User]) -> None:
    ai, fields, actor = services
    with ai.auth.database.session() as session:
        batch = Batch(batch_no="B1", project_code="P", kind="ingest", status="待核对", owner_id=actor.id,
                      uploader_id=actor.id, temp_path="B1", won=True)
        session.add(batch); session.flush()
        file = BatchFile(batch_id=batch.id, rel_path="单品/图片.jpg", size=1, mtime=1)
        session.add(file); session.flush()
        product = BatchProduct(batch_id=batch.id, batch_file_id=file.id, kind="item", display_name="图片",
                               tags={"category": {"value": "历史值"}})
        session.add(product); session.flush()
        product_id = product.id
    state = fields.read(actor)
    items = [item for item in state.fields if item.key != "category"]
    items[0].label = "材料"
    items[0].hint = "新的材料说明"
    items[0].sort_order = 20
    items.append(TagFieldItem(key="custom", label="新增字段", hint="原样进入提示词", sort_order=0))
    after = fields.save(actor, TagFieldsWrite(version=state.version, fields=items), "本机")
    assert after.version == 2 and after.fields[0].key == "category" and not after.fields[0].enabled
    assert "category" not in {item["key"] for item in fields.enabled_fields()}
    assert fields.enabled_fields()[0]["key"] == "custom"
    assert next(item for item in after.fields if item.key == "material").hint == "新的材料说明"
    with ai.auth.database.session() as session:
        assert session.get(BatchProduct, product_id).tags == {"category": {"value": "历史值"}}
        assert {row.version for row in session.scalars(select(TagField))} == {2}
        assert "修改标签字段" in list(session.scalars(select(Oplog.action)))
    unchanged = fields.save(actor, TagFieldsWrite(version=after.version, fields=after.fields), "本机")
    assert unchanged.version == 3


def test_stale_version_and_concurrent_saves(services: tuple[AiSettingsService, TagFieldService, User]) -> None:
    _, fields, actor = services
    state = fields.read(actor)
    body = TagFieldsWrite(version=state.version, fields=state.fields)
    def save(_: int) -> int:
        try:
            return fields.save(actor, body, "本机").version
        except AppError as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, range(2))) == [2, 409]


def test_current_admin_role_rechecked(services: tuple[AiSettingsService, TagFieldService, User]) -> None:
    ai, fields, actor = services
    with ai.auth.database.session() as session:
        session.get(User, actor.id).roles = ["uploader"]
    for action in (lambda: ai.read(actor), lambda: ai.save(actor, ai_body(), "本机"), lambda: fields.read(actor),
                   lambda: fields.save(actor, TagFieldsWrite(version=1, fields=[]), "本机")):
        with pytest.raises(AppError) as failure:
            action()
        assert failure.value.status_code == 403


@pytest.mark.parametrize("base_url", ["file:///tmp/api", "https://user:secret@example.test/v1", "https://example.test/v1?key=secret"])
def test_settings_rejects_credential_urls(base_url: str) -> None:
    with pytest.raises(ValidationError):
        ai_body(base_url=base_url)


def test_duplicate_keys_and_nonenglish_keys_rejected() -> None:
    field = {"key": "category", "label": "品类", "hint": "任意", "sort_order": 0}
    with pytest.raises(ValidationError):
        TagFieldsWrite.model_validate({"version": 1, "fields": [field, field]})
    with pytest.raises(ValidationError):
        TagFieldItem.model_validate({**field, "key": "中文"})
