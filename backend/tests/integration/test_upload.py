from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.config import DevUser, Settings, PathsConfig
from app.db.models import Batch, BatchFile, PreflightAck, Project, UserSession, utcnow
from app.main import create_app
from app.services.auth.service import COOKIE_NAME


@pytest.fixture
def upload_client(settings: Settings, tmp_path: Path) -> Iterator[TestClient]:
    settings.paths.dev_nas_dir = tmp_path / "nas"
    settings.dev_users = [DevUser(username=name, password="fake", permissions={"temp": "write", "formal": "read"}) for name in ("one", "two")]
    with TestClient(create_app(settings, log_dir=tmp_path / "logs")) as client:
        assert client.post("/api/auth/login", json={"username": "one", "password": "fake"}).status_code == 200
        # 测试配置保持生产结构，仅缩小服务的内存分片数值以覆盖多次顺序写。
        client.app.state.upload.chunk_bytes = 4
        yield client


def body(folder: str = "P001_企业_项目", name: str = "单品/图片.JPG", size: int = 10) -> dict[str, object]:
    return {"folder_name": folder, "won": True, "acks": ["P07", "P08", "P09"], "files": [
        {"rel_path": "单品", "is_dir": True}, {"rel_path": "04授权文件", "is_dir": True},
        {"rel_path": "05 详情页/空文件夹", "is_dir": True},
        {"rel_path": "Thumbs.db", "size": 200, "mtime": 1},
        {"rel_path": name, "size": size, "mtime": 1000},
    ]}


def create(client: TestClient, payload: dict[str, object] | None = None) -> dict:
    response = client.post("/api/batches", json=payload or body())
    assert response.status_code == 200, response.text
    return response.json()


def headers(batch: dict) -> dict[str, str]:
    return {"X-Upload-Token": batch["upload_token"], "Content-Type": "application/octet-stream"}


def file_path(client: TestClient, batch: dict, relative: str = "单品/图片.JPG") -> Path:
    return client.app.state.settings.paths.dev_nas_dir / "工作台临时区" / batch["batch_no"] / batch["folder_name"] / relative


def chunk(client: TestClient, batch: dict, index: int, data: bytes):
    return client.put(f'/api/batches/{batch["id"]}/files/{batch["files"][0]["id"]}/chunks/{index}', content=data, headers=headers(batch))


def finish(client: TestClient, batch: dict):
    return client.post(f'/api/batches/{batch["id"]}/files/{batch["files"][0]["id"]}/complete', headers=headers(batch))


def test_upload_duplicate_out_of_order_complete_confirm(upload_client: TestClient) -> None:
    client = upload_client
    batch = create(client)
    assert len(batch["files"]) == 1
    assert file_path(client, batch, "05 详情页/空文件夹").is_dir()
    assert chunk(client, batch, 1, b"efgh").status_code == 409
    assert chunk(client, batch, 0, b"abcd").json()["bytes_received"] == 4
    assert chunk(client, batch, 0, b"abcd").json()["bytes_received"] == 4
    assert chunk(client, batch, 0, b"xxxx").status_code == 409
    assert finish(client, batch).status_code == 409
    assert chunk(client, batch, 1, b"efgh").status_code == 200
    assert chunk(client, batch, 2, b"ij").status_code == 200
    done = finish(client, batch)
    assert done.status_code == 200 and done.json()["status"] == "已暂存"
    assert finish(client, batch).json()["status"] == "已暂存"
    assert file_path(client, batch).read_bytes() == b"abcdefghij"
    assert done.json()["files"][0]["fingerprint"] == hashlib.sha256(b"abcdefghij").hexdigest()
    assert client.post(f'/api/batches/{batch["id"]}/confirm').json()["status"] == "AI 处理中"
    assert client.post(f'/api/batches/{batch["id"]}/confirm').status_code == 409
    canceled = client.post(f'/api/batches/{batch["id"]}/cancel')
    # WP06 允许处理中撤回；后台坏图也可能已先结束为 AI 处理失败。
    if canceled.status_code == 200:
        assert canceled.json()["status"] == "已撤回"
    else:
        assert canceled.status_code == 409
        assert client.get(f'/api/batches/{batch["id"]}').json()["status"] in ("AI 处理失败", "待核对")
    with client.app.state.database.session() as session:
        assert "P08" in list(session.scalars(select(PreflightAck.rule_code)))
        assert session.scalar(select(Project)) is None


