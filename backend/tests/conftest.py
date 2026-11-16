"""测试仅使用本地临时目录和 dev 配置。"""
from pathlib import Path
from typing import Iterator

import pytest

from app.config import Settings, PathsConfig
from app.db.database import Database


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(mode="dev", paths=PathsConfig(data_dir=tmp_path / "data"))


@pytest.fixture
def database(settings: Settings) -> Iterator[Database]:
    db = Database(settings.paths.data_dir)
    db.migrate()
    yield db
    db.close()
