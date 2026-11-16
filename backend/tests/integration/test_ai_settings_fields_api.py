from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import DevUser, Settings
from app.db.models import Setting
from app.main import create_app


@pytest.fixture
def admin_client(settings: Settings, tmp_path: Path) -> Iterator[TestClient]:
    settings.bootstrap_admin = "admin"
    settings.paths.dev_nas_dir = tmp_path / "nas"
    settings.dev_users = [DevUser(username=name, password="fake", permissions={"temp": "write"}) for name in ("admin", "one")]
    application = create_app(settings, log_dir=tmp_path / "logs")
    with TestClient(application) as client:
        assert client.post('/api/auth/login', json={"username": "admin", "password": "fake"}).status_code == 200
        yield client


def test_settings_get_put_encrypted_key_and_validation(admin_client: TestClient) -> None:
    client = admin_client
    response = client.get('/api/settings/ai')
    assert response.status_code == 200 and response.json()["api_key_set"] is False
    body = {key: value for key, value in response.json().items() if key != 'api_key_set'}
    response = client.put('/api/settings/ai', json={**body, "api_key": "private-key", "model_vision": "fake-vision"})
    assert response.status_code == 200 and response.json()["api_key_set"] is True
    assert 'private-key' not in response.text and 'api_key_enc' not in response.text
    with client.app.state.database.session() as session:
        stored = session.scalar(select(Setting).where(Setting.key == 'ai'))
        assert stored.value['api_key_enc'] != 'private-key'
        assert client.app.state.cipher.decrypt(stored.value['api_key_enc']) == 'private-key'
    assert client.put('/api/settings/ai', json={**body, "api_key": "", "concurrency": 0}).status_code == 422
    assert client.put('/api/settings/ai', json={**body, "api_key": "", "base_url": "https://user:key@example.test"}).status_code == 422


def test_fields_get_put_soft_delete_version_and_validation(admin_client: TestClient) -> None:
    client = admin_client
    response = client.get('/api/admin/tag-fields')
    assert response.status_code == 200
    before = response.json()
    assert len(before['fields']) == 11 and before['version'] == 1
    body = {"version": 1, "fields": [*before['fields'][1:], {"key": "custom", "label": "新增字段", "hint": "新增说明", "sort_order": 0, "enabled": True}]}
    response = client.put('/api/admin/tag-fields', json=body)
    assert response.status_code == 200 and response.json()['version'] == 2
    assert next(field for field in response.json()['fields'] if field['key'] == 'category')['enabled'] is False
    assert client.put('/api/admin/tag-fields', json=body).status_code == 409
    duplicate = response.json()
    duplicate['fields'].append(duplicate['fields'][0])
    assert client.put('/api/admin/tag-fields', json=duplicate).status_code == 422


def test_settings_and_fields_require_admin_and_login(admin_client: TestClient) -> None:
    client = admin_client
    assert client.post('/api/auth/login', json={"username": "one", "password": "fake"}).status_code == 200
    for url in ('/api/settings/ai', '/api/admin/tag-fields'):
        assert client.get(url).status_code == 403
        assert client.put(url, json={}).status_code == 403
    client.cookies.clear()
    assert client.get('/api/settings/ai').status_code == 401
    assert client.get('/api/admin/tag-fields').status_code == 401