def test_precheck_acks_existing_code_and_tampered_path(upload_client: TestClient) -> None:
    client = upload_client
    invalid = body(folder="不是三段")
    assert any(issue["rule_code"] == "P01" for issue in client.post('/api/batches/precheck', json=invalid).json()["issues"])
    assert client.post('/api/batches', json=invalid).status_code == 422
    payload = body(); payload["acks"] = []
    assert client.post('/api/batches', json=payload).status_code == 422
    assert client.post('/api/batches', json=body(name="../escape")).status_code == 422
    assert client.post('/api/batches', json=body(name="单品/child/pic.png")).status_code == 422
    assert client.post('/api/batches', json=body(name="套装档案/file.png")).status_code == 422
    with client.app.state.database.session() as session:
        session.add(Project(project_code="P001", company_name="企业", project_name="项目", won=True, folder_name="P001_企业_项目"))
    # 编号已在库中：不再拦截，转入补充流程；补充包不提醒缺少资料夹。
    result = client.post('/api/batches/precheck', json=body()).json()
    assert result["code_status"] == "exists" and not [issue for issue in result["issues"] if issue["level"] in ("block", "warn")]
    renamed = client.post('/api/batches/precheck', json=body(folder="P001_企业_另一个名字")).json()
    assert [(issue["rule_code"], issue["level"]) for issue in renamed["issues"] if issue["level"] != "ignore"] == [("P02", "warn")]
    assert "库中为「项目」" in renamed["issues"][0]["message"]
    payload = body(folder="P001_企业_另一个名字"); payload["acks"] = []
    assert client.post('/api/batches', json=payload).status_code == 422
    created = client.post('/api/batches', json={**body(), "acks": []})
    assert created.status_code == 200 and created.json()["kind"] == "supplement" and created.json()["kind_label"] == "补充"
    # 同一项目同时只允许一个未完成的操作批次。
    blocked = client.post('/api/batches/precheck', json=body()).json()
    assert blocked["issues"][0]["level"] == "block" and "未完成的操作批次" in blocked["issues"][0]["message"]
    assert client.post('/api/batches', json=body()).status_code == 422


def test_queue_h_code_cancel(upload_client: TestClient) -> None:
    client = upload_client
    first = create(client, {**body("H_企业_历史"), "historical_year": 2025})
    second = create(client, {**body("H_企业_历史"), "historical_year": 2025})
    assert first["project_code"] == "H2025-0001" and second["project_code"] == "H2025-0002"
    assert second["queued"] and not first["queued"]
    assert first["folder_name"] == "H2025-0001_企业_历史"
    assert chunk(client, second, 0, b"abcd").status_code == 409
    assert client.post(f'/api/batches/{first["id"]}/cancel', headers=headers(first)).json()["status"] == "已撤回"
    assert not file_path(client, first).parent.parent.exists()
    state = client.get(f'/api/batches/{second["id"]}/upload-state', headers=headers(second)).json()
    assert not state["queued"]
    assert client.post(f'/api/batches/{second["id"]}/cancel', headers=headers(second)).status_code == 200


