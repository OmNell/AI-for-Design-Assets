from __future__ import annotations

from io import BytesIO
from pathlib import Path
import time
import json
import os
import subprocess
import sys
import threading
from typing import BinaryIO
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.config import DevUser, Settings
from app.db.models import Batch, BatchFile, Job, User, Oplog
from app.main import create_app


@pytest.fixture
def client(settings: Settings, tmp_path: Path) -> Iterator[TestClient]:
    settings.paths.dev_nas_dir = tmp_path / "nas"
    settings.bootstrap_admin = "admin"
    settings.dev_users = [DevUser(username=name, password="fake", permissions={"temp": "write", "formal": "read"})
                          for name in ("one", "admin", "reviewer", "other")]
    with TestClient(create_app(settings, log_dir=tmp_path / "logs")) as value:
        login(value)
        yield value


def login(client: TestClient, username: str = "one") -> None:
    assert client.post("/api/auth/login", json={"username": username, "password": "fake"}).status_code == 200


def jpeg() -> bytes:
    stream = BytesIO()
    with Image.new("RGB", (2000, 1000), "green") as image:
        exif = Image.Exif()
        exif[274] = 6
        exif[271] = "不可泄露的拍摄设备"
        image.save(stream, format="JPEG", exif=exif)
    return stream.getvalue()


def upload(client: TestClient, contents: dict[str, bytes]) -> dict:
    body = {"folder_name": "P_企业_项目", "won": True, "acks": ["P08"],
            "files": [{"rel_path": path, "size": len(data), "mtime": 1} for path, data in contents.items()]}
    response = client.post("/api/batches", json=body)
    assert response.status_code == 200, response.text
    batch = response.json()
    for file in batch["files"]:
        url = f'/api/batches/{batch["id"]}/files/{file["id"]}'
        headers = {"X-Upload-Token": batch["upload_token"], "Content-Type": "application/octet-stream"}
        assert client.put(url + '/chunks/0', content=contents[file["rel_path"]], headers=headers).status_code == 200
        assert client.post(url + '/complete', headers=headers).status_code == 200
    return batch


def url(batch: dict, file: dict | None = None, size: int = 300) -> str:
    return f'/api/batches/{batch["id"]}/thumbs/{(file or batch["files"][0])["id"]}/{size}'


def test_twenty_main_images_all_three_sizes_within_ten_seconds(client: TestClient) -> None:
    source = jpeg()
    batch = upload(client, {f"单品/主图{index}.jpg": source for index in range(20)})
    completed_at = time.monotonic()
    assert client.app.state.jobs.wait_idle(timeout=10)
    elapsed = time.monotonic() - completed_at
    assert elapsed < 10
    for file in batch["files"]:
        for size, dimensions in [(300, (150, 300)), (1600, (800, 1600)), (1024, (512, 1024))]:
            response = client.get(url(batch, file, size))
            assert response.status_code == 200, response.text
            assert response.headers["content-type"] == "image/jpeg"
            assert response.headers["cache-control"] == "private, no-store"
            with Image.open(BytesIO(response.content)) as image:
                assert image.size == dimensions and not image.getexif()
    with client.app.state.database.session() as session:
        assert session.get(Batch, batch["id"]).status == "已暂存"
        jobs = list(session.scalars(select(Job)))
        assert len(jobs) == 20 and all(job.status == "done" and job.run_as_user_id == 1 for job in jobs)
        assert all(job.kind == 'thumb' for job in jobs)
    # 幂等重试 complete 不重复派发。
    assert client.post(f'/api/batches/{batch["id"]}/files/{batch["files"][0]["id"]}/complete').status_code == 200
    with client.app.state.database.session() as session:
        assert len(list(session.scalars(select(Job)))) == 20
    print(f"20 张主图上传完成后生成全部三档并可访问：{elapsed:.3f} 秒（dev/LocalFs）")


