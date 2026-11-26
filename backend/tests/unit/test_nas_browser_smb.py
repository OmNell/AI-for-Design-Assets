"""验证同一 SMB 连接上的路径边界、元数据和文件句柄。"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from smbprotocol.file_info import FileAttributes
from smbprotocol.open import CreateDisposition, CreateOptions

from app.services.nas.browser import ReadRoot
from app.services.nas.errors import NasNotFound, NasPermissionDenied, NasUnavailable
from app.services.nas.fs import BaseFs
from app.services.nas.paths import NasPaths
from app.services.nas.smb import DirectConnection, SmbFs

NOW = datetime.now(timezone.utc)
FOLDER = FileAttributes.FILE_ATTRIBUTE_DIRECTORY
LINK = FileAttributes.FILE_ATTRIBUTE_REPARSE_POINT


class Field:
    def __init__(self, value: object) -> None:
        self.value = value
    def get_value(self) -> object:
        return self.value


def metadata(name: str, flags: int) -> dict[str, Field]:
    return {"file_name": Field(name.encode("utf-16-le")), "file_attributes": Field(flags),
            "end_of_file": Field(12), "last_write_time": Field(NOW)}


@pytest.fixture
def smb(monkeypatch):
    connection = DirectConnection("nas.local", 30)
    connection.transport = SimpleNamespace(connected=True)
    connection.max_read_size = 65536
    session = SimpleNamespace(connection=connection)
    tree = SimpleNamespace(is_dfs_share=False, session=session)
    fs = SmbFs.__new__(SmbFs)
    BaseFs.__init__(fs, NasPaths.under("工作目录"))
    fs.browser_tree, fs._session, fs._disconnected = tree, session, False
    opened = []
    closed = []
    queries = []

    class SmbOpen:
        def __init__(self, current, path):
            assert current is tree
            self.tree_connect = current
            self.path = path
            self.last_write_time = NOW
            self.end_of_file = 12
            self.file_attributes = {"工作目录": FOLDER, "工作目录\\链接": FOLDER | LINK}.get(path, 0)

        def create(self, impersonation, rights, attributes, sharing, disposition, options):
            assert disposition == CreateDisposition.FILE_OPEN
            assert options & CreateOptions.FILE_OPEN_REPARSE_POINT
            opened.append(self.path)

        def query_directory(self, pattern, *args, **kwargs):
            queries.append(pattern)
            if pattern == "*":
                return [] if queries.count("*") > 1 else [
                    metadata("工作台临时区", FOLDER), metadata("_工作台备份", FOLDER), metadata("链接", FOLDER | LINK),
                    metadata("正常 文档.pdf", 0),
                ]
            target = {"WORKTA~1": "工作台临时区", "__BACK~1": "_工作台备份"}.get(pattern, pattern)
            return [metadata(target, 0)]

        def read(self, offset, length):
            return b"abcdefghijkl"[offset:offset + length]

        def close(self):
            closed.append(self.path)

    monkeypatch.setattr("app.services.nas.browser.Open", SmbOpen)
    return ReadRoot(fs, "工作目录"), opened, closed, queries


def test_smb_metadata_batches_without_opening_children(smb):
    root, opened, closed, queries = smb
    result = root.list("", False, lambda: False)
    assert [(row.name, row.stat.size) for row in result] == [("正常 文档.pdf", 12)]
    assert opened == closed == ["工作目录"]
    assert queries == ["*", "*"]


@pytest.mark.parametrize("path", ["WORKTA~1/秘密.txt", "__BACK~1/归档.zip"])
def test_smb_short_name_cannot_open_protected_directory(smb, path):
    root, opened, closed, queries = smb
    with pytest.raises(NasNotFound):
        root.info(path)
    assert opened == closed == ["工作目录"]
    assert queries == [path.split("/")[0]]


def test_smb_reparse_and_dfs_are_rejected(smb):
    root, opened, closed, queries = smb
    with pytest.raises(NasPermissionDenied):
        root.info("链接")
    assert opened == ["工作目录", "工作目录\\链接"] and closed == opened[::-1]
    root.fs.browser_tree.is_dfs_share = True
    with pytest.raises(NasPermissionDenied):
        root.info("正常 文档.pdf")


def test_smb_offset_and_release_on_existing_session_close(smb):
    root, opened, closed, queries = smb
    with root.open("正常 文档.pdf") as (info, stream):
        assert info.size == 12
        stream.seek(9)
        assert stream.read(3) == b"jkl"
        assert not closed
        stream.close()
        assert closed == opened[::-1]


def test_smb_directory_cancel_closes_handle(smb):
    root, opened, closed, queries = smb
    with pytest.raises(NasUnavailable, match="取消"):
        root.list("", False, lambda: True)
    assert opened == closed == ["工作目录"]