@pytest.mark.parametrize("termination", ["expire", "logout"])
def test_upload_token_survives_login_expiry_and_logout(upload_client: TestClient, termination: str) -> None:
    client = upload_client
    batch = create(client)
    if termination == "expire":
        with client.app.state.database.session() as session:
            session.execute(update(UserSession).values(expires_at=utcnow() - timedelta(hours=1)))
    else:
        assert client.post('/api/auth/logout').status_code == 200
    assert client.get('/api/auth/me').status_code == 401
    assert chunk(client, batch, 0, b"abcd").status_code == 200
    # Cookie 已清除，上传状态与暂停仍由独立令牌授权。
    assert client.get(f'/api/batches/{batch["id"]}/upload-state', headers=headers(batch)).status_code == 200
    assert client.post(f'/api/batches/{batch["id"]}/pause', headers=headers(batch)).json()["status"] == "上传中断"
    assert client.get('/api/batches').status_code == 401


def test_resume_same_manifest_changed_manifest_and_reset(upload_client: TestClient) -> None:
    client = upload_client
    batch = create(client)
    chunk(client, batch, 0, b"abcd")
    assert client.post(f'/api/batches/{batch["id"]}/pause', headers=headers(batch)).status_code == 200
    changed = body(size=11)
    response = client.post(f'/api/batches/{batch["id"]}/resume', json=changed)
    assert response.status_code == 200 and response.json()["differences"]["changed"] == ["单品/图片.JPG"]
    assert file_path(client, batch).read_bytes() == b"abcd"
    accepted = client.post(f'/api/batches/{batch["id"]}/resume', json={**changed, "accept_changes": True}).json()
    assert accepted["files"][0]["bytes_received"] == 0 and accepted["upload_token"]
    assert chunk(client, accepted, 0, b"ABCD").status_code == 200
    assert client.post(f'/api/batches/{batch["id"]}/resume', json={**body(), "folder_name": "X_别家_项目"}).status_code == 422
    assert chunk(client, batch, 0, b"abcd").status_code == 401


def test_file_tail_after_crash_is_rolled_back(upload_client: TestClient) -> None:
    client = upload_client
    batch = create(client)
    chunk(client, batch, 0, b"abcd")
    with file_path(client, batch).open('ab') as stream:
        stream.write(b"orphan")
    assert chunk(client, batch, 1, b"efgh").status_code == 200
    assert file_path(client, batch).read_bytes() == b"abcdefgh"


def test_restart_relogin_resume_falls_back_to_stream_hash(upload_client: TestClient, tmp_path: Path) -> None:
    client = upload_client
    batch = create(client)
    chunk(client, batch, 0, b"abcd")
    config = client.app.state.settings
    # 使用同一 SQLite 和 NAS 根的新进程替身，独立池与全新哈希缓存。
    with TestClient(create_app(config, log_dir=tmp_path / "logs2")) as restarted:
        restarted.app.state.upload.chunk_bytes = 4
        assert restarted.post('/api/auth/login', json={"username": "one", "password": "fake"}).status_code == 200
        assert restarted.get('/api/batches').json()[0]["status"] == "上传中断"
        resumed = restarted.post(f'/api/batches/{batch["id"]}/resume', json=body()).json()
        assert resumed["files"][0]["bytes_received"] == 4
        assert chunk(restarted, resumed, 1, b"efgh").status_code == 200
        assert chunk(restarted, resumed, 2, b"ij").status_code == 200
        assert finish(restarted, resumed).json()["files"][0]["fingerprint"] == hashlib.sha256(b"abcdefghij").hexdigest()


def test_other_user_tokens_ownership_role_and_wrong_file(upload_client: TestClient) -> None:
    client = upload_client
    batch = create(client)
    assert client.get('/api/batches/999').status_code == 404
    assert client.post('/api/batches/999/confirm').status_code == 404
    assert client.post(f'/api/batches/{batch["id"]}/files/999/complete', headers=headers(batch)).status_code == 404
    assert client.get(f'/api/batches/{batch["id"]}/upload-state', headers={"X-Upload-Token": "invalid"}).status_code == 401
    client.post('/api/auth/login', json={"username": "two", "password": "fake"})
    for url in (f'/api/batches/{batch["id"]}', f'/api/batches/{batch["id"]}/upload-state'):
        assert client.get(url).status_code == 403
    assert client.post(f'/api/batches/{batch["id"]}/cancel').status_code == 403
    assert client.post(f'/api/batches/{batch["id"]}/resume', json=body()).status_code == 403
    assert client.get('/api/batches').json() == []


