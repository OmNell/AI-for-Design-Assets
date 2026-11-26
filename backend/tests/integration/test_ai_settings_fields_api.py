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


def test_fields_get_put_rejects_identity_changes_and_stale_versions(admin_client: TestClient) -> None:
    client = admin_client
    response = client.get('/api/admin/tag-fields')
    assert response.status_code == 200
    before = response.json()
    assert len(before['fields']) == 11
    items = [{key: field[key] for key in ("key", "hint", "sort_order")} for field in before["fields"]]
    body = {"version": before["version"], "fields": items}
    for bad in (items[1:], [*items, {"key": "custom", "hint": "说明", "sort_order": 20}],
                [{**items[0], "label": "新名"}, *items[1:]], [{**items[0], "enabled": False}, *items[1:]],
                [items[0], items[0], *items[2:]]):
        assert client.put('/api/admin/tag-fields', json={**body, "fields": bad}).status_code == 422
    assert client.get('/api/admin/tag-fields').json() == before
    items[0]["hint"] = "新的填写说明"
    response = client.put('/api/admin/tag-fields', json=body)
    assert response.status_code == 200 and response.json()['version'] == before["version"] + 1
    assert response.json()['fields'][0]['hint'] == "新的填写说明"
    assert client.put('/api/admin/tag-fields', json=body).status_code == 409


def test_settings_and_fields_require_admin_and_login(admin_client: TestClient) -> None:
    client = admin_client
    assert client.post('/api/auth/login', json={"username": "one", "password": "fake"}).status_code == 200
    for url in ('/api/settings/ai', '/api/admin/tag-fields'):
        assert client.get(url).status_code == 403
        assert client.put(url, json={}).status_code == 403
    client.cookies.clear()
    assert client.get('/api/settings/ai').status_code == 401
    assert client.get('/api/admin/tag-fields').status_code == 401
