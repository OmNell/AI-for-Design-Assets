import hashlib
from pathlib import Path
import pytest

from app.schemas.upload import ManifestEntry
from app.services.preflight import check, ignored
from app.services.nas import LocalFs, Region, Access
from app.services.upload.fingerprint import fingerprint
from app.config import DevUser, Settings
from app.db.database import Database
from app.db.models import Batch, Oplog
from app.schemas.upload import CreateBatchRequest, ResumeRequest
from app.services.auth.service import AuthService
from app.services.nas import NasSessionExpired
from app.services.nas.factory import create_auth_backend, create_session_pool
from app.services.upload.service import UploadService
from sqlalchemy import select


@pytest.mark.parametrize('folder,path,code', [
    ('not_three', '单品/a.png', 'P01'), ('P_企业_项目', 'a:b', 'P03'),
    ('P_企业_项目', 'a'*200, 'P04'), ('P_企业_项目', '资料/单品档案', 'P05'),
    ('P_企业_项目', '单品/a.gif', 'P06'), ('P_企业_项目', '资料/a', 'P07'),
    ('P_企业_项目', '单品/a.png', 'P08'), ('P_企业_项目 名', '单品/a.png', 'P09'),
])
def test_server_preflight_rechecks(folder: str, path: str, code: str) -> None:
    assert code in [issue.rule_code for issue in check(folder, [ManifestEntry(rel_path=path)])]


def test_ignored_and_unsafe_manifests() -> None:
    assert ignored(ManifestEntry(rel_path='单品/Thumbs.db'))
    assert not ignored(ManifestEntry(rel_path='Thumbs.db', is_dir=True))
    assert not any(i.rule_code == 'P06' for i in check('P_企业_项目', [ManifestEntry(rel_path='单品/desktop.ini')]))
    assert any(i.rule_code == 'manifest' for i in check('P_企业_项目', [ManifestEntry(rel_path='a'), ManifestEntry(rel_path='a/b')]))


def test_fingerprints_full_cached_quick_and_2gb_sparse(tmp_path: Path) -> None:
    fs = LocalFs(tmp_path, {Region.TEMP: Access.WRITE})
    path = '工作台临时区/data'
    data = b'a' * (5 * 1024 * 1024) + b'z' * (4 * 1024 * 1024)
    with fs.open_write(path) as stream:
        stream.write(data)
    assert fingerprint(fs, path, len(data), len(data)) == (hashlib.sha256(data).hexdigest(), 'full')
    digest = hashlib.sha256(data)
    assert fingerprint(fs, path, len(data), len(data), digest)[0] == digest.hexdigest()
    expected = hashlib.sha256(data[:4*1024*1024] + data[-4*1024*1024:] + str(len(data)).encode()).hexdigest()
    assert fingerprint(fs, path, len(data), 1) == (expected, 'quick')
    with fs.open_write(path) as stream:
        stream.truncate(2 * 1024**3)
    size = 2 * 1024**3
    expected = hashlib.sha256(bytes(8*1024*1024) + str(size).encode()).hexdigest()
    assert fingerprint(fs, path, size, 200*1024*1024) == (expected, 'quick')
    fs.close()


def test_idle_upload_releases_own_session_and_resumes_durably(database: Database, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    monkeypatch.setattr('app.services.upload.service.time.monotonic', lambda: clock[0])
    settings.paths.dev_nas_dir = tmp_path / 'nas'
    settings.dev_users = [DevUser(username='one', password='fake', permissions={'temp': 'write'})]
    with create_session_pool(settings) as pool:
        auth = AuthService(database, pool, create_auth_backend(settings), settings)
        upload = UploadService(auth)
        try:
            user, token, _ = auth.login('one', 'fake', '本机')
            body = CreateBatchRequest(folder_name='P_企业_项目', won=True, acks=['P07', 'P08'], files=[ManifestEntry(rel_path='资料.txt', size=1)])
            batch = upload.create(body, user, '本机')
            upload.chunk(batch.id, batch.files[0].id, 0, b'X', user, '本机')
            auth.logout(token, '本机')
            assert pool.get(user.id)
            clock[0] = 61
            upload.expire_idle()
            with pytest.raises(NasSessionExpired):
                pool.get(user.id)
            with database.session() as session:
                assert session.get(Batch, batch.id).status == '上传中断'
                assert '上传中断' in list(session.scalars(select(Oplog.action)))
            user, _, _ = auth.login('one', 'fake', '本机')
            resumed = upload.resume(batch.id, ResumeRequest(folder_name=body.folder_name, files=body.files, acks=body.acks), user, '本机')
            assert resumed.files[0].bytes_received == 1
            assert upload.complete(batch.id, batch.files[0].id, user, '本机').status == '已暂存'
        finally:
            upload.shutdown()
