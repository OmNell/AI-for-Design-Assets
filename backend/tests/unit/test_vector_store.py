"""两种向量实现共用一套测试。"""
from collections.abc import Iterator
import math

import pytest

from app.db.database import Database
from app.db.models import Batch, CatalogFile, Product, ProductEmbedding, Project, User, utcnow
from app.services.search.vectors import NumpyVectorStore, SqliteVecStore, VectorStore, create_vector_store


@pytest.fixture(params=["sqlite-vec", "numpy"])
def store(request: pytest.FixtureRequest, database: Database) -> Iterator[VectorStore]:
    created = create_vector_store(database, request.param)
    assert created.name == request.param
    with database.session() as session:
        project = Project(project_code="P1", code_source="company", company_name="企业", won=True, project_name="项目", folder_name="P1_企业_项目", current_path="x", status="active")
        session.add(project)
        session.flush()
        for index in range(1, 7):
            file = CatalogFile(project_id=project.id, rel_path=f"单品/{index}.jpg", size=1, mtime=1, fingerprint="f", fingerprint_kind="full", category="单品", status="active")
            session.add(file)
            session.flush()
            session.add(Product(product_code=f"P1-{index:03d}", project_id=project.id, kind="item", display_name=str(index), file_id=file.id,
                                archive_rel_path="x", status="active", free_text="", vision_model="", ingested_at=utcnow()))
    yield created


def test_upsert_search_order_and_scores(store: VectorStore, database: Database) -> None:
    with database.session() as session:
        store.upsert(session, 1, [1, 0, 0], "h1", "m")
        store.upsert(session, 2, [0.9, 0.1, 0], "h2", "m")
        store.upsert(session, 3, [0, 1, 0], "h3", "m")
        store.upsert(session, 4, [-1, 0, 0], "h4", "m")
    with database.session() as session:
        found = store.search(session, [1, 0, 0], 3)
        assert [product_id for product_id, _ in found] == [1, 2, 3]
        assert found[0][1] == pytest.approx(1.0, abs=1e-5) and found[1][1] == pytest.approx(0.9 / math.sqrt(0.82), abs=1e-5) and found[2][1] == pytest.approx(0, abs=1e-5)
        assert [product_id for product_id, _ in store.search(session, [2, 0, 0], 10)] == [1, 2, 3, 4]
        assert store.search(session, [1, 0, 0], 0) == [] and store.search(session, [0, 0, 0], 5) == []
        assert store.count(session) == 4


def test_upsert_replaces_delete_and_restrict_to_ids(store: VectorStore, database: Database) -> None:
    with database.session() as session:
        for product_id, vector in ((1, [1, 0]), (2, [0, 1]), (3, [1, 1])):
            store.upsert(session, product_id, vector, "a", "m")
        store.upsert(session, 1, [0, 1], "b", "m")
    with database.session() as session:
        assert [product_id for product_id, _ in store.search(session, [0, 1], 2)] == [1, 2]
        assert session.query(ProductEmbedding).filter_by(product_id=1).one().text_hash == "b" and store.count(session) == 3
        assert [product_id for product_id, _ in store.search(session, [0, 1], 5, {2, 3})] == [2, 3]
        assert store.search(session, [0, 1], 5, set()) == [] and store.search(session, [0, 1], 5, {99}) == []
        store.delete(session, 2)
        store.delete(session, 99)
    with database.session() as session:
        assert [product_id for product_id, _ in store.search(session, [0, 1], 5)] == [1, 3]


def test_dimension_change_and_clear(store: VectorStore, database: Database) -> None:
    with database.session() as session:
        store.upsert(session, 1, [1, 0], "a", "old")
        store.upsert(session, 2, [0, 1], "a", "old")
    with database.session() as session:
        # 换了向量模型：维度不同的旧向量不参与检索，也不报错。
        store.upsert(session, 3, [1, 0, 0, 0], "a", "new")
        assert [product_id for product_id, _ in store.search(session, [1, 0, 0, 0], 5)] == [3]
    with database.session() as session:
        store.clear(session)
        assert store.count(session) == 0 and store.search(session, [1, 0], 5) == []
        store.upsert(session, 4, [1, 0], "a", "new")
        assert [product_id for product_id, _ in store.search(session, [1, 0], 5)] == [4]


def test_backend_selection_and_automatic_fallback(database: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    assert isinstance(create_vector_store(database, "numpy"), NumpyVectorStore) and not isinstance(create_vector_store(database, "numpy"), SqliteVecStore)
    assert isinstance(create_vector_store(database, "auto"), SqliteVecStore)
    monkeypatch.setattr(Database, "load_vector_extension", lambda self: False)
    fallback = create_vector_store(database, "auto")
    assert fallback.name == "numpy"


def test_index_survives_new_store_instance(database: Database) -> None:
    with database.session() as session:
        project = Project(project_code="P1", code_source="company", company_name="企业", won=True, project_name="项目", folder_name="f", current_path="x", status="active")
        session.add(project); session.flush()
        file = CatalogFile(project_id=project.id, rel_path="a", size=1, mtime=1, fingerprint="f", fingerprint_kind="full", category="单品", status="active")
        session.add(file); session.flush()
        session.add(Product(product_code="P1-001", project_id=project.id, kind="item", display_name="a", file_id=file.id, archive_rel_path="x", status="active", free_text="", vision_model="", ingested_at=utcnow()))
    first = create_vector_store(database, "sqlite-vec")
    with database.session() as session:
        first.upsert(session, 1, [0.6, 0.8], "h", "m")
    # 重启后新建的两种实现都能直接读到已有向量。
    for backend in ("sqlite-vec", "numpy"):
        with database.session() as session:
            assert create_vector_store(database, backend).search(session, [0.6, 0.8], 1)[0][0] == 1
