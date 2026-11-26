"""入库后档案计划、历史身份及作废期限。"""
from datetime import datetime, timedelta, timezone
from hashlib import sha256

import pytest
from sqlalchemy import select

from app.db.models import Batch, CatalogFile, HistoryVersion, Link, PostopPlan, Product, ProductRelation, Project, ReviewSnapshot, Setting, User
from app.errors import AppError
from app.services import archive
from app.services.ai.field_catalog import FIXED_FIELDS
from app.services.postops import manifest


STAMP = datetime(2026, 6, 1, tzinfo=timezone.utc)
INITIAL_ROOT = "01已中标项目/01酒业/01企业/P-001_企业_原项目"
IMAGE_SHA = sha256(b"original").hexdigest()
NEW_SHA = sha256(b"updated").hexdigest()


@pytest.fixture
def initial(database):
    with database.session() as session:
        session.add(User(id=1, username="uploader", display_name="上传人", roles=["uploader"], enabled=True))
        project = Project(id=100, project_code="P-001", code_source="company", company_name="企业",
                          won=True, project_name="原项目", folder_name="P-001_企业_原项目", current_path=INITIAL_ROOT,
                          status="active", ingested_at=STAMP, archive_updated_at=STAMP)
        session.add(project)
        session.flush()
        for key, path, category in [(101, "单品/单品一.jpg", "单品"), (102, "套装/套装一.jpg", "套装"),
                                    (103, "资料/资料.txt", "other")]:
            session.add(CatalogFile(id=key, project_id=100, rel_path=path, size=8, mtime=1, fingerprint=IMAGE_SHA,
                                    fingerprint_kind="full", category=category, status="active"))
        session.flush()
        session.add_all(Product(id=key, project_id=100, product_code=f"P-001-{index:03d}", kind=kind, display_name=kind,
                                file_id=file_id, archive_rel_path=f"{archive.PRODUCT_DIR[kind]}/P-001-{index:03d}.json",
                                status="active", free_text="", vision_model="假模型", ingested_at=STAMP)
                        for key, index, kind, file_id in [(110, 1, "item", 101), (111, 2, "set", 102)])
        session.flush()
        session.add_all(Link(kind=kind, target_id=identifier, link_id=f"stable_link_{identifier}") for kind, identifier in (
            ("project", 100), ("file", 101), ("file", 102), ("file", 103), ("product", 110), ("product", 111)))
        session.add(ProductRelation(set_product_id=111, item_product_id=110, source="manual", confirmed_by=1,
                                    confirmed_at=STAMP, status="active", relation_version=1))
    return database


def change(session, kind, params, now, *, payload=None, execute=True):
    project = session.get(Project, 100)
    number = len(list(session.scalars(select(Batch)))) + 1
    batch = Batch(batch_no=f"test-number-{number}", project_id=100, project_code="P-001", kind=kind, status="入库中",
                  owner_id=1, uploader_id=1, temp_path=f"临时批次{number}/P-001_企业_原项目", won=True, round=1)
    session.add(batch)
    session.flush()
    before = archive.from_catalog(session, project)
    operation = {"params": params, "before": before, "before_digest": manifest.digest(before)}
    if kind == "purge":
        operation["purge"] = manifest.purge_preview(session, project, params, now)
    snapshot = ReviewSnapshot(batch_id=batch.id, round=batch.round, submitted_by=1, submitted_at=now,
                              digest="d" * 64, content={"operation": operation, "submitted_by": 1, **(payload or {})})
    if not execute:
        return manifest.prevalidate(session, batch, snapshot.content, now)
    canonical = manifest.logical_path(session, batch, snapshot)
    target = f"{canonical}/版本{number}"
    plan = manifest.plan(session, batch, snapshot, target, canonical_path=canonical, moment=now)
    manifest.publish(session, batch, plan)
    session.add(PostopPlan(batch_id=batch.id, previous_path=plan["old_root"],
                           target_path=target, baseline=plan["baseline"], manifest=plan["manifest"],
                           sources=plan["sources"], purge=plan["purge"], progress={"published_at": archive.utc_text(now)}))
    batch.status = "已入库"
    session.flush()
    return plan