def test_corrupt_image_does_not_block_staging_and_admin_can_retry(client: TestClient) -> None:
    batch = upload(client, {"单品/好图.jpg": jpeg(), "套装/坏图.jpg": b'broken', "资料/图片.jpg": b'not-a-main-image'})
    assert client.app.state.jobs.wait_idle()
    good, bad, document = batch["files"]
    assert client.get(url(batch, good)).status_code == 200
    response = client.get(url(batch, bad))
    assert response.status_code == 404 and response.json()["detail"] == {"retryable": False, "reason": "图片损坏或无法解码"}
    assert client.get(url(batch, document)).status_code == 404
    current = client.get(f'/api/batches/{batch["id"]}').json()
    assert current["status"] == "已暂存"
    assert current["files"][1]["thumb_status"] == "failed" and current["files"][1]["thumb_error"]
    assert current["files"][1]["upload_status"] == "done"
    assert client.get('/api/admin/jobs').status_code == 403
    assert client.post('/api/admin/jobs/1/retry').status_code == 403
    login(client, "admin")
    rows = client.get('/api/admin/jobs').json()
    failed = next(row for row in rows if row["status"] == "failed")
    assert len(rows) == 2 and "payload" not in failed
    assert client.post(f'/api/admin/jobs/{failed["id"]}/retry').status_code == 200
    assert client.app.state.jobs.wait_idle()
    assert client.post('/api/admin/jobs/999/retry').status_code == 404
    good_job = next(row for row in rows if row["status"] == "done")
    assert client.post(f'/api/admin/jobs/{good_job["id"]}/retry').status_code == 409
    with client.app.state.database.session() as session:
        assert "管理员重试任务" in list(session.scalars(select(Oplog.action)))


def test_pending_marker_authorization_and_cached_read_without_nas(client: TestClient) -> None:
    batch = upload(client, {"单品/主图.jpg": jpeg()})
    assert client.app.state.jobs.wait_idle()
    with client.app.state.database.session() as session:
        file = session.get(BatchFile, batch["files"][0]["id"])
        file.thumb_status = "pending"
    response = client.get(url(batch))
    assert response.status_code == 404 and response.json()["detail"]["retryable"] is True
    with client.app.state.database.session() as session:
        session.get(BatchFile, batch["files"][0]["id"]).thumb_status = "done"
    client.app.state.auth.pool.close(1, force=True)
    assert client.get(url(batch)).status_code == 200
    assert client.get(url(batch, size=999)).status_code == 422
    login(client, "reviewer")
    assert client.get(url(batch)).status_code == 403
    with client.app.state.database.session() as session:
        reviewer = session.scalar(select(User).where(User.username == 'reviewer'))
        reviewer.roles = ['first_reviewer']
        session.get(Batch, batch["id"]).first_reviewer_id = reviewer.id
    assert client.get(url(batch)).status_code == 200
    login(client, "other")
    assert client.get(url(batch)).status_code == 403
    login(client, "admin")
    assert client.get(url(batch)).status_code == 200
    assert client.get(url({**batch, "id": 999})).status_code == 404
    client.cookies.clear()
    assert client.get(url(batch)).status_code == 401


def test_running_thumb_recovers_after_restart_and_owner_relogin(client: TestClient, tmp_path: Path) -> None:
    batch = upload(client, {"单品/主图.jpg": jpeg()})
    assert client.app.state.jobs.wait_idle()
    client.app.state.jobs.shutdown()
    with client.app.state.database.session() as session:
        job = session.scalar(select(Job))
        job.status = 'running'
        session.get(BatchFile, batch['files'][0]['id']).thumb_status = 'running'
    client.app.state.thumbs.invalidate(batch['id'])
    with TestClient(create_app(client.app.state.settings, log_dir=tmp_path / 'restart')) as restarted:
        # 新进程没有可恢复的 NAS 密码，必须记录会话失效，不能用系统身份替代。
        assert restarted.app.state.jobs.wait_idle()
        with restarted.app.state.database.session() as session:
            job = session.scalar(select(Job))
            assert job.status == 'failed' and job.error == '操作人会话已失效'
        login(restarted)
        assert restarted.app.state.jobs.wait_idle()
        assert restarted.get(url(batch)).status_code == 200
        with restarted.app.state.database.session() as session:
            assert session.scalar(select(Job)).status == 'done'


