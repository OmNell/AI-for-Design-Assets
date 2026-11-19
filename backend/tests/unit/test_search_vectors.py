"""使用同一组向量检查 sqlite-vec 与 NumPy 后备。"""
from dataclasses import replace

import pytest

from app.db.models import CatalogFile, Product, ProductEmbedding, Project, utcnow
from app.services.ingest.vectors import Model, text_hash
from app.services.search.scoring import Candidate, fallback, fuse, lexical
from app.services.search.store import VectorStore


@pytest.mark.parametrize("prefer_sqlite", [True, False])
def test_cosine_generation_dimension_and_stale_hash(database, prefer_sqlite):
    store = VectorStore(prefer_sqlite)
    model = Model("测试模型", "revision")
    digest = text_hash("正文")
    with database.session() as session:
        project = Project(project_code="P1", company_name="企业", project_name="项目", folder_name="P1_企业_项目", won=True)
        session.add(project)
        session.flush()
        ids = []
        for sequence in range(3):
            file = CatalogFile(project_id=project.id, rel_path=f"{sequence}.jpg", size=1, mtime=1,
                               fingerprint="a" * 64, category="单品")
            session.add(file)
            session.flush()
            product = Product(project_id=project.id, product_code=f"P1-{sequence:03d}", kind="item",
                file_id=file.id, display_name=str(sequence), archive_rel_path=f"{sequence}.json", ingested_at=utcnow())
            session.add(product)
            session.flush()
            ids.append(product.id)
        store.upsert(session, model, ids[0], [0, 1], digest, 2)
        store.upsert(session, model, ids[1], [1, 0], digest, 2)
        store.upsert(session, replace(model, generation=2), ids[2], [0, 1], digest, 2)
        session.flush()
        expected = {pid: digest for pid in ids}
        ranking, backend = store.top(session, model, 2, [0, 1], expected)
        assert backend in (("sqlite-vec", "numpy") if prefer_sqlite else ("numpy",))
        assert ranking == [(ids[0], 1.0), (ids[1], 0.0)]
        assert store.get(session, ids[2], model, digest, 2) is None
        assert store.get(session, ids[0], model, digest, 3) is None
        session.get(ProductEmbedding, model.key(ids[0])).embedding = b"corrupt"
        session.flush()
        results, _ = store.top(session, model, 2, [0, 1], {**expected, ids[1]: text_hash("已更新")})
        assert results == []
        with pytest.raises(ValueError, match="维度"):
            store.top(session, model, 2, [0, 1, 2], expected)


def test_keyword_exclusion_relaxation_and_fusion_are_deterministic():
    candidate = Candidate(12, "保温杯", "适合新年送礼", {"material": "陶瓷", "color": "中国红"}, {"material": ["陶瓷"]})
    assert lexical(candidate, fallback("陶瓷 不要 中国红")) is None
    score, fields, relaxed = lexical(candidate, fallback("保温杯"))
    assert (score, fields, relaxed) == (3, [], 0)
    assert fuse([9, 12, 5], [12, 22, 9]) == fuse([9, 12, 5], [12, 22, 9]) == [12, 9, 22, 5]


def test_upgrade_preserves_wp09_embedding_and_formal_pointer(tmp_path):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import MetaData, Table, select, text

    from app.db.database import BACKEND_DIR, Database
    from app.db.models import Setting
    from app.services.ingest.vectors import FORMAL_MODEL

    db = Database(tmp_path)
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    try:
        with db.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "0017_asset_access")
        model = Model("legacy", "original-version")
        with db.session() as session:
            # 使用旧版迁移的实际列写入样本，避免新 ORM 字段影响升级前数据。
            meta = MetaData()
            stamp = utcnow()
            projects, files, products = [Table(name, meta, autoload_with=session.connection())
                                         for name in ("projects", "files", "products")]
            project_id, file_id, product_id = 77, 78, 79
            timestamps = {"created_at": stamp, "updated_at": stamp}
            session.execute(projects.insert().values(id=project_id, project_code="P2", code_source="company",
                company_name="企业", project_name="项目", folder_name="P2_企业_项目", status="active",
                current_path="设计资产样本", won=True, **timestamps))
            session.execute(files.insert().values(id=file_id, project_id=project_id, rel_path="单品/杯.jpg", size=1,
                mtime=1, fingerprint="b" * 64, fingerprint_kind="full", category="单品", status="active", **timestamps))
            session.execute(products.insert().values(id=product_id, project_id=project_id, product_code="P2-001",
                kind="item", file_id=file_id, display_name="杯", archive_rel_path="档案.json", status="active",
                free_text="", vision_model="", ingested_at=stamp, **timestamps))
            VectorStore.upsert(session, model, product_id, [0.2, 0.8], text_hash("杯"), 2)
            setting = {"name": model.name, "version": model.version, "dimension": 2, "generation": 1}
            session.add(Setting(key=FORMAL_MODEL, value=setting, encrypted=False))
        db.migrate()
        with db.session() as session:
            assert session.get(ProductEmbedding, model.key(product_id)).text_hash == text_hash("杯")
            assert session.get(Project, project_id).asset_version == 0
            assert session.get(Product, product_id).file_id == file_id
            assert session.get(CatalogFile, file_id).fingerprint == "b" * 64
            assert session.scalar(select(Setting).where(Setting.key == FORMAL_MODEL)).value == setting
            assert list(session.execute(text("PRAGMA foreign_key_check"))) == []
    finally:
        db.close()