def test_rename_keeps_links_and_file_identity(initial):
    with initial.session() as session:
        target = change(session, "rename", {"project_name": "新项目",
                        "products": [{"id": 110, "name": "名称 空格", "rename_file": True}]}, STAMP)
        project = session.get(Project, 100)
        item = session.get(Product, 110)
        image = session.get(CatalogFile, 101)
        assert item.archive_rel_path == "单品档案/P-001-001.json"
        assert item.file_id == 101 and image.rel_path == "单品/名称 空格.jpg"
        assert target["sources"][image.rel_path]["path"] == INITIAL_ROOT + "/单品/单品一.jpg"
        assert session.scalar(select(Link.link_id).where(Link.target_id == 110)) == "stable_link_110"
        assert archive.matches_catalog(session, project, target["manifest"])


def test_void_and_restore_preserve_reviewed_relations(initial):
    with initial.session() as session:
        change(session, "void", {"product_ids": [111], "reason": "套装暂时不用"}, STAMP)
        before = archive.from_catalog(session, session.get(Project, 100))
        assert session.get(Product, 110).status == "active"
        assert before["relations"][0]["inactive_reason"] == "set_void"
        change(session, "restore", {"product_ids": [111], "reason": "恢复套装"}, STAMP)
        assert archive.from_catalog(session, session.get(Project, 100))["relations"][0]["status"] == "active"
        change(session, "void", {"product_ids": [110], "reason": "单品暂时不用"}, STAMP)
        change(session, "retag", {"products": [{"id": 111, "item_ids": []}]}, STAMP)
        change(session, "restore", {"product_ids": [110], "reason": "恢复单品"}, STAMP)
        relation = archive.from_catalog(session, session.get(Project, 100))["relations"][0]
        assert relation["status"] == "inactive" and relation["inactive_reason"] == "reviewed_change"


def test_project_restore_leaves_earlier_void_unchanged(initial):
    with initial.session() as session:
        change(session, "void", {"product_ids": [110], "reason": "单品暂时不用"}, STAMP)
        operation = change(session, "void", {"project": True, "reason": "整项目暂时不用"}, STAMP)
        assert operation["canonical_path"] == "_已作废项目/P-001_企业_原项目"
        assert session.get(Project, 100).status == "voided"
        restored = change(session, "restore", {"project": True, "reason": "恢复整个项目"}, STAMP)
        assert restored["canonical_path"] == INITIAL_ROOT
        assert session.get(Product, 110).status == "voided"
        assert session.get(Product, 111).status == "active"
        assert session.get(CatalogFile, 103).status == "active"


def test_purge_bans_active_and_freezes_exact_cooldown_and_historical_copies(initial):
    params = {"product_ids": [110], "reason": "作废已满期", "confirm": "P-001-001"}
    void = {"product_ids": [110], "reason": "单品作废"}
    restore = {"product_ids": [110], "reason": "暂不删除"}
    with initial.session() as session:
        project = session.get(Project, 100)
        with pytest.raises(AppError):
            manifest.purge_preview(session, project, params, STAMP)
        change(session, "void", void, STAMP)
        assert not manifest.purge_preview(session, project, params, STAMP + timedelta(days=30, microseconds=-1))["eligible"]
        assert manifest.purge_preview(session, project, params, STAMP + timedelta(days=30))["eligible"]
        change(session, "restore", restore, STAMP + timedelta(days=30))
        change(session, "void", void, STAMP + timedelta(days=31))
        assert not manifest.purge_preview(session, project, params, STAMP + timedelta(days=60))["eligible"]
        purge = change(session, "purge", params, STAMP + timedelta(days=61))
        assert not any(row["file_id"] == 101 for row in purge["sources"].values())
        assert all(row["file_id"] == 101 for row in purge["purge"])
        assert len(purge["purge"]) == 4 and purge["purge"][0]["historical"] is False
        assert session.scalar(select(Link.link_id).where(Link.target_id == 101)) == "stable_link_101"
        assert session.get(CatalogFile, 101).status == "deleted"
        assert all(row.expected_missing for row in session.scalars(select(HistoryVersion).where(HistoryVersion.file_id == 101)))
        assert session.get(CatalogFile, 102).status == "active"


