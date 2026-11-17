"""三层档案：单品档案、套装档案、项目档案。

档案是卡片柜的投影：内容先整理成与存储无关的文档对象，再渲染成文本；同一份文档对象既可以
来自入库中的批次数据，也可以来自卡片柜，所以入库时写出的档案与事后按卡片柜重写的逐字节一致。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (Batch, BatchFile, BatchProduct, CatalogFile, HistoryVersion, Industry, Product, ProductRelation, ProductTag,
                           Project, TagField, User)
from app.services.nas import NasFs, NasNotFound, Region
from app.services.nas.paths import NasPaths

FORMAT_VERSION = 2
END_KEY, END_VALUE = "结束标记", "完"
PROJECT_ARCHIVE = "_项目档案.txt"
HISTORY_DIR = "_历史版本"
ARCHIVE_DIRS = {"item": "单品档案", "set": "套装档案"}
KIND_NAMES = {"item": "单品", "set": "套装"}
RELATION_SOURCES = {"ai": "AI推荐", "manual": "人工补充"}
TAG_SOURCES = {"ai": "AI", "manual": "人工", "ai_confirmed": "AI已确认"}
CODE_SOURCES = {"company": "公司立项编号", "system_h": "系统补发"}
STATUSES = {"active": "有效", "voided": "已作废", "deleted": "已删除"}
LOCAL = timezone(timedelta(hours=8))


def _reverse(mapping: dict[str, str]) -> dict[str, str]:
    return {value: key for key, value in mapping.items()}


def local_date(moment: datetime) -> str:
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).astimezone(LOCAL).strftime("%Y-%m-%d")


def human_size(size: int) -> str:
    value, units = float(size), ["B", "KB", "MB", "GB", "TB"]
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    return f"{size}B" if index == 0 else f"{value:.1f}".rstrip("0").rstrip(".") + units[index]


@dataclass(frozen=True)
class ProductDoc:
    code: str
    kind: str                      # item / set
    display_name: str
    project_code: str
    image_rel_path: str
    image_fingerprint: str
    # 套装：[(单品编号, ai/manual)]；单品：[(所属套装编号, "")]
    related: tuple[tuple[str, str], ...]
    tags: tuple[tuple[str, str, str], ...]   # (字段名, 值, ai/manual/ai_confirmed)
    free_text: str
    vision_model: str
    ingested_on: str
    status: str = "active"
    archive_rel_path: str = ""


@dataclass(frozen=True)
class FileLine:
    rel_path: str
    size: int
    fingerprint: str


@dataclass(frozen=True)
class ProductLine:
    code: str
    kind: str
    image_rel_path: str
    related: tuple[str, ...]       # 三位流水


@dataclass(frozen=True)
class HistoryLine:
    old_rel_path: str
    new_rel_path: str
    fingerprint: str
    reason: str


@dataclass(frozen=True)
class ProjectDoc:
    project_code: str
    code_source: str
    project_name: str
    company: str
    industry: str
    won: bool
    uploader: str
    first_reviewer: str
    final_reviewer: str
    ingested_on: str
    updated_on: str
    files: tuple[FileLine, ...] = ()
    products: tuple[ProductLine, ...] = ()
    history: tuple[HistoryLine, ...] = ()


@dataclass
class ArchiveSet:
    project: ProjectDoc
    products: list[ProductDoc] = field(default_factory=list)


class ArchiveError(ValueError):
    """档案缺少结束标记或结构不完整，视为损坏。"""


# ---- 渲染与解析：单品 / 套装档案 ----
def render_product(doc: ProductDoc) -> str:
    body: dict[str, object] = {"格式版本": FORMAT_VERSION, "商品编号": doc.code, "商品类型": KIND_NAMES[doc.kind], "展示名": doc.display_name,
        "立项编号": doc.project_code, "主图相对路径": doc.image_rel_path, "主图指纹": doc.image_fingerprint}
    if doc.kind == "set":
        body["包含单品"] = [{"商品编号": code, "来源": RELATION_SOURCES.get(source, source)} for code, source in doc.related]
    else:
        body["所属套装"] = [code for code, _ in doc.related]
    body.update({"标签": {label: value for label, value, _ in doc.tags},
        "标签来源": {label: TAG_SOURCES.get(source, source) for label, _, source in doc.tags},
        "自由描述": doc.free_text, "识图模型": doc.vision_model, "入库时间": doc.ingested_on,
        "状态": STATUSES.get(doc.status, doc.status), END_KEY: END_VALUE})
    return json.dumps(body, ensure_ascii=False, indent=2) + "\n"


def parse_product(text: str, archive_rel_path: str = "") -> ProductDoc:
    try:
        body = json.loads(text)
    except ValueError:
        raise ArchiveError("档案不是完整的 JSON") from None
    if not isinstance(body, dict) or body.get(END_KEY) != END_VALUE:
        raise ArchiveError("档案缺少结束标记")
    try:
        kind = _reverse(KIND_NAMES)[body["商品类型"]]
        sources = _reverse(RELATION_SOURCES)
        related = (tuple((item["商品编号"], sources.get(item["来源"], item["来源"])) for item in body["包含单品"]) if kind == "set"
                   else tuple((code, "") for code in body["所属套装"]))
        tag_sources = _reverse(TAG_SOURCES)
        tags = tuple((label, value, tag_sources.get(body["标签来源"][label], body["标签来源"][label])) for label, value in body["标签"].items())
        return ProductDoc(code=body["商品编号"], kind=kind, display_name=body["展示名"], project_code=body["立项编号"],
            image_rel_path=body["主图相对路径"], image_fingerprint=body["主图指纹"], related=related, tags=tags,
            free_text=body["自由描述"], vision_model=body["识图模型"], ingested_on=body["入库时间"],
            status=_reverse(STATUSES).get(body["状态"], body["状态"]), archive_rel_path=archive_rel_path)
    except (KeyError, TypeError):
        raise ArchiveError("档案字段不完整") from None


# ---- 渲染与解析：项目档案 ----
def render_project(doc: ProjectDoc) -> str:
    lines = [f"立项编号：{doc.project_code}   编号来源：{CODE_SOURCES.get(doc.code_source, doc.code_source)}",
             f"项目名称：{doc.project_name}",
             f"企业：{doc.company}   行业：{doc.industry}   中标：{'是' if doc.won else '否'}",
             f"上传人：{doc.uploader}   初审：{doc.first_reviewer}   终审：{doc.final_reviewer}   入库时间：{doc.ingested_on}",
             f"最后更新：{doc.updated_on}", "", "【文件清单】"]
    width = max((len(item.rel_path) for item in doc.files), default=0) + 4
    # 人读的大小后面括注精确字节数，解析回来才能与卡片柜逐项比对。
    lines += [f"{item.rel_path.ljust(width)}{(human_size(item.size) + '(' + str(item.size) + ')').ljust(24)}指纹 {item.fingerprint}" for item in doc.files] or ["（无）"]
    lines += ["", "【商品】"]
    image_width = max((len(item.image_rel_path) for item in doc.products), default=0) + 4
    for item in doc.products:
        line = f"{item.code}  {KIND_NAMES[item.kind]}  {item.image_rel_path}"
        if item.related:
            line = f"{item.code}  {KIND_NAMES[item.kind]}  {item.image_rel_path.ljust(image_width)}{'包含单品' if item.kind == 'set' else '所属套装'}：{'、'.join(item.related)}"
        lines.append(line)
    if not doc.products:
        lines.append("（无）")
    lines += ["", "【历史版本】"]
    lines += [f"{item.old_rel_path}  →  {item.new_rel_path}    指纹 {item.fingerprint}    原因 {item.reason}" for item in doc.history] or ["（无）"]
    lines += ["", f"{END_KEY}：{END_VALUE}"]
    return "\n".join(lines) + "\n"


_FILE = re.compile(r"^(.+?)\s{2,}\S+\((\d+)\)\s{2,}指纹 (\S+)$")
_PRODUCT = re.compile(r"^(\S+)  (单品|套装)  (.+?)(?:\s{2,}(?:包含单品|所属套装)：(\S+))?$")
_HISTORY = re.compile(r"^(.+?)  →  (.+?)\s{4}指纹 (\S+)\s{4}原因 (\S+)$")


def parse_project(text: str) -> ProjectDoc:
    lines = text.rstrip("\n").split("\n")
    if not lines or lines[-1] != f"{END_KEY}：{END_VALUE}":
        raise ArchiveError("档案缺少结束标记")
    try:
        head = "\n".join(lines[:5])
        def pick(label: str) -> str:
            match = re.search(rf"{label}：(.*?)(?:   |$)", head, re.MULTILINE)
            if match is None:
                raise ArchiveError("档案字段不完整")
            return match.group(1)
        sections: dict[str, list[str]] = {}
        current = ""
        for line in lines[5:-1]:
            if line.startswith("【") and line.endswith("】"):
                current = line
                sections[current] = []
            elif line and line != "（无）" and current:
                sections[current].append(line)
        files = tuple(FileLine(match[1], int(match[2]), match[3]) for match in (_FILE.match(line) for line in sections["【文件清单】"]) if match)
        products = tuple(ProductLine(match[1], _reverse(KIND_NAMES)[match[2]], match[3], tuple(match[4].split("、")) if match[4] else ())
                         for match in (_PRODUCT.match(line) for line in sections["【商品】"]) if match)
        history = tuple(HistoryLine(match[1], match[2], match[3], match[4]) for match in (_HISTORY.match(line) for line in sections["【历史版本】"]) if match)
        if len(files) != len(sections["【文件清单】"]) or len(products) != len(sections["【商品】"]) or len(history) != len(sections["【历史版本】"]):
            raise ArchiveError("档案清单行格式不正确")
        return ProjectDoc(project_code=pick("立项编号"), code_source=_reverse(CODE_SOURCES).get(pick("编号来源"), pick("编号来源")),
            project_name=pick("项目名称"), company=pick("企业"), industry=pick("行业"), won=pick("中标") == "是", uploader=pick("上传人"),
            first_reviewer=pick("初审"), final_reviewer=pick("终审"), ingested_on=pick("入库时间"), updated_on=pick("最后更新"),
            files=files, products=products, history=history)
    except KeyError:
        raise ArchiveError("档案缺少必要的段落") from None


# ---- 文档对象的两种来源 ----
def archive_name(kind: str, file_name: str, duplicated: bool) -> str:
    """档案以展示名命名；同一类型里展示名重复（如 a.jpg 与 a.png）时带上扩展名区分。"""
    return f"{ARCHIVE_DIRS[kind]}/{file_name if duplicated else file_name.rsplit('.', 1)[0]}.json"


def _labels(session: Session) -> dict[str, str]:
    return {row.key: row.label for row in session.scalars(select(TagField))}


def _name(session: Session, user_id: int | None) -> str:
    user = session.get(User, user_id) if user_id else None
    return (user.display_name or user.username) if user else ""


def _industry(path: str) -> str:
    parts = path.split("/")
    return re.sub(r"^\d{2}", "", parts[1]) if len(parts) >= 3 else ""


def _serial(code: str) -> str:
    return code.rsplit("-", 1)[-1]


def from_batch(session: Session, batch: Batch) -> ArchiveSet:
    """入库执行中、卡片柜尚未登记时使用；编号、入库时间、模型名已固定在批次上。"""
    labels = _labels(session)
    moment = datetime.fromisoformat(str(batch.payload["ingested_at"]))
    day = local_date(moment)
    files = {row.id: row for row in session.scalars(select(BatchFile).where(BatchFile.batch_id == batch.id)) if not row.removed}
    rows = sorted((row for row in session.scalars(select(BatchProduct).where(BatchProduct.batch_id == batch.id))
                   if row.batch_file_id in files and row.product_code), key=lambda row: row.product_code)
    codes = {row.id: row.product_code for row in rows}
    stems: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (row.kind, files[row.batch_file_id].rel_path.rsplit("/", 1)[-1].rsplit(".", 1)[0])
        stems[key] = stems.get(key, 0) + 1
    owners: dict[int, list[str]] = {}
    for row in rows:
        if row.kind == "set":
            for item in row.relation_items:
                if isinstance(item, dict) and item.get("batch_product_id") in codes:
                    owners.setdefault(item["batch_product_id"], []).append(row.product_code)
    products: list[ProductDoc] = []
    lines: list[ProductLine] = []
    parts = str(batch.payload["folder_name"]).split("_")
    for row in rows:
        file = files[row.batch_file_id]
        name = file.rel_path.rsplit("/", 1)[-1]
        if row.kind == "set":
            related = tuple(sorted((codes[item["batch_product_id"]], str(item.get("source", "ai"))) for item in row.relation_items
                                   if isinstance(item, dict) and item.get("batch_product_id") in codes))
        else:
            related = tuple((code, "") for code in sorted(owners.get(row.id, [])))
        tags = tuple((labels.get(key, key), str(tag.get("value", "")), str(tag.get("source", "ai")))
                     for key, tag in sorted(row.tags.items()) if isinstance(tag, dict))
        products.append(ProductDoc(code=row.product_code, kind=row.kind, display_name=row.display_name, project_code=batch.project_code,
            image_rel_path=file.rel_path, image_fingerprint=file.fingerprint or "", related=related, tags=tags, free_text=row.free_text,
            vision_model=str(batch.payload.get("ingest_model", "")), ingested_on=day,
            archive_rel_path=archive_name(row.kind, name, stems[(row.kind, name.rsplit(".", 1)[0])] > 1)))
        lines.append(ProductLine(row.product_code, row.kind, file.rel_path, tuple(_serial(code) for code, _ in related)))
    project = ProjectDoc(project_code=batch.project_code, code_source="system_h" if batch.project_code.startswith("H") else "company",
        project_name=parts[2], company=parts[1], industry=_industry(batch.final_path or ""), won=batch.won,
        uploader=_name(session, batch.uploader_id), first_reviewer=_name(session, batch.first_reviewer_id),
        final_reviewer=_name(session, batch.final_reviewer_id), ingested_on=day, updated_on=day,
        files=tuple(FileLine(row.rel_path, row.size, row.fingerprint or "") for row in sorted(files.values(), key=lambda row: row.rel_path)),
        products=tuple(lines))
    return ArchiveSet(project, products)


def from_catalog(session: Session, project: Project) -> ArchiveSet:
    """按卡片柜生成；verify 与 rebuild 以它为准。"""
    labels = _labels(session)
    files = {row.id: row for row in session.scalars(select(CatalogFile).where(CatalogFile.project_id == project.id))}
    # 物理删除后的商品没有档案，也不再列入项目档案；卡片柜里的记录仍然保留。
    rows = list(session.scalars(select(Product).where(Product.project_id == project.id, Product.status != "deleted").order_by(Product.product_code)))
    codes = {row.id: row.product_code for row in rows}
    relations = list(session.scalars(select(ProductRelation).where(ProductRelation.set_product_id.in_(codes) if codes else False)))
    products: list[ProductDoc] = []
    lines: list[ProductLine] = []
    for row in rows:
        file = files[row.file_id]
        if row.kind == "set":
            related = tuple(sorted((codes[item.item_product_id], item.source) for item in relations if item.set_product_id == row.id and item.item_product_id in codes))
        else:
            related = tuple((code, "") for code in sorted(codes[item.set_product_id] for item in relations if item.item_product_id == row.id and item.set_product_id in codes))
        tags = tuple((labels.get(tag.field_key, tag.field_key), tag.value, tag.source) for tag in session.scalars(
            select(ProductTag).where(ProductTag.product_id == row.id).order_by(ProductTag.field_key)))
        products.append(ProductDoc(code=row.product_code, kind=row.kind, display_name=row.display_name, project_code=project.project_code,
            image_rel_path=file.rel_path, image_fingerprint=file.fingerprint, related=related, tags=tags, free_text=row.free_text,
            vision_model=row.vision_model, ingested_on=local_date(row.ingested_at), status=row.status, archive_rel_path=row.archive_rel_path))
        lines.append(ProductLine(row.product_code, row.kind, file.rel_path, tuple(_serial(code) for code, _ in related)))
    batch = session.scalar(select(Batch).where(Batch.project_id == project.id, Batch.kind == "ingest").order_by(Batch.id))
    history = tuple(HistoryLine(row.old_rel_path, row.new_rel_path, row.old_fingerprint, row.reason) for row in session.scalars(
        select(HistoryVersion).where(HistoryVersion.project_id == project.id).order_by(HistoryVersion.id))
        if row.file_id in files and files[row.file_id].status != "deleted")
    # 项目被移到「_已作废项目」后路径里没有行业一层，所以优先用卡片柜里的行业。
    industry = session.get(Industry, project.industry_id) if project.industry_id else None
    doc = ProjectDoc(project_code=project.project_code, code_source=project.code_source, project_name=project.project_name,
        company=project.company_name, industry=industry.name if industry else _industry(project.current_path), won=project.won,
        uploader=_name(session, batch.uploader_id if batch else None), first_reviewer=_name(session, batch.first_reviewer_id if batch else None),
        final_reviewer=_name(session, batch.final_reviewer_id if batch else None),
        ingested_on=local_date(project.ingested_at) if project.ingested_at else "",
        updated_on=local_date(project.archive_updated_at or project.ingested_at) if project.ingested_at else "",
        files=tuple(FileLine(row.rel_path, row.size, row.fingerprint) for row in sorted(files.values(), key=lambda row: row.rel_path) if row.status == "active"),
        products=tuple(lines), history=history)
    return ArchiveSet(doc, products)


def documents(archive: ArchiveSet) -> dict[str, str]:
    """项目内相对路径 → 档案全文。"""
    result = {doc.archive_rel_path: render_product(doc) for doc in archive.products}
    result[PROJECT_ARCHIVE] = render_project(archive.project)
    return result


# ---- 写入、校验、重写 ----
def read_text(fs: NasFs, path: str) -> str:
    with fs.open_read(path) as stream:
        return stream.read().decode("utf-8")


def write_text(fs: NasFs, path: str, text: str) -> bool:
    """先写临时名 → 读回校验 → rename 为正式名；内容已一致时不重写，返回是否实际写入。"""
    data = text.encode("utf-8")
    if fs.exists(path):
        try:
            if read_text(fs, path) == text:
                return False
        except UnicodeDecodeError:
            pass
    temporary = path + ".写入中"
    if fs.exists(temporary):
        fs.delete(temporary)
    with fs.open_write(temporary) as stream:
        stream.write(data)
    with fs.open_read(temporary) as stream:
        if stream.read() != data:
            fs.delete(temporary)
            raise ArchiveError("档案写入后校验不一致")
    if fs.exists(path):
        fs.delete(path)
    fs.rename(temporary, path)
    return True


def write_documents(fs: NasFs, root: str, texts: dict[str, str]) -> int:
    written = 0
    for rel_path, text in sorted(texts.items()):
        target = f"{root}/{rel_path}"
        parent = target.rsplit("/", 1)[0]
        if not fs.exists(parent):
            fs.mkdir(parent, recursive=True)
        written += write_text(fs, target, text)
    return written


@dataclass(frozen=True)
class Difference:
    path: str
    problem: str   # missing / damaged / mismatch / unexpected


def project_root(paths: NasPaths, project: Project) -> str:
    return paths.join(Region.FORMAL, project.current_path)


def verify(session: Session, fs: NasFs, paths: NasPaths, project: Project) -> list[Difference]:
    """比对 NAS 上的档案与卡片柜，返回不一致清单（空表示一致）。只读，不写 NAS。"""
    root = project_root(paths, project)
    expected = documents(from_catalog(session, project))
    result: list[Difference] = []
    for rel_path, text in sorted(expected.items()):
        try:
            actual = read_text(fs, f"{root}/{rel_path}")
        except NasNotFound:
            result.append(Difference(rel_path, "missing"))
            continue
        except UnicodeDecodeError:
            result.append(Difference(rel_path, "damaged"))
            continue
        if actual == text:
            continue
        try:
            parse_project(actual) if rel_path == PROJECT_ARCHIVE else parse_product(actual)
            result.append(Difference(rel_path, "mismatch"))
        except ArchiveError:
            result.append(Difference(rel_path, "damaged"))
    for directory in ARCHIVE_DIRS.values():
        if fs.exists(f"{root}/{directory}"):
            for name in fs.list(f"{root}/{directory}"):
                if f"{directory}/{name}" not in expected:
                    result.append(Difference(f"{directory}/{name}", "unexpected"))
    return result


def rebuild(session: Session, fs: NasFs, paths: NasPaths, project: Project) -> int:
    """按卡片柜重写该项目全部档案并清掉多余的档案文件；返回实际改动的文件数。需要正式目录写权限。"""
    return apply_documents(fs, project_root(paths, project), documents(from_catalog(session, project)))


def apply_documents(fs: NasFs, root: str, expected: dict[str, str]) -> int:
    """把已经算好的档案全文写到 NAS；与取数分开，执行器可以在不占用数据库的情况下写文件。"""
    changed = write_documents(fs, root, expected)
    for directory in ARCHIVE_DIRS.values():
        if fs.exists(f"{root}/{directory}"):
            for name in fs.list(f"{root}/{directory}"):
                if f"{directory}/{name}" not in expected:
                    fs.delete(f"{root}/{directory}/{name}")
                    changed += 1
    if not fs.exists(f"{root}/{HISTORY_DIR}"):
        fs.mkdir(f"{root}/{HISTORY_DIR}")
    return changed
