"""项目 TXT 和商品 JSON 的生成、校验与卡片柜恢复。"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CatalogFile, HistoryVersion, Link, Product, ProductRelation, ProductTag, Project
from app.services.nas import NasAlreadyExists, NasFs, NasNotFound, NasPermissionDenied, safe_join
from app.services.upload.fingerprint import fingerprint

PRODUCT_DIR = {"item": "单品档案", "set": "套装档案"}
PROJECT_FILE = "_项目档案.txt"
HISTORY_DIR = "_历史版本"
IDENTITY_SEQUENCE = "catalog_identity_sequence"
DATA_START = "\n【恢复数据 JSON】\n"
END = "\n结束标记：完\n"
PROJECT_FIELDS = ("id", "project_code", "code_source", "company_name", "company_id", "industry_id", "won",
                  "project_name", "folder_name", "current_path", "ingested_at", "archive_updated_at", "archive_review")
FILE_FIELDS = ("id", "project_id", "rel_path", "size", "mtime", "fingerprint", "fingerprint_kind", "category", "status")
PRODUCT_FIELDS = ("id", "product_code", "project_id", "kind", "display_name", "file_id", "archive_rel_path", "status",
                  "free_text", "vision_model", "ingested_at")
TAG_FIELDS = ("product_id", "field_key", "value", "terms", "source", "ai_value", "flag", "confirmation_status")
RELATION_FIELDS = ("set_product_id", "item_product_id", "source", "confirmed_by", "confirmed_at", "status",
                   "inactive_reason", "relation_version")
HISTORY_FIELDS = ("id", "project_id", "file_id", "product_id", "old_fingerprint", "old_rel_path", "new_rel_path",
                  "reason", "expected_missing")
LINK_FIELDS = ("kind", "target_id", "link_id")
LIFECYCLE_FIELDS = ("voided_at", "void_reason", "void_batch_id", "deleted_at", "delete_reason")
# JSON 恢复数据在这一层保持为普通字典，进入卡片柜前核对身份及各对象间的引用。
Manifest = dict[str, Any]


class ArchiveError(ValueError):
    pass


def utc_text(moment: datetime | None) -> str | None:
    return (moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat() if moment else None


def _row(record: object, names: tuple[str, ...]) -> dict:
    values = {name: getattr(record, name) for name in names}
    return {key: utc_text(value) if isinstance(value, datetime) else value for key, value in values.items()}


def _optional(record: object, names: tuple[str, ...]) -> dict:
    return {key: value for key, value in _row(record, names).items() if value is not None}


def from_catalog(session: Session, project: Project) -> Manifest:
    products = list(session.scalars(select(Product).where(Product.project_id == project.id)))
    files = list(session.scalars(select(CatalogFile).where(CatalogFile.project_id == project.id)))
    ids = [p.id for p in products]
    tags = session.scalars(select(ProductTag).where(ProductTag.product_id.in_(ids)))
    relations = []
    for row in session.scalars(select(ProductRelation).where(ProductRelation.set_product_id.in_(ids))):
        restored = _row(row, RELATION_FIELDS)
        restored["confirmed_by"] = row.confirmed_by_original_id if row.confirmed_by_original_id is not None else row.confirmed_by
        relations.append(restored)
    history = session.scalars(select(HistoryVersion).where(HistoryVersion.project_id == project.id))
    links = []
    for kind, targets in (("project", [project.id]), ("product", ids), ("file", [f.id for f in files])):
        links.extend(_row(row, LINK_FIELDS) for row in session.scalars(select(Link).where(Link.kind == kind, Link.target_id.in_(targets))))
    metadata = _row(project, PROJECT_FIELDS)
    if project.asset_version:
        metadata.update(_optional(project, (*LIFECYCLE_FIELDS, "asset_version", "status", "canonical_path", "restore_path")))
    # 保持旧档案的序列化格式，发布新版本时才记录独立物理位置及生命周期。
    return {"version": 2, "project": metadata,
            "files": [{**_row(r, FILE_FIELDS), **(_optional(r, (*LIFECYCLE_FIELDS, "physical_rel_path", "void_prior_status"))
                       if project.asset_version else {})} for r in files],
            "products": [{**_row(r, PRODUCT_FIELDS), **_optional(r, LIFECYCLE_FIELDS)} for r in products],
            "tags": [_row(r, TAG_FIELDS) for r in tags],
            "relations": relations, "history": [_row(r, HISTORY_FIELDS) for r in history],
            "links": links}


def _canonical(data: Manifest) -> Manifest:
    order = {"files": ("id",), "products": ("id",), "tags": ("product_id", "field_key"),
             "relations": ("set_product_id", "item_product_id"), "history": ("id",), "links": ("kind", "target_id")}
    return {**data, **{name: sorted(data[name], key=lambda row: tuple(row[key] for key in keys))
                      for name, keys in order.items()}}


def validate(data: Manifest) -> None:
    try:
        project = data["project"]
        files = {row["id"]: row for row in data["files"]}
        products = {row["id"]: row for row in data["products"]}
        if data["version"] != 2 or len(files) != len(data["files"]) or len(products) != len(data["products"]):
            raise ArchiveError("档案版本或对象 ID 不合法")
        expected = {("project", project["id"]), *(("file", key) for key in files), *(("product", key) for key in products)}
        if any(type(key) is not int or key <= 0 or key >= 2**53 for _, key in expected):
            raise ArchiveError("对象 ID 不合法")
        links = {(row["kind"], row["target_id"]) for row in data["links"]}
        if links != expected or len(links) != len(data["links"]) or len({row["link_id"] for row in data["links"]}) != len(links):
            raise ArchiveError("永久链接与对象映射不一致")
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{12,64}", row["link_id"]) for row in data["links"]):
            raise ArchiveError("永久链接标识不合法")
        for row in [*files.values(), *products.values(), *data["history"]]:
            if row["project_id"] != project["id"]:
                raise ArchiveError("档案含其他项目的对象")
        for row in files.values():
            if "physical_rel_path" in row and safe_join(row["physical_rel_path"]) != row["physical_rel_path"]:
                raise ArchiveError("文件物理路径不合法")
            if safe_join(row["rel_path"]) != row["rel_path"] or not re.fullmatch(r"[0-9a-f]{64}", row["fingerprint"]) or row["fingerprint_kind"] != "full":
                raise ArchiveError("文件路径或完整 SHA-256 不合法")
        for row in products.values():
            code = row["product_code"]
            if row["file_id"] not in files or safe_join(code) != code or "/" in code or row["archive_rel_path"] != f"{PRODUCT_DIR[row['kind']]}/{code}.json":
                raise ArchiveError("商品编号、主图映射或档案路径不合法")
        for row in data["relations"]:
            if products[row["set_product_id"]]["kind"] != "set" or products[row["item_product_id"]]["kind"] != "item":
                raise ArchiveError("套装关系不合法")
        for row in data["tags"]:
            if row["product_id"] not in products:
                raise ArchiveError("标签缺少对应商品")
        for row in data["history"]:
            if row["file_id"] not in files or row["product_id"] is not None and row["product_id"] not in products:
                raise ArchiveError("历史文件或商品身份无法还原")
            if safe_join(row["new_rel_path"]) != row["new_rel_path"] or not row["new_rel_path"].startswith(HISTORY_DIR + "/"):
                raise ArchiveError("历史版本路径不合法")
    except (KeyError, TypeError, AttributeError, NasPermissionDenied) as exc:
        raise ArchiveError("档案结构不完整") from exc


def documents(data: Manifest) -> dict[str, str]:
    validate(data)
    data = _canonical(data)
    project = data["project"]
    files = {f["id"]: f for f in data["files"]}
    products = {p["id"]: p for p in data["products"]}
    links = {(row["kind"], row["target_id"]): row["link_id"] for row in data["links"]}
    docs = {}
    for row in products.values():
        image = files[row["file_id"]]
        is_set = row["kind"] == "set"
        related = "item_product_id" if is_set else "set_product_id"
        own = "set_product_id" if is_set else "item_product_id"
        doc = {"格式版本": 2, "商品编号": row["product_code"], "商品ID": row["id"], "展示名": row["display_name"],
               "商品类型": "套装" if is_set else "单品", "立项编号": project["project_code"], "项目ID": project["id"],
               "主图文件ID": image["id"], "主图相对路径": image["rel_path"], "主图指纹": image["fingerprint"],
               "商品永久链接ID": links["product", row["id"]], "主图永久链接ID": links["file", image["id"]],
               "包含单品" if is_set else "所属套装": [
                   {"商品ID": rel[related], "商品编号": products[rel[related]]["product_code"], "来源": rel["source"],
                    "状态": rel["status"], "关系版本": rel["relation_version"]} for rel in data["relations"] if rel[own] == row["id"]],
               "标签": {tag["field_key"]: tag for tag in data["tags"] if tag["product_id"] == row["id"]},
               "自由描述": row["free_text"], "识图模型": row["vision_model"], "状态": row["status"],
               "入库时间": row["ingested_at"], "历史版本": [{**h, "文件永久链接ID": links["file", h["file_id"]]} for h in data["history"] if h["product_id"] == row["id"]],
               "结束标记": "完"}
        if row.get("voided_at") or row.get("deleted_at"):
            doc["生命周期"] = {key: row[key] for key in LIFECYCLE_FIELDS if key in row}
        docs[row["archive_rel_path"]] = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    audit = project["archive_review"] or {}
    readable = [f"立项编号：{project['project_code']}  项目ID：{project['id']}  编号来源：{project['code_source']}",
                f"项目名称：{project['project_name']}  企业：{project['company_name']}  行业：{audit.get('industry', '')}  中标：{'是' if project['won'] else '否'}",
                f"正式位置：{project['current_path']}  档案生成：{project['archive_updated_at']}",
                f"审核轮次：{audit.get('round', '')}  快照 SHA-256：{audit.get('snapshot_sha256', '')}", "", "【文件清单】"]
    readable.extend(f"{f['rel_path']}  {f['size']} 字节  {f['fingerprint']}" for f in files.values())
    readable.extend(["", "【商品】"])
    readable.extend(f"{p['product_code']}  {p['display_name']}  {files[p['file_id']]['rel_path']}" for p in products.values())
    readable.extend(["", "【历史版本】"])
    readable.extend(f"{h['old_rel_path']} → {h['new_rel_path']}" for h in data["history"])
    docs[PROJECT_FILE] = "\n".join(readable) + DATA_START + json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + END
    return docs


def _read(fs: NasFs, path: str) -> str:
    with fs.open_read(path) as stream:
        return stream.read().decode("utf-8")


def mkdir(fs: NasFs, path: str, *, recursive: bool = False) -> None:
    if not fs.exists(path):
        try:
            fs.mkdir(path, recursive=recursive)
        except NasAlreadyExists:
            pass
    if not fs.stat(path).is_dir:
        raise ArchiveError("所需目录被同名文件占用")


def write(fs: NasFs, root: str, docs: dict[str, str], *, replace: bool = False) -> None:
    for relative, content in docs.items():
        path = safe_join(root, relative)
        occupied = fs.exists(path)
        if occupied:
            try:
                if _read(fs, path) == content:
                    continue
            except UnicodeError:
                if not replace:
                    raise ArchiveError("目标档案无法解码") from None
            if not replace:
                raise ArchiveError("已有档案与本次入库内容不一致")
        temp = safe_join(root, HISTORY_DIR, PROJECT_FILE + ".tmp") if relative == PROJECT_FILE else path + ".tmp"
        with fs.open_write(temp) as stream:
            body = content.encode("utf-8")
            if stream.write(body) != len(body):
                raise ArchiveError("档案未完整写入")
            stream.flush()
        if _read(fs, temp) != content:
            raise ArchiveError("档案读回校验失败")
        if occupied:
            fs.replace(temp, path)
        else:
            fs.rename(temp, path)
        if _read(fs, path) != content:
            raise ArchiveError("档案发布后校验失败")


def parse_project(fs: NasFs, root: str) -> Manifest:
    """核验项目 TXT 本身，供巡检在某份 SKU 档案损坏时定位项目身份。"""
    try:
        raw = _read(fs, safe_join(root, PROJECT_FILE))
        if not raw.endswith(END) or DATA_START not in raw:
            raise ArchiveError("项目档案缺少有效恢复数据或结束标记")
        payload = json.loads(raw.rsplit(DATA_START, 1)[1][:-len(END)])
        if documents(payload)[PROJECT_FILE] != raw:
            raise ArchiveError("项目档案与结构化数据不一致")
        return payload
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ArchiveError("档案内容损坏") from exc


def parse(fs: NasFs, root: str) -> Manifest:
    payload = parse_project(fs, root)
    try:
        for path, doc in documents(payload).items():
            if path != PROJECT_FILE and json.loads(_read(fs, safe_join(root, path))) != json.loads(doc):
                raise ArchiveError("商品档案与项目档案不一致")
        return payload
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ArchiveError("档案内容损坏") from exc


def verify(session: Session, project: Project, fs: NasFs, root: str) -> list[str]:
    try:
        actual = parse(fs, root)
        if _canonical(actual) != _canonical(from_catalog(session, project)):
            return ["SQLite 与 NAS 档案的正式数据或链接映射不一致"]
    except (ArchiveError, NasNotFound) as exc:
        return [str(exc)]
    return []


def rebuild(session: Session, project: Project, fs: NasFs, root: str) -> None:
    fs.check_write_directory(root)
    for name in (*PRODUCT_DIR.values(), HISTORY_DIR):
        mkdir(fs, safe_join(root, name))
    write(fs, root, documents(from_catalog(session, project)), replace=True)


def _with_dates(row: dict, *fields: str) -> dict:
    return {**row, **{field: datetime.fromisoformat(row[field]) if row[field] else None for field in fields if field in row}}


def matches_catalog(session: Session, project: Project, data: Manifest) -> bool:
    return _canonical(from_catalog(session, project)) == _canonical(data)


def register(session: Session, data: Manifest, batch_id: int | None = None, *, current_path: str | None = None) -> Project:
    """由调用方在一个事务内登记。恢复时暂不公开，待重新生成向量。"""
    validate(data)
    fields = _with_dates(data["project"], "ingested_at", "archive_updated_at", "voided_at", "deleted_at")
    if current_path is not None:
        fields["current_path"] = current_path
    status = fields.pop("status", "active")
    project = Project(**fields, status=status if status in {"voided", "deleted"} else "ingesting")
    session.add(project)
    session.flush()
    session.add_all(CatalogFile(**_with_dates(row, "voided_at", "deleted_at"), first_batch_id=batch_id) for row in data["files"])
    session.flush()
    session.add_all(Product(**_with_dates(row, "ingested_at", "voided_at", "deleted_at")) for row in data["products"])
    session.flush()
    session.add_all(ProductTag(**row) for row in data["tags"])
    for row in data["relations"]:
        values = _with_dates(row, "confirmed_at")
        if batch_id is None and values["confirmed_by"] is not None:
            # 恢复后的用户主键可能已被另一个账号使用。
            values["confirmed_by_original_id"] = values["confirmed_by"]
            # 保留档案中原审核身份，重建库无法建立不存在的用户外键。
            values["confirmed_by"] = None
        session.add(ProductRelation(**values))
    session.add_all(HistoryVersion(**row, batch_id=batch_id) for row in data["history"])
    session.add_all(Link(**row) for row in data["links"])
    session.flush()
    return project


def restore(session: Session, fs: NasFs, root: str, current_path: str) -> Project:
    data = parse(fs, root)
    history = {row["file_id"]: row for row in sorted(data["history"], key=lambda row: row["id"])}
    for file in data["files"]:
        old = history.get(file["id"])
        if (old and old["expected_missing"]) or file["status"] == "deleted":
            continue
        path = safe_join(root, file.get("physical_rel_path") or (old["new_rel_path"] if old else file["rel_path"]))
        if fingerprint(fs, path, file["size"]) != file["fingerprint"]:
            raise ArchiveError("恢复时文件 SHA-256 与档案不一致")
    return register(session, data, current_path=safe_join(current_path))