def test_cancel_hides_and_removes_cached_thumbnails(client: TestClient) -> None:
    batch = upload(client, {"单品/主图.jpg": jpeg()})
    assert client.app.state.jobs.wait_idle()
    assert client.get(url(batch)).status_code == 200
    assert client.post(f'/api/batches/{batch["id"]}/cancel', json={'reason': '本次无需继续'}).status_code == 200
    assert client.get(url(batch)).status_code == 404
    assert not (client.app.state.settings.paths.data_dir / 'thumbs' / 'batch' / str(batch['id'])).exists()


def test_cancel_during_render_cannot_republish_cache(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.thumbs as thumbs
    original = thumbs.render_variants
    rendered, release = threading.Event(), threading.Event()
    def paused(source: BinaryIO, destination: Path) -> None:
        original(source, destination)
        rendered.set()
        assert release.wait(3)
    monkeypatch.setattr(thumbs, 'render_variants', paused)
    batch = upload(client, {"单品/主图.jpg": jpeg()})
    try:
        assert rendered.wait(2)
        assert client.post(f'/api/batches/{batch["id"]}/cancel', json={'reason': '本次无需继续'}).status_code == 200
    finally:
        release.set()
    assert client.app.state.jobs.wait_idle()
    assert client.get(url(batch)).status_code == 404
    assert not (client.app.state.settings.paths.data_dir / 'thumbs/batch' / str(batch['id'])).exists()


def test_actual_process_kill_and_restart_recovers_after_relogin(settings: Settings, tmp_path: Path) -> None:
    source, marker, config = tmp_path / 'source.jpg', tmp_path / 'running', tmp_path / 'config.yaml'
    source.write_bytes(jpeg())
    config.write_text('mode: dev\npaths:\n  data_dir: ' + json.dumps(str(settings.paths.data_dir)) +
        '\n  dev_nas_dir: ' + json.dumps(str(tmp_path / 'nas')) +
        '\ndev_users:\n  - username: one\n    password: fake\n    permissions: {temp: write}\n', encoding='utf-8')
    child = r'''
import os, time
from pathlib import Path
from fastapi.testclient import TestClient
import app.services.thumbs as thumbs
from app.main import create_app
def blocked(source, destination):
    Path(os.environ['WP05_MARKER']).write_text('running')
    time.sleep(60)
thumbs.render_variants = blocked
with TestClient(create_app()) as client:
    assert client.post('/api/auth/login', json={'username':'one','password':'fake'}).status_code == 200
    data=Path(os.environ['WP05_SOURCE']).read_bytes()
    result=client.post('/api/batches', json={'folder_name':'P_企业_项目','won':True,'acks':['P08'],
        'files':[{'rel_path':'单品/主图.jpg','size':len(data),'mtime':1}]}).json()
    base=f"/api/batches/{result['id']}/files/{result['files'][0]['id']}"
    headers={'X-Upload-Token':result['upload_token']}
    assert client.put(base+'/chunks/0',content=data,headers=headers).status_code == 200
    assert client.post(base+'/complete',headers=headers).status_code == 200
    time.sleep(60)
'''
    environment = {**os.environ, 'NASWB_CONFIG': str(config), 'WP05_SOURCE': str(source), 'WP05_MARKER': str(marker)}
    process = subprocess.Popen([sys.executable, '-c', child], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 8
        while not marker.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(.02)
        assert marker.exists(), '子进程任务没有进入执行状态'
        process.kill()
        process.wait(timeout=5)
        from app.config import load_settings
        with TestClient(create_app(load_settings(config), log_dir=tmp_path / 'restart-logs')) as restarted:
            assert restarted.app.state.jobs.wait_idle()
            login(restarted)
            assert restarted.app.state.jobs.wait_idle()
            batch = restarted.get('/api/batches').json()[0]
            assert batch['status'] == '已暂存'
            for size in (300, 1600, 1024):
                assert restarted.get(url(batch, size=size)).status_code == 200
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stderr:
            process.stderr.close()
