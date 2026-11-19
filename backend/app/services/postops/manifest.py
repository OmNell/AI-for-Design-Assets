"""从冻结的操作快照生成完整资产版本，并在发布事务中保留稳定身份。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import PurePosixPath
import re
import secrets
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Batch, CatalogFile, CompanyIndustryMap, HistoryVersion, Industry, Link, Product,
    PostopPlan, ProductRelation, ProductTag, Project, ReviewRound, ReviewSnapshot, Setting, User, utcnow,
)
from app.errors import AppError
from app.services import archive
from app.services.admin.company_map import company_path
from app.services.ai.field_catalog import FIXED_FIELDS
from app.services.ai.parsing import split_terms
from app.services.nas import NasPermissionDenied, safe_join
from app.services.preflight import REQUIRED


VOIDED_ROOT = "_已作废项目"
COOLING_PERIOD = timedelta(days=30)
AUTOMATIC_DEACTIVATION = {"item_void", "set_void", "project_void"}
RECOGNIZED_CONFIRMATIONS = {"confirmed_value", "confirmed_unknown"}
IDENTITY_SETTING = "catalog_identity_sequence"
Row = dict[str, Any]


def _error(code: str, message: str, detail: object = None) -> AppError:
    return AppError(code, message, 409, detail)


def digest(data: archive.Manifest) -> str:
    encoded = json.dumps(archive._canonical(data), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _utc(moment: datetime | str | None) -> datetime | None:
    if isinstance(moment, str):
        moment = datetime.fromisoformat(moment)
    if moment is not None:
        return (moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    return None


def _path(path: str) -> str:
    try:
        if safe_join(path) == path:
            return path
    except NasPermissionDenied:
        pass
    raise _error("operation_invalid_path", "正式资产路径不合法", {"path": path})


def _segment(name: str, *, project: bool = False) -> str:
    if not name or "/" in name or "\\" in name or project and "_" in name:
        raise _error("operation_invalid_name", "名称含有不适用的目录分隔符")
    return _path(name)


def _by_id(rows: list[Row]) -> dict[int, Row]:
    return {row["id"]: row for row in rows}


def physical_paths(manifest: archive.Manifest) -> dict[int, str]:
    history = {row["file_id"]: row["new_rel_path"] for row in sorted(manifest["history"], key=lambda row: row["id"])}
    return {row["id"]: _path(row.get("physical_rel_path") or history.get(row["id"]) or row["rel_path"])
            for row in manifest["files"]}


def _sha(file: Row) -> None:
    if file["fingerprint_kind"] != "full" or re.fullmatch(r"[0-9a-f]{64}", file["fingerprint"] or "") is None:
        raise _error("operation_fingerprint_required", "文件缺少完整 SHA-256，无法核对其身份", {"file_id": file["id"]})


def _target_scope(data: archive.Manifest, params: Row, status: str, *, historic_void: bool = False) -> tuple[list[Row], list[Row]]:
    products = _by_id(data["products"])
    files = _by_id(data["files"])
    product_ids = set(params.get("product_ids", []))
    file_ids = set(params.get("file_ids", []))
    whole_project = params.get("project", False)
    if whole_project and (product_ids or file_ids) or not (whole_project or product_ids or file_ids):
        raise _error("operation_scope_invalid", "请选择整个项目或明确的商品、文件")
    if product_ids - products.keys() or file_ids - files.keys():
        raise _error("operation_scope_invalid", "操作包含不属于该项目的商品或文件")
    if whole_project:
        return ([row for row in products.values() if row["status"] != "deleted"],
                [row for row in files.values() if row["status"] != "deleted"])
    file_ids.update(products[key]["file_id"] for key in product_ids)
    if any(row["file_id"] in file_ids and key not in product_ids and row["status"] != "deleted"
           for key, row in products.items()):
        raise _error("operation_scope_invalid", "主图必须随所属商品一起操作")
    selected_products = [products[key] for key in sorted(product_ids)]
    selected_files = [files[key] for key in sorted(file_ids)]
    file_states = {status, "replaced"} if historic_void else {status}
    if any(row["status"] != status for row in selected_products) or any(row["status"] not in file_states for row in selected_files):
        raise _error("operation_scope_state", "所选商品或文件的当前状态不允许执行该操作")
    return selected_products, selected_files


def _purge_preview(session: Session, data: archive.Manifest, params: Row, now: datetime) -> Row:
    project = data["project"]
    whole_project = params.get("project", False)
    if not params.get("reason", "").strip():
        raise _error("operation_reason_required", "请填写物理删除原因")
    if whole_project and project.get("status") != "voided":
        raise _error("purge_requires_void", "项目必须先作废才能物理删除")
    if not whole_project and project.get("status", "active") != "active":
        raise _error("purge_requires_void", "作废项目请提交整项目删除申请")
    products, files = _target_scope(data, params, "voided")
    if any(row["status"] != "voided" for row in [*products, *files]):
        raise _error("purge_requires_void", "清单包含尚未作废的正式资产")
    dates = []
    for row in [*([project] if whole_project else []), *products, *files]:
        if not row.get("voided_at"):
            raise _error("purge_void_time_missing", "部分资产没有可信的作废时间，无法物理删除")
        started = _utc(row["voided_at"])
        execution = session.get(PostopPlan, row["void_batch_id"]) if row.get("void_batch_id") else None
        published = _utc(execution.progress.get("published_at")) if execution else None
        # 草稿及预备窗口不计入实际生效后的 30 天。
        if published is None:
            raise _error("purge_void_time_missing", "无法确认作废版本已正式发布")
        dates.append(max(started, published))
    if not dates:
        raise _error("purge_void_time_missing", "没有可核对的作废资产")
    earliest = max(dates) + COOLING_PERIOD
    paths = physical_paths(data)
    file_ids = {row["id"] for row in files}
    project_path = _path(project["current_path"])
    whitelist = []
    for row in sorted(files, key=lambda file: file["id"]):
        _sha(row)
        whitelist.append({"file_id": row["id"], "rel_path": paths[row["id"]], "path": safe_join(project_path, paths[row["id"]]),
                          "fingerprint": row["fingerprint"], "size": row["size"], "link_status": "deleted", "historical": False})
    product_ids = {row["id"] for row in products}
    label = products[0]["product_code"] if not whole_project and len(products) == 1 and len(files) == 1 else project["project_code"]
    return {
        "project": whole_project, "project_code": project["project_code"],
        "confirm_label": label, "expected_confirmation": label,
        "files": whitelist, "file_count": len(whitelist), "total_size": sum(row["size"] for row in whitelist),
        "products": [{"id": row["id"], "product_code": row["product_code"], "link_status": "deleted"} for row in products],
        "history": [row for row in data["history"] if row["file_id"] in file_ids],
        "relations": [row for row in data["relations"]
                      if row["set_product_id"] in product_ids or row["item_product_id"] in product_ids],
        "earliest_allowed": archive.utc_text(earliest), "in_cooling": now < earliest, "eligible": now >= earliest,
        "link_status": "deleted",
    }


def purge_preview(session: Session, project: Project, params: Row, now: datetime | None = None) -> Row:
    before = _physical_baseline(session, project.id, archive.from_catalog(session, project))
    before["project"]["status"] = project.status
    return _historic_purge(session, project.id, _purge_preview(session, before, params, _utc(now) or utcnow()))


def _frozen(session: Session, batch: Batch, snapshot: ReviewSnapshot) -> tuple[archive.Manifest, Row]:
    project = session.get(Project, batch.project_id)
    if project is None:
        raise _error("operation_project_missing", "对应的正式项目不存在")
    operation = snapshot.content["operation"]
    before = archive.from_catalog(session, project)
    frozen = operation["before"]
    if digest(before) != digest(frozen) or operation.get("before_digest", digest(frozen)) != digest(frozen):
        raise _error("operation_base_changed", "正式资产与提交时的版本不一致，请停止发布并重新核对")
    return before, operation["params"]


def _location(session: Session, batch: Batch, baseline: Row, params: Row, *, reviewed: bool) -> tuple[str, Row]:
    old_canonical = baseline.get("canonical_path") or baseline["current_path"]
    canonical = _path(old_canonical)
    parent, _, _ = canonical.rpartition("/")
    project_name = baseline["project_name"]
    fields: Row = {}
    if batch.kind == "rename" and params.get("project_name"):
        project_name = _segment(params["project_name"], project=True)
        fields["project_name"] = project_name
    whole_project = params.get("project", False)
    if batch.kind == "void" and whole_project:
        fields["restore_path"] = canonical
        return safe_join(VOIDED_ROOT, baseline["folder_name"]), fields
    final = session.scalar(select(ReviewRound).where(
        ReviewRound.batch_id == batch.id, ReviewRound.round == batch.round, ReviewRound.stage == "final",
        ReviewRound.decision == "pass")) if reviewed else None
    selection = (final.path_change or {}).get("after") if final else None
    explicit_path = (selection if selection and (final.path_change.get("before") is not None or batch.kind == "move") else None)
    if explicit_path and batch.kind not in {"move", "rename", "restore"}:
        raise _error("operation_path_unapproved", "该操作不能更改项目分类或中标状态")
    if batch.kind == "restore" and whole_project:
        restored_path = baseline.get("restore_path")
        if restored_path:
            canonical = _path(restored_path)
            parent, _, _ = canonical.rpartition("/")
        elif reviewed and not explicit_path:
            raise _error("operation_restore_path_missing", "原项目目录未登记，请终审核定恢复位置")
    company_name = baseline["company_name"]
    if batch.kind == "move" or explicit_path and (batch.kind == "rename" or whole_project):
        company_id = (explicit_path or params)["company_id"]
        won = (explicit_path or params)["won"]
        row = session.get(CompanyIndustryMap, company_id)
        industry = session.scalar(select(Industry).where(Industry.seq == row.industry_seq, Industry.name == row.industry_name)) if row else None
        if not row or not industry:
            raise _error("operation_destination_missing", "企业行业对照表已变化，请重新审核")
        parent = company_path(won, row)
        if explicit_path and explicit_path["path"] != parent:
            raise _error("operation_destination_changed", "核准后目录树已发生变化，无法按已审核位置执行")
        company_name = _segment(row.company_name, project=True)
        fields.update(company_name=company_name, company_id=row.id, won=won, industry_id=industry.id)
    new_folder = _path(f"{baseline['project_code']}_{company_name}_{project_name}")
    if "/" in new_folder:
        raise _error("operation_invalid_name", "项目文件夹名称不合法")
    if batch.kind in {"rename", "move"} or batch.kind == "restore" and whole_project:
        fields["folder_name"] = new_folder
        canonical = safe_join(parent, new_folder) if parent else new_folder
    return canonical, fields


def logical_path(session: Session, batch: Batch, snapshot: ReviewSnapshot) -> str:
    before, params = _frozen(session, batch, snapshot)
    return _location(session, batch, before["project"], params, reviewed=True)[0]


class _ManifestChange:
    def __init__(self, session: Session, batch: Batch, snapshot: ReviewSnapshot, before: archive.Manifest,
                 data: archive.Manifest, moment: datetime) -> None:
        self.session, self.batch, self.snapshot = session, batch, snapshot
        self.before, self.data = before, data
        self.project = data["project"]
        self.files, self.products = _by_id(data["files"]), _by_id(data["products"])
        self.tags = {(tag["product_id"], tag["field_key"]): tag for tag in data["tags"]}
        self.relations = {(pair["set_product_id"], pair["item_product_id"]): pair for pair in data["relations"]}
        self.previous_paths = physical_paths(before)
        self.temp_sources: dict[int, str] = {}
        self.batch_product_codes: dict[int, str] = {}
        self.now, self.stamp = moment, archive.utc_text(moment)
        self.purge: list[Row] = []
        self.reserved = session.scalar(select(Setting).where(Setting.key == IDENTITY_SETTING))
        self.last_id = max(self.reserved.value if self.reserved else 0,
                           *(session.scalar(select(func.max(model.id))) or 0 for model in (Project, CatalogFile, Product, HistoryVersion)))
        self.first_id = self.last_id

    def identity(self) -> int:
        self.last_id += 1
        if self.last_id >= 2**53:
            raise _error("operation_identity_exhausted", "正式资产身份编号已用完")
        return self.last_id

    def reserve(self) -> None:
        if self.first_id == self.last_id:
            return
        if self.reserved is None:
            self.session.add(Setting(key=IDENTITY_SETTING, value=self.last_id, encrypted=False))
        else:
            self.reserved.value = self.last_id

    def link(self, kind: str, target: int) -> None:
        self.data["links"].append({"kind": kind, "target_id": target, "link_id": secrets.token_urlsafe(16)})

    def history(self, file: Row, reason: str, product: int | None = None, *, relocate: bool = True) -> None:
        identifier = self.identity()
        old_path = self.previous_paths[file["id"]]
        new_path = safe_join(archive.HISTORY_DIR, f"{self.stamp[:10]}-{identifier}", old_path.rsplit("/", 1)[-1])
        self.data["history"].append({
            "id": identifier, "project_id": self.project["id"], "file_id": file["id"], "product_id": product,
            "old_fingerprint": file["fingerprint"], "old_rel_path": old_path, "new_rel_path": new_path,
            "reason": reason, "expected_missing": False,
        })
        if relocate:
            file["physical_rel_path"] = new_path

    def retire(self, file: Row, product: int | None = None) -> None:
        file["status"] = "replaced"
        self.history(file, "replaced", product)

    def product(self, identifier: int, kind: str | None = None) -> Row:
        product = self.products.get(identifier)
        if not product or product["status"] != "active" or kind and product["kind"] != kind:
            raise _error("operation_product_invalid", "商品不存在、已作废或类型不符", {"product_id": identifier})
        return product

    def review_relations(self, set_id: int, item_ids: list[int]) -> None:
        self.product(set_id, "set")
        if len(set(item_ids)) != len(item_ids):
            raise _error("operation_relation_invalid", "套装关联重复")
        for item_id in item_ids:
            self.product(item_id, "item")
        included = set(item_ids)
        for key, relation in list(self.relations.items()):
            if key[0] != set_id:
                continue
            if key[1] in included:
                included.remove(key[1])
                relation.update(status="active", inactive_reason=None, source="manual",
                                confirmed_by=self.snapshot.submitted_by, confirmed_at=archive.utc_text(self.snapshot.submitted_at),
                                relation_version=relation["relation_version"] + 1)
            elif relation["status"] == "active" or relation["inactive_reason"] in AUTOMATIC_DEACTIVATION:
                relation.update(status="inactive", inactive_reason="reviewed_change", source="manual",
                                confirmed_by=self.snapshot.submitted_by, confirmed_at=archive.utc_text(self.snapshot.submitted_at),
                                relation_version=relation["relation_version"] + 1)
        for item_id in sorted(included):
            row = {"set_product_id": set_id, "item_product_id": item_id, "source": "manual",
                   "confirmed_by": self.snapshot.submitted_by, "confirmed_at": archive.utc_text(self.snapshot.submitted_at),
                   "status": "active", "inactive_reason": None, "relation_version": 1}
            self.relations[set_id, item_id] = row
            self.data["relations"].append(row)

    def rename(self, params: Row) -> None:
        for change in params.get("products", []):
            product = self.product(change["id"])
            product["display_name"] = change["name"]
            if change.get("rename_file"):
                file = self.files[product["file_id"]]
                previous = PurePosixPath(file["rel_path"])
                relative = _path(previous.with_name(_segment(change["name"]) + previous.suffix).as_posix())
                file["rel_path"] = file["physical_rel_path"] = relative

    def retag(self, params: Row) -> None:
        for change in params["products"]:
            product = self.product(change["id"])
            if change.get("name") is not None:
                product["display_name"] = change["name"]
            if change.get("free_text") is not None:
                product["free_text"] = change["free_text"]
            for key, value in change.get("tags", {}).items():
                if key not in FIXED_FIELDS:
                    raise _error("operation_tag_invalid", "只能修改固定 11 个标签字段")
                _confirmation(value)
                tag = self.tags.get((product["id"], key))
                if tag is None:
                    tag = {"product_id": product["id"], "field_key": key, "ai_value": "", "flag": None}
                    self.tags[product["id"], key] = tag
                    self.data["tags"].append(tag)
                tag.update(value=value["value"], terms=split_terms(value["value"]), source="manual", flag=None,
                           confirmation_status=value["confirmation_status"])
            if change.get("item_ids") is not None:
                self.review_relations(product["id"], change["item_ids"])

    def lifecycle(self, params: Row) -> None:
        kind = self.batch.kind
        whole_project = params.get("project", False)
        expected = "active" if kind == "void" else "voided"
        reason = params.get("reason", "").strip()
        if not reason:
            raise _error("operation_reason_required", "请填写本次操作的原因")
        if self.project.get("status", "active") != (expected if whole_project else "active"):
            raise _error("operation_project_state", "项目当前状态不允许该操作")
        selected_products, selected_files = _target_scope(self.data, params, expected, historic_void=kind == "void")
        if kind == "purge":
            preview = _historic_purge(self.session, self.project["id"], _purge_preview(self.session, self.before, params, self.now))
            if preview["in_cooling"]:
                raise _error("purge_cooling", "距离最近一次有效作废尚未满 30 天", preview)
            if params.get("confirm") != preview["confirm_label"]:
                raise _error("purge_confirmation_required", "请输入指定资产编号确认物理删除", preview)
            self.purge = preview["files"]
        if whole_project and kind == "restore":
            void_batch = self.project.get("void_batch_id")
            if not void_batch:
                raise _error("operation_void_origin_missing", "项目缺少原作废记录，无法确认恢复范围")
            selected_products = [row for row in selected_products if row.get("void_batch_id") == void_batch]
            selected_files = [row for row in selected_files if row.get("void_batch_id") == void_batch]
        if whole_project and kind == "void":
            selected_products = [row for row in selected_products if row["status"] == "active"]
            selected_files = [row for row in selected_files if row["status"] in {"active", "replaced"}]
        selected_ids = {row["id"] for row in selected_products}
        file_ids = {row["id"] for row in selected_files}
        owners = {row["file_id"]: row["id"] for row in selected_products}
        if kind == "void":
            for file in selected_files:
                file["void_prior_status"] = file["status"]
                if not whole_project:
                    self.history(file, "voided", owners.get(file["id"]))
            for row in [*selected_products, *selected_files, *([self.project] if whole_project else [])]:
                row.update(status="voided", voided_at=self.stamp, void_reason=reason, void_batch_id=self.batch.id)
        elif kind == "restore":
            for row in [*selected_products, *selected_files, *([self.project] if whole_project else [])]:
                row["status"] = row.get("void_prior_status") or "active"
            if not whole_project:
                void_sources = {row["file_id"]: row["old_rel_path"] for row in self.data["history"] if row["reason"] == "voided"}
                for file in selected_files:
                    file["physical_rel_path"] = void_sources.get(file["id"], file["rel_path"])
        else:
            for row in [*selected_products, *selected_files, *([self.project] if whole_project else [])]:
                row.update(status="deleted", deleted_at=self.stamp, delete_reason=reason)
            for history in self.data["history"]:
                if history["file_id"] in file_ids:
                    history["expected_missing"] = True

        for relation in self.data["relations"]:
            set_id, item_id = relation["set_product_id"], relation["item_product_id"]
            if not ({set_id, item_id} & selected_ids):
                continue
            if kind == "void" and relation["status"] == "active":
                relation.update(status="inactive", inactive_reason="project_void" if whole_project else
                                ("item_void" if item_id in selected_ids else "set_void"),
                                relation_version=relation["relation_version"] + 1)
            elif kind == "restore" and relation["inactive_reason"] in AUTOMATIC_DEACTIVATION:
                if all(self.products[key]["status"] == "active" for key in (set_id, item_id)):
                    relation.update(status="active", inactive_reason=None, relation_version=relation["relation_version"] + 1)
            elif kind == "purge" and relation["inactive_reason"] != "purged":
                relation.update(status="inactive", inactive_reason="purged", relation_version=relation["relation_version"] + 1)

    def supplement(self, params: Row) -> None:
        content = self.snapshot.content
        evidence = content["evidence"]
        incoming_products = {row["batch_file_id"]: row for row in content["review"]["products"]}
        active_files = {row["rel_path"].casefold(): row for row in self.files.values() if row["status"] == "active"}
        original_product = {row["file_id"]: row for row in self.products.values() if row["status"] == "active"}
        answers = {row["id"]: row["choice"] for row in params.get("files", [])}
        assignments = {row["id"]: row["existing_product_id"] for row in params.get("products", [])}
        suffixes = [row["product_code"].rsplit("-", 1)[-1] for row in self.products.values()]
        if any(not suffix.isascii() or not suffix.isdigit() for suffix in suffixes):
            raise _error("operation_product_code_conflict", "部分旧商品编号无法识别，停止分配新编号")
        number = max((int(suffix) for suffix in suffixes), default=0)
        mapped_products: dict[int, int] = {}
        replaced_ids: set[int] = set()
        replaced_item_ids: set[int] = set()
        new_item = False
        existing_sets = {row["id"] for row in self.products.values() if row["kind"] == "set" and row["status"] == "active"}
        rows = sorted(content["files"], key=lambda row: (row["rel_path"].startswith("套装/"), row["rel_path"]))
        if len({row["rel_path"].casefold() for row in rows}) != len(rows):
            raise _error("operation_file_exists", "本次上传包含同名文件")
        for row in rows:
            _sha(row)
            relative = _path(row["rel_path"])
            top = relative.partition("/")[0]
            if top.casefold() in {archive.PROJECT_FILE.casefold(), archive.HISTORY_DIR.casefold(),
                                 *(name.casefold() for name in archive.PRODUCT_DIR.values())}:
                raise _error("operation_reserved_path", "补充文件不能写入正式档案保留路径", {"path": relative})
            review = incoming_products.get(row["id"])
            proof = evidence[str(review["id"])] if review else None
            occupied = active_files.get(relative.casefold())
            target_id = proof.get("existing_product_id") if proof else None
            if review and review["id"] in assignments and assignments[review["id"]] != target_id:
                raise _error("operation_assignment_changed", "主图替换对象与冻结的核对记录不一致")
            if occupied and occupied["fingerprint"] == row["fingerprint"] and occupied["size"] == row["size"]:
                current = original_product.get(occupied["id"])
                if review and (not current or target_id not in (None, current["id"])):
                    raise _error("operation_assignment_invalid", "重复主图与指定的商品不符")
                if review:
                    mapped_products[review["id"]] = current["id"]
                continue

            choice = answers.get(row["id"], row.get("conflict_choice"))
            if occupied and choice not in {"replace", "keep_both"}:
                raise _error("operation_conflict_choice_required", "同名不同内容的文件必须明确选择替换或另存",
                             {"path": relative})
            if occupied and choice == "keep_both":
                if target_id:
                    raise _error("operation_assignment_invalid", "同名另存不能同时替换已有商品")
                relative = self.available_path(relative, set(active_files))
            target = self.product(target_id, review["kind"]) if target_id else None
            if occupied and choice == "replace":
                current = original_product.get(occupied["id"])
                if current and (not target or current["id"] != target["id"]):
                    raise _error("operation_assignment_invalid", "替换主图必须明确指定原图所属商品",
                                 {"file_id": occupied["id"]})
                if not current:
                    self.retire(occupied)
                    active_files.pop(occupied["rel_path"].casefold(), None)
            if target:
                if target_id in replaced_ids:
                    raise _error("operation_assignment_invalid", "同一个商品不能在一批中替换多次主图")
                old_image = self.files[target["file_id"]]
                self.retire(old_image, target["id"])
                active_files.pop(old_image["rel_path"].casefold(), None)
                replaced_ids.add(target_id)
                if target["kind"] == "item":
                    replaced_item_ids.add(target_id)

            identifier = self.identity()
            category = top if "/" in relative and top in (*REQUIRED, "套装", "单品") else "other"
            file = {"id": identifier, "project_id": self.project["id"], "rel_path": relative, "size": row["size"],
                    "mtime": row["mtime"], "fingerprint": row["fingerprint"], "fingerprint_kind": "full",
                    "category": category, "status": "active"}
            self.files[identifier] = active_files[relative.casefold()] = file
            self.data["files"].append(file)
            self.temp_sources[identifier] = _path(f"{self.batch.temp_path}/{row['rel_path']}")
            self.link("file", identifier)
            if not review:
                if category in {"单品", "套装"} and PurePosixPath(relative).suffix.lower() in {".jpg", ".jpeg", ".png"}:
                    raise _error("operation_vision_required", "新增或替换主图没有对应的视觉 AI 和核对记录",
                                 {"file_id": row["id"]})
                continue
            if not proof.get("ai_success_at") or not proof.get("ai_model") or proof.get("ai_fingerprint") != row["fingerprint"]:
                raise _error("operation_vision_required", "视觉 AI 成功证据与本次主图的完整 SHA-256 不匹配",
                             {"file_id": row["id"]})
            if review["tags"].keys() != FIXED_FIELDS.keys():
                raise _error("operation_tag_required", "新主图的 11 个标签必须分别确认")
            if target:
                old_code = target["product_code"]
            else:
                number += 1
                old_code = f"{self.project['project_code']}-{number:03d}"
                product_id = self.identity()
                target = {"id": product_id, "product_code": old_code, "project_id": self.project["id"],
                          "archive_rel_path": f"{archive.PRODUCT_DIR[review['kind']]}/{old_code}.json",
                          "kind": review["kind"], "status": "active", "ingested_at": self.stamp}
                self.products[product_id] = target
                self.data["products"].append(target)
                self.link("product", product_id)
                new_item |= review["kind"] == "item"
            target.update(display_name=review["display_name"], file_id=identifier, free_text=review["free_text"],
                          vision_model=proof["ai_model"])
            mapped_products[review["id"]] = target["id"]
            self.batch_product_codes[review["id"]] = target["product_code"]
            for key, value in review["tags"].items():
                _confirmation(value)
                confirmation = proof.get("tags_confirmation", {}).get(key, {})
                if (not isinstance(confirmation, dict) or value["confirmation_status"] != confirmation.get("status")
                        or confirmation.get("fingerprint") != row["fingerprint"] or confirmation.get("value") != value["value"]):
                    raise _error("operation_tag_required", "新主图的标签确认记录不完整")
                tag = self.tags.get((target["id"], key))
                if tag is None:
                    tag = {"product_id": target["id"], "field_key": key}
                    self.tags[target["id"], key] = tag
                    self.data["tags"].append(tag)
                tag.update({field: value[field] for field in archive.TAG_FIELDS if field not in ("product_id", "field_key")})

        relation_reviews = {int(key): value for key, value in params.get("relation_reviews", {}).items()}
        relation_batch_items = {int(key): value for key, value in params.get("relation_batch_items", {}).items()}
        batch_set_items = {int(key): value for key, value in params.get("batch_set_items", {}).items()}
        replaced_sets = {identifier for identifier in replaced_ids if self.products[identifier]["kind"] == "set"}
        required = (existing_sets if new_item else {row["set_product_id"] for row in self.relations.values()
                    if row["status"] == "active" and row["item_product_id"] in replaced_item_ids}) - replaced_sets
        if required - relation_reviews.keys() or relation_batch_items.keys() - relation_reviews.keys():
            raise _error("operation_relation_review_required", "受影响套装关系必须明确重新核对",
                         {"product_ids": sorted(required - relation_reviews.keys())})
        for set_id, members in relation_reviews.items():
            if set_id not in existing_sets - replaced_sets:
                raise _error("operation_relation_invalid", "关联核对对象不属于本次受理的已有套装")
            self.review_relations(set_id, [*members, *[_batch_member(mapped_products, item)
                                  for item in relation_batch_items.get(set_id, [])]])
        for row in rows:
            review = incoming_products.get(row["id"])
            if not review or review["kind"] != "set":
                continue
            set_id = mapped_products[review["id"]]
            if self.products[set_id]["file_id"] not in self.temp_sources:
                continue  # 完全重复的主图不覆盖正式标签或关系。
            if not review["relation_confirmed"] or set_id in replaced_sets and review["id"] not in batch_set_items:
                raise _error("operation_relation_review_required", "替换套装主图必须明确复核原有关系",
                             {"product_id": set_id})
            self.review_relations(set_id, [*batch_set_items.get(review["id"], []),
                                  *[_batch_member(mapped_products, item["batch_product_id"]) for item in review["relation_items"]]])

    @staticmethod
    def available_path(relative: str, occupied: set[str]) -> str:
        file = PurePosixPath(relative)
        counter = 2
        while (candidate := file.with_name(f"{file.stem}_{counter}{file.suffix}").as_posix()).casefold() in occupied:
            counter += 1
        return _path(candidate)

    def sources(self) -> dict[str, Row]:
        result: dict[str, Row] = {}
        occupied: set[str] = set()
        logical_paths: set[str] = set()
        paths = physical_paths(self.data)
        for identifier, relative in sorted(paths.items()):
            row = self.files[identifier]
            if row["status"] == "deleted":
                continue
            if row["status"] == "active":
                logical = row["rel_path"].casefold()
                if logical in logical_paths:
                    raise _error("operation_file_exists", "目标相对路径已有同名文件", {"path": row["rel_path"]})
                logical_paths.add(logical)
            if relative.casefold() in occupied:
                raise _error("operation_file_exists", "目标实际路径已有同名文件", {"path": relative})
            occupied.add(relative.casefold())
            _sha(row)
            source = self.temp_sources.get(identifier)
            result[relative] = {"region": "temp" if source else "formal",
                                "path": source or safe_join(self.before["project"]["current_path"], self.previous_paths[identifier]),
                                "file_id": identifier, "fingerprint": row["fingerprint"], "size": row["size"]}
        if any(parent.as_posix().casefold() in occupied for path in result for parent in PurePosixPath(path).parents):
            raise _error("operation_file_exists", "目标路径中同名文件和文件夹冲突")
        return result


def _batch_member(mapping: dict[int, int], batch_id: int) -> int:
    member = mapping.get(batch_id)
    if member is None:
        raise _error("operation_relation_invalid", "套装关联指向本次未上传的单品")
    return member


def _confirmation(value: Row) -> None:
    text = value["value"].strip()
    status = value["confirmation_status"]
    valid_value = bool(text) and text not in {"无法判断", "无法确定"}
    if status not in RECOGNIZED_CONFIRMATIONS or status == "confirmed_value" and not valid_value or status == "confirmed_unknown" and text:
        raise _error("operation_tag_required", "标签确认状态与确认值不一致")


def _historic_purge(session: Session, project_id: int, preview: Row) -> Row:
    """删除清单覆盖已发布副本，既不扫描目录，也不推定同名文件属于本项目。"""
    selected = {entry["file_id"]: entry for entry in preview["files"]}
    listed = {entry["path"].casefold(): entry for entry in preview["files"]}
    plans = session.scalars(select(PostopPlan).join(Batch, Batch.id == PostopPlan.batch_id).where(
        Batch.project_id == project_id, Batch.status == "已入库").order_by(PostopPlan.batch_id))
    for plan in plans:
        for root, data in ((plan.previous_path, plan.baseline), (plan.target_path, plan.manifest)):
            paths = physical_paths(data)
            for row in data["files"]:
                approved = selected.get(row["id"])
                if not approved or row["status"] == "deleted":
                    continue
                identifier = row["id"]
                _sha(row)
                if any(row[field] != approved[field] for field in ("fingerprint", "size")):
                    raise _error("purge_identity_conflict", "历史副本的文件身份与当前记录不一致", {"file_id": identifier})
                path = safe_join(root, paths[identifier])
                known = listed.get(path.casefold())
                if known and any(known[field] != value for field, value in (("path", path), ("file_id", identifier))):
                    raise _error("purge_path_conflict", "历史副本与审批清单的实际路径发生冲突", {"path": path})
                if not known:
                    listed[path.casefold()] = {**approved, "path": path, "rel_path": paths[identifier], "historical": True}
    files = sorted(listed.values(), key=lambda row: (row["historical"], row["file_id"], row["path"]))
    return {**preview, "files": files, "file_count": len(files), "total_size": sum(row["size"] for row in files)}


def _physical_baseline(session: Session, project_id: int, before: archive.Manifest) -> archive.Manifest:
    actual = {row.id: row.physical_rel_path for row in session.scalars(
        select(CatalogFile).where(CatalogFile.project_id == project_id, CatalogFile.physical_rel_path.is_not(None)))}
    if not actual:
        return before
    current = deepcopy(before)
    for row in current["files"]:
        if row["id"] in actual:
            row["physical_rel_path"] = actual[row["id"]]
    return current


def _prepared(session: Session, batch: Batch, snapshot: ReviewSnapshot, target_path: str | None,
              *, moment: datetime | None, reviewed: bool) -> Row:
    before, params = _frozen(session, batch, snapshot)
    base_hash = digest(before)
    baseline = _physical_baseline(session, batch.project_id, before)
    physical = snapshot.content["operation"].get("physical_paths")
    if physical is not None:
        frozen_files = {str(key): value for key, value in physical.items()}
        physical_pointers = {str(row["id"]): row.get("physical_rel_path") for row in baseline["files"]}
        resolved_paths = {str(key): value for key, value in physical_paths(baseline).items()}
        if frozen_files != physical_pointers and frozen_files != resolved_paths:
            raise _error("operation_physical_path_changed", "正式文件位置与提交时不一致，请重新核对")
    canonical, fields = _location(session, batch, baseline["project"], params, reviewed=reviewed)
    if target_path is None:
        target_path = canonical
    target = deepcopy(baseline)
    moment = _utc(moment) or utcnow()
    target["project"].setdefault("status", session.get(Project, batch.project_id).status)
    target["project"].update(fields, current_path=_path(target_path), canonical_path=canonical,
                             asset_version=(target["project"].get("asset_version") or 0) + 1,
                             archive_updated_at=archive.utc_text(moment))
    change = _ManifestChange(session, batch, snapshot, baseline, target, moment)
    if batch.kind in {"void", "restore", "purge"}:
        change.lifecycle(params)
    else:
        if target["project"]["status"] != "active":
            raise _error("operation_project_state", "已作废项目不能修改，须先按流程恢复")
        handlers = {"rename": change.rename, "retag": change.retag, "supplement": change.supplement}
        if batch.kind != "move":
            handlers[batch.kind](params)
    if batch.kind == "purge":
        freeze = snapshot.content["operation"].get("purge")
        if reviewed and (not freeze or freeze["files"] != change.purge):
            raise _error("purge_whitelist_changed", "执行前的实际删除清单与已审批的清单不一致")
    if reviewed:
        metadata = target["project"]["archive_review"] or {}
        actor_ids = {batch.uploader_id, snapshot.submitted_by, batch.first_reviewer_id, batch.final_reviewer_id}
        actors = {str(user.id): user.username for user in session.scalars(select(User).where(User.id.in_(actor_ids - {None})))}
        review = session.scalar(select(ReviewRound).where(ReviewRound.batch_id == batch.id, ReviewRound.round == batch.round,
                                                         ReviewRound.stage == "final", ReviewRound.decision == "pass"))
        target["project"]["archive_review"] = {
            **metadata, "batch_id": batch.id, "round": batch.round, "snapshot_id": snapshot.id,
            "snapshot_sha256": snapshot.digest, "submitted_by": snapshot.submitted_by,
            "first_reviewer_id": batch.first_reviewer_id, "final_reviewer_id": batch.final_reviewer_id,
            "submitted_at": archive.utc_text(snapshot.submitted_at), "reviewed_at": archive.utc_text(review.decided_at) if review else None,
            "operation": batch.kind, "uploader_id": batch.uploader_id, "actors": actors,
        }
        if fields.get("industry_id"):
            target["project"]["archive_review"]["industry"] = session.get(Industry, fields["industry_id"]).name
    result = {"manifest": archive._canonical(target), "sources": change.sources(), "purge": change.purge,
              "baseline": baseline, "old_manifest_hash": base_hash, "old_root": baseline["project"]["current_path"],
              "project_status": target["project"]["status"], "canonical_path": canonical,
              "batch_product_codes": {str(key): value for key, value in change.batch_product_codes.items()}}
    try:
        archive.validate(result["manifest"])
    except archive.ArchiveError as exc:
        raise _error("operation_manifest_invalid", str(exc)) from None
    if reviewed:
        change.reserve()
    return result


def prevalidate(session: Session, batch: Batch, snapshot: ReviewSnapshot | Row, moment: datetime | None = None) -> Row:
    """以相同业务规则检查待提交快照，不修改数据库，不消耗正式编号。"""
    if isinstance(snapshot, dict):
        snapshot = ReviewSnapshot(batch_id=batch.id, round=batch.round, submitted_by=snapshot["submitted_by"],
                                  submitted_at=_utc(moment) or utcnow(), content=snapshot)
    return _prepared(session, batch, snapshot, None, moment=moment, reviewed=False)


def plan(session: Session, batch: Batch, snapshot: ReviewSnapshot, target_path: str, *,
         canonical_path: str | None = None, moment: datetime | None = None) -> Row:
    """在准备事务中分配新身份并返回计划；调用方应在同一事务保存计划。"""
    result = _prepared(session, batch, snapshot, target_path, moment=moment, reviewed=True)
    if canonical_path is not None and result["canonical_path"] != canonical_path:
        raise _error("operation_destination_changed", "执行目录与核准的项目位置不一致")
    return result


def _assign(record: object, values: Row, date_fields: tuple[str, ...] = ()) -> None:
    for field, value in archive._with_dates(values, *date_fields).items():
        setattr(record, field, value)


def publish(session: Session, batch: Batch, prepared: Row) -> Project:
    """在调用方的发布事务中更新正式索引、档案身份和项目可见指针所用的行。"""
    target = prepared["manifest"]
    project = session.get(Project, batch.project_id)
    if project is None:
        raise _error("operation_project_missing", "对应的正式项目不存在")
    current = archive.from_catalog(session, project)
    if digest(current) != prepared["old_manifest_hash"]:
        raise _error("operation_base_changed", "发布时正式资产已发生变化，停止覆盖")
    if "baseline" in prepared and physical_paths(_physical_baseline(session, project.id, current)) != physical_paths(prepared["baseline"]):
        raise _error("operation_physical_path_changed", "发布时正式文件位置已经变化，停止覆盖")
    archive.validate(target)
    _assign(project, target["project"], ("ingested_at", "archive_updated_at", "voided_at", "deleted_at"))
    for model, rows, dates in ((CatalogFile, target["files"], ("voided_at", "deleted_at")),
                               (Product, target["products"], ("ingested_at", "voided_at", "deleted_at"))):
        current = {row.id: row for row in session.scalars(select(model).where(model.project_id == project.id))}
        for values in rows:
            record = current.get(values["id"])
            if record is None:
                record = model(first_batch_id=batch.id) if model is CatalogFile else model()
                session.add(record)
            _assign(record, values, dates)
        session.flush()

    product_ids = [row["id"] for row in target["products"]]
    current_tags = {(row.product_id, row.field_key): row for row in session.scalars(
        select(ProductTag).where(ProductTag.product_id.in_(product_ids)))}
    for values in target["tags"]:
        record = current_tags.get((values["product_id"], values["field_key"]))
        if record is None:
            session.add(ProductTag(**values))
        else:
            _assign(record, values)
    current_relations = {(row.set_product_id, row.item_product_id): row for row in session.scalars(
        select(ProductRelation).where(ProductRelation.set_product_id.in_(product_ids)))}
    for values in target["relations"]:
        record = current_relations.get((values["set_product_id"], values["item_product_id"]))
        existing_by = (record.confirmed_by_original_id or record.confirmed_by) if record else None
        source = values["confirmed_by"]
        original_id = record.confirmed_by_original_id if record else None
        if existing_by != source:
            original_id = source if source is not None and session.get(User, source) is None else None
        if record is None:
            record = ProductRelation()
            session.add(record)
        record.confirmed_by_original_id = original_id
        confirmed_by = source if record.confirmed_by_original_id is None else None
        _assign(record, {**values, "confirmed_by": confirmed_by}, ("confirmed_at",))

    current_history = {row.id: row for row in session.scalars(
        select(HistoryVersion).where(HistoryVersion.project_id == project.id))}
    for values in target["history"]:
        record = current_history.get(values["id"])
        if record is None:
            session.add(HistoryVersion(**values, batch_id=batch.id))
        elif record.expected_missing != values["expected_missing"]:
            record.expected_missing = values["expected_missing"]
    for values in target["links"]:
        record = session.scalar(select(Link).where(Link.kind == values["kind"], Link.target_id == values["target_id"]))
        if record is None:
            session.add(Link(**values))
        elif record.link_id != values["link_id"]:
            raise _error("operation_link_changed", "既有永久链接不得重新分配")
    session.flush()
    if not archive.matches_catalog(session, project, target):
        raise _error("operation_manifest_mismatch", "正式卡片柜与预备版本的档案数据不一致")
    return project