def test_empty_files_directories_and_bounded_body(upload_client: TestClient) -> None:
    client = upload_client
    batch = create(client, body(name="空文件", size=0))
    assert finish(client, batch).json()["status"] == "已暂存"
    assert file_path(client, batch, "空文件").stat().st_size == 0
    assert client.post(f'/api/batches/{batch["id"]}/cancel', headers=headers(batch)).json()["status"] == "已撤回"
    next_batch = create(client)
    assert chunk(client, next_batch, 0, b"12345").status_code == 413
    assert chunk(client, next_batch, -1, b"1234").status_code == 422
    assert client.post(f'/api/batches/{next_batch["id"]}/confirm').status_code == 409
    assert client.get('/api/batches/precheck').status_code == 422
    client.cookies.clear()
    assert finish(client, batch).status_code == 401


def test_queue_fifo_and_restart_does_not_leave_unreachable_queue(upload_client: TestClient, tmp_path: Path) -> None:
    client = upload_client
    first, second, third = create(client), create(client), create(client)
    assert second["queued"] and third["queued"]
    client.post(f'/api/batches/{first["id"]}/pause', headers=headers(first))
    assert client.get(f'/api/batches/{third["id"]}/upload-state', headers=headers(third)).json()["queued"]
    assert not client.get(f'/api/batches/{second["id"]}/upload-state', headers=headers(second)).json()["queued"]
    with TestClient(create_app(client.app.state.settings, log_dir=tmp_path / "restart")) as restarted:
        restarted.post('/api/auth/login', json={"username": "one", "password": "fake"})
        rows = restarted.get('/api/batches').json()
        assert len(rows) == 3
        assert all(row["status"] == "上传中断" and not row["queued"] for row in rows)


def test_resume_added_missing_and_done_files_are_preserved(upload_client: TestClient) -> None:
    client = upload_client
    payload = body(size=4)
    payload["files"].append({"rel_path": "01定稿文件/待删除.txt", "size": 4, "mtime": 1})
    batch = create(client, payload)
    assert chunk(client, batch, 0, b"abcd").status_code == 200
    assert finish(client, batch).json()["files"][0]["upload_status"] == "done"
    client.post(f'/api/batches/{batch["id"]}/pause', headers=headers(batch))
    changed = body(size=4)
    changed["files"].append({"rel_path": "02设计方案/新增.txt", "size": 1, "mtime": 2})
    preview = client.post(f'/api/batches/{batch["id"]}/resume', json=changed).json()
    assert preview["differences"]["missing"] == ["01定稿文件/待删除.txt"]
    assert preview["differences"]["added"] == ["02设计方案/新增.txt"]
    assert len(client.get(f'/api/batches/{batch["id"]}').json()["files"]) == 2
    resumed = client.post(f'/api/batches/{batch["id"]}/resume', json={**changed, "accept_changes": True}).json()
    assert resumed["files"][0]["upload_status"] == "done" and resumed["files"][0]["bytes_received"] == 4
    assert file_path(client, batch).read_bytes() == b"abcd"
    added = resumed["files"][1]
    assert client.put(f'/api/batches/{batch["id"]}/files/{added["id"]}/chunks/0', content=b"X", headers=headers(resumed)).status_code == 200
    assert client.post(f'/api/batches/{batch["id"]}/files/{added["id"]}/complete', headers=headers(resumed)).json()["status"] == "已暂存"


def test_upload_permission_revocation_rejects_independent_token(upload_client: TestClient) -> None:
    client = upload_client
    batch = create(client)
    with client.app.state.database.session() as session:
        from app.db.models import User
        session.get(User, 1).roles = ["admin"]
    assert chunk(client, batch, 0, b"abcd").status_code == 403
    assert client.post('/api/batches/precheck', json=body()).status_code == 403
    assert client.post('/api/batches', json=body()).status_code == 403