def test_replacement_retains_original_product_identity_and_old_image_link(initial):
    file = {"id": 301, "rel_path": "单品/单品一.jpg", "size": 7, "mtime": 1,
            "fingerprint": NEW_SHA, "fingerprint_kind": "full", "conflict_choice": "replace"}
    tags = {key: {"confirmation_status": "confirmed_unknown", "value": "", "terms": [], "source": "manual",
                  "ai_value": "无法判断", "flag": None} for key in FIXED_FIELDS}
    proof = {"ai_fingerprint": NEW_SHA, "ai_model": "假模型", "ai_success_at": STAMP.isoformat(), "existing_product_id": 110,
             "tags_confirmation": {key: {"status": "confirmed_unknown", "fingerprint": NEW_SHA, "value": ""} for key in FIXED_FIELDS}}
    payload = {"files": [file], "evidence": {"401": proof}, "review": {"products": [
        {"id": 401, "batch_file_id": 301, "kind": "item", "display_name": "新展示名", "free_text": "新描述", "tags": tags}]}}
    params = {"files": [{"id": 301, "choice": "replace"}], "relation_reviews": {"111": [110]}}
    with initial.session() as session:
        draft = change(session, "supplement", params, STAMP, payload=payload, execute=False)
        assert session.scalar(select(Setting).where(Setting.key == archive.IDENTITY_SEQUENCE)) is None
        draft_batch = session.scalar(select(Batch))
        draft_batch.status = "已终止"
        proof["ai_fingerprint"] = IMAGE_SHA
        with pytest.raises(AppError, match="证据"):
            manifest.prevalidate(session, draft_batch, {"submitted_by": 1, "operation": {
                "params": params, "before": archive.from_catalog(session, session.get(Project, 100))}, **payload}, STAMP)
        proof["ai_fingerprint"] = NEW_SHA
        assert draft["manifest"]["products"][0]["product_code"] == "P-001-001"
        publish = change(session, "supplement", params, STAMP, payload=payload)
        item = session.get(Product, 110)
        assert item.product_code == "P-001-001" and item.file_id != 101
        assert session.get(CatalogFile, 101).status == "replaced"
        assert session.scalar(select(Link.link_id).where(Link.target_id == 101)) == "stable_link_101"
        historic_path = next(path for path, source in publish["sources"].items() if source["file_id"] == 101)
        assert historic_path.startswith(archive.HISTORY_DIR + "/")
        change(session, "void", {"file_ids": [101], "reason": "旧文件作废"}, STAMP)
        assert session.get(CatalogFile, 101).status == "voided"
        change(session, "restore", {"file_ids": [101], "reason": "恢复历史文件"}, STAMP)
        assert session.get(CatalogFile, 101).status == "replaced"
        assert session.get(CatalogFile, 101).physical_rel_path == historic_path


def test_document_supplement_skips_duplicate_and_preserves_conflicting_original(initial):
    files = [
        {"id": 301, "rel_path": "资料/资料.txt", "size": 7, "mtime": 1, "fingerprint": NEW_SHA, "fingerprint_kind": "full"},
        {"id": 302, "rel_path": "资料/内容相同但路径不同.txt", "size": 8, "mtime": 1,
         "fingerprint": IMAGE_SHA, "fingerprint_kind": "full"},
        {"id": 303, "rel_path": "单品/单品一.jpg", "size": 8, "mtime": 1,
         "fingerprint": IMAGE_SHA, "fingerprint_kind": "full"},
    ]
    payload = {"files": files, "evidence": {}, "review": {"products": []}}
    with initial.session() as session:
        with pytest.raises(AppError, match="替换或另存"):
            change(session, "supplement", {"files": []}, STAMP, payload=payload, execute=False)
        session.scalar(select(Batch)).status = "已终止"
        plan = change(session, "supplement", {"files": [{"id": 301, "choice": "keep_both"}]}, STAMP, payload=payload)
        assert plan["sources"]["资料/资料.txt"]["file_id"] == 103
        assert plan["sources"]["资料/资料_2.txt"]["region"] == "temp"
        assert plan["sources"]["资料/内容相同但路径不同.txt"]["file_id"] != 103
        assert plan["sources"]["单品/单品一.jpg"]["region"] == "formal"
        assert len(plan["manifest"]["products"]) == 2
