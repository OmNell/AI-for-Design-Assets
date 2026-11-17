"""postop 任务的执行器：生成计划 → 移动与写入文件 → 更新卡片柜 → 更新档案 → 完成。

计划在第一次执行时算好并存进批次（文件的去向、历史版本的文件名、新商品的编号都在这时定下），
之后每一步只按计划做事并各自检查「是否已完成」，所以从任何一步、任何一个文件操作中断后都能继续。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import shutil

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import (Batch, BatchFile, BatchProduct, CatalogFile, CompanyIndustryMap, HistoryVersion, Industry, Product,
                           ProductRelation, ProductTag, Project, utcnow)
from app.services import archive
from app.services.ai.orchestrator import active_products
from app.services.ai.parsing import split_terms
from app.services.batch import DONE, RUNNING, kind_label
from app.services.ingest import IngestError, category_of
from app.services.jobs import JobContext, JobFailure
from app.services.links import ensure_link
from app.services.nas import NasFs, Region
from app.services.oplog import OplogService
from app.services.upload import supplement

VOIDED_ROOT = "_已作废项目"


class Executor:
    # ---- 任务入口 ----
    def handle(self, context: JobContext) -> None:
        batch_id = context.job.payload["batch_id"]
        with self.auth.lock, self.auth.database.session() as session:
            batch = session.get(Batch, batch_id)
            if batch is None or batch.kind == "ingest" or batch.status != RUNNING or batch.final_reviewer_id != context.job.run_as_user_id:
                return
        if context.fs is None:
            raise JobFailure("操作人会话已失效", retryable=False)
        self.ingest.run_steps(batch_id, context.fs, {1: self._plan, 2: self._files, 3: self._apply, 4: self._archive, 5: self._finish})

    def _formal(self, *parts: str) -> str:
        return self.paths.join(Region.FORMAL, *parts)

    def _log(self, session: Session, batch: Batch, action: str, kind: str, object_id: int, before: object, after: object) -> None:
        OplogService(session).write(batch.final_reviewer_id, "final_reviewer", action, kind, str(object_id), before, after, "成功", "入库后操作任务")

    def _history_names(self, session: Session, fs: NasFs, project: Project) -> Callable[[str], str]:
        """历史版本的文件名：原名加日期，同一天重复再加序号。"""
        taken = {path.casefold() for path in session.scalars(select(CatalogFile.rel_path).where(CatalogFile.project_id == project.id))}
        date = archive.local_date(utcnow()).replace("-", "")

        def allocate(rel_path: str) -> str:
            name = rel_path.rsplit("/", 1)[-1]
            stem, dot, extension = name.rpartition(".")
            if not dot:
                stem, extension = name, ""
            number = 1
            while True:
                candidate = f"{archive.HISTORY_DIR}/{stem}_{date}{'' if number == 1 else f'_{number}'}{'.' + extension if dot else ''}"
                if candidate.casefold() not in taken and not fs.exists(self._formal(project.current_path, candidate)):
                    taken.add(candidate.casefold())
                    return candidate
                number += 1
        return allocate

    # ---- 第 1 步：生成执行计划 ----
    def _plan(self, batch_id: int, fs: NasFs) -> None:
        with self.auth.lock, self.auth.database.session() as session:
            batch = session.get(Batch, batch_id)
            if isinstance(batch.payload.get("plan"), dict):
                return
            project = session.get(Project, batch.project_id)
            params = batch.payload.get("params", {})
            if batch.kind == "void":
                name = f"_plan_{'restore' if params.get('action') == 'restore' else 'void'}_{'project' if params.get('scope') == 'project' else 'items'}"
            else:
                name = f"_plan_{batch.kind}"
            plan = getattr(self, name)(session, fs, batch, project)
            payload = {**batch.payload, "plan": plan}
            # 修改标签和补充的预览依赖当前卡片柜，在落盘前定格。
            payload.setdefault("preview", self._build_preview(session, batch).model_dump())
            batch.payload = payload

    def _plan_rename(self, session: Session, fs: NasFs, batch: Batch, project: Project) -> dict[str, object]:
        params = batch.payload["params"]
        parent = project.current_path.rsplit("/", 1)[0]
        folder = f"{project.project_code}_{project.company_name}_{params['project_name']}" if params.get("project_name") else project.folder_name
        root = f"{parent}/{folder}"
        ops: list[dict[str, object]] = []
        if root != project.current_path:
            # 先改文件夹名，之后的文件改名都在新文件夹里进行。
            ops.append({"op": "move", "from": self._formal(project.current_path), "to": self._formal(root)})
        files = []
        for item in params.get("products", []):
            product = session.get(Product, item["id"])
            file = session.get(CatalogFile, product.file_id)
            directory, old = file.rel_path.split("/", 1)
            target = f"{directory}/{item['name']}.{old.rsplit('.', 1)[1]}"
            ops.append({"op": "move", "from": self._formal(root, file.rel_path), "to": self._formal(root, target)})
            files.append({"file_id": file.id, "product_id": product.id, "to": target, "name": item["name"]})
        return {"ops": ops, "root": root, "folder": folder, "files": files}

    def _plan_retag(self, session: Session, fs: NasFs, batch: Batch, project: Project) -> dict[str, object]:
        return {"ops": []}

    def _plan_void_items(self, session: Session, fs: NasFs, batch: Batch, project: Project) -> dict[str, object]:
        params = batch.payload["params"]
        allocate = self._history_names(session, fs, project)
        ops: list[dict[str, object]] = [{"op": "mkdir", "path": self._formal(project.current_path, archive.HISTORY_DIR)}]
        moves = []
        targets = [(session.get(Product, value).file_id, value) for value in params.get("product_ids", [])] + [(value, None) for value in params.get("file_ids", [])]
        for file_id, product_id in targets:
            file = session.get(CatalogFile, file_id)
            history = allocate(file.rel_path)
            ops.append({"op": "move", "from": self._formal(project.current_path, file.rel_path), "to": self._formal(project.current_path, history)})
            moves.append({"file_id": file.id, "product_id": product_id, "from": file.rel_path, "to": history})
        return {"ops": ops, "moves": moves}

    def _plan_void_project(self, session: Session, fs: NasFs, batch: Batch, project: Project) -> dict[str, object]:
        target = f"{VOIDED_ROOT}/{project.folder_name}"
        if fs.exists(self._formal(target)) and fs.exists(self._formal(project.current_path)):
            raise IngestError("「_已作废项目」里已有同名的项目文件夹")
        return {"ops": [{"op": "mkdir", "path": self._formal(VOIDED_ROOT)}, {"op": "move", "from": self._formal(project.current_path), "to": self._formal(target)}],
                "original_path": project.current_path, "target": target}

    def _plan_restore_items(self, session: Session, fs: NasFs, batch: Batch, project: Project) -> dict[str, object]:
        params = batch.payload["params"]
        owners = {session.get(Product, value).file_id: value for value in params.get("product_ids", [])}
        ops: list[dict[str, object]] = []
        moves = []
        for file_id in params.get("file_ids", []):
            file = session.get(CatalogFile, file_id)
            row = session.scalar(select(HistoryVersion).where(HistoryVersion.file_id == file.id, HistoryVersion.reason == "voided").order_by(HistoryVersion.id.desc()))
            if row is None:
                raise IngestError(f"找不到 {file.rel_path} 作废前的位置")
            if "/" in row.old_rel_path:
                ops.append({"op": "mkdir", "path": self._formal(project.current_path, row.old_rel_path.rsplit("/", 1)[0])})
            ops.append({"op": "move", "from": self._formal(project.current_path, file.rel_path), "to": self._formal(project.current_path, row.old_rel_path)})
            moves.append({"file_id": file.id, "product_id": owners.get(file.id), "history_id": row.id, "from": file.rel_path, "to": row.old_rel_path})
        return {"ops": ops, "moves": moves}

    def _plan_restore_project(self, session: Session, fs: NasFs, batch: Batch, project: Project) -> dict[str, object]:
        original = str(project.lifecycle.get("original_path", ""))
        if not original:
            raise IngestError("找不到该项目作废前的位置")
        return {"ops": [{"op": "mkdir", "path": self._formal(original.rsplit("/", 1)[0])},
                        {"op": "move", "from": self._formal(project.current_path), "to": self._formal(original)}], "target": original}

    def _plan_move(self, session: Session, fs: NasFs, batch: Batch, project: Project) -> dict[str, object]:
        params = batch.payload["params"]
        company = session.get(CompanyIndustryMap, params["company_id"])
        if company is None:
            raise IngestError("目标企业已不在对照表中")
        industry = session.scalar(select(Industry).where(Industry.name == company.industry_name))
        target = f"{params['target']}/{project.folder_name}"
        return {"ops": [{"op": "mkdir", "path": self._formal(str(params["target"]))},
                        {"op": "move", "from": self._formal(project.current_path), "to": self._formal(target)}],
                "target": target, "won": params["won"], "company_id": company.id, "industry_id": industry.id if industry else None}

    def _plan_purge(self, session: Session, fs: NasFs, batch: Batch, project: Project) -> dict[str, object]:
        params = batch.payload["params"]
        # 删除前先登记「预期消失」，巡检看到这些文件不在时不会报丢失。
        for file_id in params["file_ids"]:
            file = session.get(CatalogFile, file_id)
            file.lifecycle = {**file.lifecycle, "expected_missing": True}
            for row in session.scalars(select(HistoryVersion).where(HistoryVersion.file_id == file.id)):
                row.expected_missing = True
        if params.get("project"):
            project.lifecycle = {**project.lifecycle, "expected_missing": True}
            return {"ops": [{"op": "delete", "path": self._formal(project.current_path), "recursive": True}]}
        return {"ops": [{"op": "delete", "path": self._formal(project.current_path, session.get(CatalogFile, file_id).rel_path)} for file_id in params["file_ids"]]}

    def _plan_supplement(self, session: Session, fs: NasFs, batch: Batch, project: Project) -> dict[str, object]:
        folder = str(batch.payload["folder_name"])
        existing = supplement.catalog_files(session, project.id)
        allocate = self._history_names(session, fs, project)
        files = [row for row in session.scalars(select(BatchFile).where(BatchFile.batch_id == batch.id).order_by(BatchFile.rel_path))
                 if not row.removed and row.conflict != "duplicate"]
        ops: list[dict[str, object]] = []
        replaced, imports = [], []
        made: set[str] = set()

        def directory(path: str) -> None:
            if path not in made:
                made.add(path)
                ops.append({"op": "mkdir", "path": path})

        for file in files:
            target = file.rel_path
            old = existing.get(file.rel_path.casefold())
            if file.conflict == "conflict" and file.conflict_choice == "replace" and old is not None:
                # 先把旧文件移入历史版本，再把新文件放到原位置（沿用库里原有的路径写法）。
                history = allocate(old.rel_path)
                directory(self._formal(project.current_path, archive.HISTORY_DIR))
                ops.append({"op": "move", "from": self._formal(project.current_path, old.rel_path), "to": self._formal(project.current_path, history)})
                replaced.append({"batch_file_id": file.id, "old_file_id": old.id, "history": history})
                target = old.rel_path
            elif old is not None:
                raise IngestError(f"项目里已出现同名文件：{file.rel_path}")
            if "/" in target:
                directory(self._formal(project.current_path, target.rsplit("/", 1)[0]))
            ops.append({"op": "move", "from": self.paths.join(Region.TEMP, batch.batch_no, folder, file.rel_path), "to": self._formal(project.current_path, target)})
            imports.append({"batch_file_id": file.id, "to": target})
        # 临时区里剩下的只有被跳过的重复文件，连同空目录一起清掉；批次的回收区不动。
        ops.append({"op": "delete", "path": self.paths.join(Region.TEMP, batch.batch_no, folder), "recursive": True})
        # 新商品接着原项目的流水发放编号：先单品后套装，各按文件名排序。
        names = {row.id: row.rel_path.rsplit("/", 1)[-1] for row in files}
        products = [row for row in active_products(session, batch.id) if row.batch_file_id in names]
        serial = max((int(code.rsplit("-", 1)[1]) for code in session.scalars(select(Product.product_code).where(Product.project_id == project.id))), default=0)
        for row in sorted((row for row in products if row.existing_product_id is None), key=lambda row: (row.kind != "item", names[row.batch_file_id])):
            serial += 1
            if serial > 999:
                raise IngestError("商品超过 999 个，三位流水不够用")
            row.product_code = f"{project.project_code}-{serial:03d}"
        for row in products:
            if row.existing_product_id is not None:
                row.product_code = session.get(Product, row.existing_product_id).product_code
        return {"ops": ops, "replaced": replaced, "imports": imports}

    # ---- 第 2 步：移动与写入文件 ----
    @staticmethod
    def _run_op(fs: NasFs, op: dict[str, object]) -> None:
        if op["op"] == "mkdir":
            if not fs.exists(str(op["path"])):
                fs.mkdir(str(op["path"]), recursive=True)
        elif op["op"] == "delete":
            if fs.exists(str(op["path"])):
                fs.delete(str(op["path"]), recursive=bool(op.get("recursive")))
        else:
            source, target = str(op["from"]), str(op["to"])
            has_source, has_target = fs.exists(source), fs.exists(target)
            if not has_source and has_target:
                return
            name = target.rsplit("/", 1)[-1]
            if has_source and has_target:
                raise IngestError(f"目标位置已存在同名文件或文件夹：{name}")
            if not has_source:
                raise IngestError(f"找不到要移动的文件或文件夹：{source.rsplit('/', 1)[-1]}")
            fs.rename(source, target)

    def _files(self, batch_id: int, fs: NasFs) -> None:
        with self.auth.lock, self.auth.database.session() as session:
            payload = session.get(Batch, batch_id).payload
            ops, done = list(payload["plan"]["ops"]), int(payload.get("ops_done", 0))
        # 做完一个记一个：替换时旧文件移走后原位置会被新文件占上，光看磁盘分不清做没做过。
        # 「做完但还没记上」就断掉的那一个，靠 _run_op 里的检查跳过。
        for index, op in enumerate(ops, 1):
            if index <= done:
                continue
            self._run_op(fs, op)
            with self.auth.lock, self.auth.database.session() as session:
                batch = session.get(Batch, batch_id)
                batch.payload = {**batch.payload, "ops_done": index}
            if self.fault_after_ops == index:
                self.fault_after_ops = None
                raise IngestError("故障注入：文件操作中途中断")

    # ---- 第 3 步：更新卡片柜（一个事务） ----
    def _apply(self, batch_id: int, fs: NasFs) -> None:
        with self.auth.lock, self.auth.database.session() as session:
            batch = session.get(Batch, batch_id)
            if batch.payload.get("applied"):
                return
            stats: dict[int, int] = {}
            pending = [(item["batch_file_id"], self._formal(session.get(Project, batch.project_id).current_path, item["to"]))
                       for item in batch.payload["plan"].get("imports", [])]
        for file_id, path in pending:
            stats[file_id] = int(fs.stat(path).mtime * 1000)
        with self.auth.lock, self.auth.database.session() as session:
            batch = session.get(Batch, batch_id)
            project = session.get(Project, batch.project_id)
            params = batch.payload.get("params", {})
            if batch.kind == "void":
                name = f"_apply_{'restore' if params.get('action') == 'restore' else 'void'}_{'project' if params.get('scope') == 'project' else 'items'}"
            else:
                name = f"_apply_{batch.kind}"
            affected = getattr(self, name)(session, batch, project, stats)
            if project.status != "deleted":
                project.archive_updated_at = utcnow()
            batch.payload = {**batch.payload, "applied": True, "affected": sorted(set(affected))}

    def _apply_rename(self, session: Session, batch: Batch, project: Project, stats: dict[int, int]) -> list[int]:
        plan, params = batch.payload["plan"], batch.payload["params"]
        if params.get("project_name"):
            before = {"project_name": project.project_name, "folder_name": project.folder_name, "current_path": project.current_path}
            project.project_name, project.folder_name, project.current_path = str(params["project_name"]), str(plan["folder"]), str(plan["root"])
            self._log(session, batch, "项目改名", "project", project.id, before,
                      {"project_name": project.project_name, "folder_name": project.folder_name, "current_path": project.current_path})
        affected = []
        products = [row for row in session.scalars(select(Product).where(Product.project_id == project.id)) if row.status != "deleted"]
        for item in plan["files"]:
            file, product = session.get(CatalogFile, item["file_id"]), session.get(Product, item["product_id"])
            before = {"display_name": product.display_name, "rel_path": file.rel_path, "archive_rel_path": product.archive_rel_path}
            file.rel_path, product.display_name = str(item["to"]), str(item["name"])
            name = file.rel_path.rsplit("/", 1)[-1]
            plain = archive.archive_name(product.kind, name, False)
            product.archive_rel_path = archive.archive_name(product.kind, name, any(
                other.id != product.id and other.archive_rel_path.casefold() == plain.casefold() for other in products))
            self._log(session, batch, "商品改名", "product", product.id, before,
                      {"display_name": product.display_name, "rel_path": file.rel_path, "archive_rel_path": product.archive_rel_path})
            affected.append(product.id)
        return affected

    def _apply_retag(self, session: Session, batch: Batch, project: Project, stats: dict[int, int]) -> list[int]:
        changes = self.retag_changes(session, batch)
        now = utcnow()
        grouped: dict[int, list[dict[str, object]]] = {}
        for change in changes:
            grouped.setdefault(int(change["product_id"]), []).append(change)
        affected: list[int] = []
        for product_id, items in grouped.items():
            product = session.get(Product, product_id)
            for change in items:
                if change["type"] == "tag":
                    tag = session.scalar(select(ProductTag).where(ProductTag.product_id == product.id, ProductTag.field_key == change["key"]))
                    value = str(change["after"])
                    if tag is None:
                        session.add(ProductTag(product_id=product.id, field_key=str(change["key"]), value=value, terms=split_terms(value), source="manual", ai_value=""))
                    else:
                        tag.value, tag.terms, tag.source, tag.flag = value, split_terms(value), "manual", None
                elif change["type"] == "free_text":
                    product.free_text = str(change["after"])
                else:
                    for item_id in change["removed"]:
                        session.execute(delete(ProductRelation).where(ProductRelation.set_product_id == product.id, ProductRelation.item_product_id == item_id))
                    for item_id in change["added"]:
                        session.add(ProductRelation(set_product_id=product.id, item_product_id=item_id, source="manual", confirmed_by=batch.owner_id, confirmed_at=now))
                    affected += [*change["removed"], *change["added"]]
            self._log(session, batch, "修改标签与关系", "product", product.id, {str(item["label"]): item["before"] for item in items},
                      {str(item["label"]): item["after"] for item in items})
            affected.append(product.id)
        return affected

    @staticmethod
    def _relations_of(session: Session, product_id: int) -> list[ProductRelation]:
        return list(session.scalars(select(ProductRelation).where((ProductRelation.set_product_id == product_id) | (ProductRelation.item_product_id == product_id))))

    def _apply_void_items(self, session: Session, batch: Batch, project: Project, stats: dict[int, int]) -> list[int]:
        params = batch.payload["params"]
        now = utcnow().isoformat()
        affected: list[int] = []
        for move in batch.payload["plan"]["moves"]:
            file = session.get(CatalogFile, move["file_id"])
            product = session.get(Product, move["product_id"]) if move["product_id"] else None
            snapshot: dict[str, object] = {"reason": params["reason"]}
            if product is not None:
                # 套装作废后包含关系随之失效；记下来，恢复时再接回。
                relations = self._relations_of(session, product.id)
                snapshot["relations"] = [{"set_id": row.set_product_id, "item_id": row.item_product_id, "source": row.source} for row in relations]
                affected += [row.item_product_id for row in relations] + [row.set_product_id for row in relations]
                for row in relations:
                    session.delete(row)
                before = {"status": product.status}
                product.status = "voided"
                product.lifecycle = {**product.lifecycle, "voided_at": now, "void_reason": params["reason"], "voided_by": "item", "void_batch_id": batch.id}
                self._log(session, batch, "商品作废", "product", product.id, before, {"status": "voided", "reason": params["reason"], "history": move["to"]})
                affected.append(product.id)
            session.add(HistoryVersion(project_id=project.id, file_id=file.id, product_id=product.id if product else None, old_fingerprint=file.fingerprint,
                old_rel_path=str(move["from"]), new_rel_path=str(move["to"]), reason="voided", batch_id=batch.id, expected_missing=False, snapshot=snapshot))
            file.rel_path, file.status = str(move["to"]), "voided"
            file.lifecycle = {**file.lifecycle, "voided_at": now, "void_reason": params["reason"]}
            if product is None:
                self._log(session, batch, "文件作废", "file", file.id, {"rel_path": move["from"], "status": "active"}, {"rel_path": move["to"], "status": "voided", "reason": params["reason"]})
        return affected

    def _apply_void_project(self, session: Session, batch: Batch, project: Project, stats: dict[int, int]) -> list[int]:
        params, plan = batch.payload["params"], batch.payload["plan"]
        now = utcnow().isoformat()
        before = {"status": project.status, "current_path": project.current_path}
        project.status, project.current_path = "voided", str(plan["target"])
        project.lifecycle = {**project.lifecycle, "voided_at": now, "void_reason": params["reason"], "original_path": plan["original_path"], "void_batch_id": batch.id}
        affected = []
        for product in session.scalars(select(Product).where(Product.project_id == project.id, Product.status == "active")):
            product.status = "voided"
            product.lifecycle = {**product.lifecycle, "voided_at": now, "void_reason": params["reason"], "voided_by": "project", "void_batch_id": batch.id}
            affected.append(product.id)
        self._log(session, batch, "整项目作废", "project", project.id, before,
                  {"status": "voided", "current_path": project.current_path, "reason": params["reason"], "products": len(affected)})
        return affected

    def _apply_restore_items(self, session: Session, batch: Batch, project: Project, stats: dict[int, int]) -> list[int]:
        now = utcnow().isoformat()
        affected: list[int] = []
        pending: list[tuple[int, list[dict[str, object]]]] = []
        for move in batch.payload["plan"]["moves"]:
            file = session.get(CatalogFile, move["file_id"])
            row = session.get(HistoryVersion, move["history_id"])
            file.rel_path, file.status, file.category = str(move["to"]), "active", category_of(str(move["to"]))
            file.lifecycle = {**file.lifecycle, "restored_at": now}
            if move["product_id"]:
                product = session.get(Product, move["product_id"])
                product.status = "active"
                product.lifecycle = {**product.lifecycle, "restored_at": now}
                snapshot = row.snapshot if row is not None and isinstance(row.snapshot, dict) else {}
                pending.append((product.id, list(snapshot.get("relations", []))))
                self._log(session, batch, "商品恢复", "product", product.id, {"status": "voided"}, {"status": "active", "rel_path": file.rel_path})
                affected.append(product.id)
            else:
                self._log(session, batch, "文件恢复", "file", file.id, {"rel_path": move["from"], "status": "voided"}, {"rel_path": move["to"], "status": "active"})
            if row is not None:
                session.delete(row)
        session.flush()
        for product_id, relations in pending:
            for item in relations:
                ends = [session.get(Product, item["set_id"]), session.get(Product, item["item_id"])]
                if all(end is not None and end.status == "active" for end in ends):
                    if session.scalar(select(ProductRelation.id).where(ProductRelation.set_product_id == item["set_id"], ProductRelation.item_product_id == item["item_id"])) is None:
                        session.add(ProductRelation(set_product_id=item["set_id"], item_product_id=item["item_id"], source=str(item.get("source", "manual")),
                                                    confirmed_by=batch.owner_id, confirmed_at=utcnow()))
                        session.flush()
                    affected += [item["set_id"], item["item_id"]]
                    continue
                # 另一头还处于作废状态：把这条关系转记到它的历史版本里，等它恢复时再接回。
                for end in ends:
                    if end is None or end.id == product_id or end.status != "voided":
                        continue
                    other = session.scalar(select(HistoryVersion).where(HistoryVersion.file_id == end.file_id, HistoryVersion.reason == "voided").order_by(HistoryVersion.id.desc()))
                    if other is not None:
                        snapshot = dict(other.snapshot) if isinstance(other.snapshot, dict) else {}
                        kept = list(snapshot.get("relations", []))
                        if item not in kept:
                            other.snapshot = {**snapshot, "relations": [*kept, item]}
        return affected

    def _apply_restore_project(self, session: Session, batch: Batch, project: Project, stats: dict[int, int]) -> list[int]:
        now = utcnow().isoformat()
        before = {"status": project.status, "current_path": project.current_path}
        project.status, project.current_path = "active", str(batch.payload["plan"]["target"])
        project.lifecycle = {**project.lifecycle, "restored_at": now}
        affected = []
        # 只恢复随项目一起作废的商品；此前单独作废的仍保持作废。
        for product in session.scalars(select(Product).where(Product.project_id == project.id, Product.status == "voided")):
            if product.lifecycle.get("voided_by") == "project":
                product.status = "active"
                product.lifecycle = {**product.lifecycle, "restored_at": now}
                affected.append(product.id)
        self._log(session, batch, "整项目恢复", "project", project.id, before, {"status": "active", "current_path": project.current_path, "products": len(affected)})
        return affected

    def _apply_move(self, session: Session, batch: Batch, project: Project, stats: dict[int, int]) -> list[int]:
        plan = batch.payload["plan"]
        before = {"current_path": project.current_path, "won": project.won, "company_id": project.company_id, "industry_id": project.industry_id}
        project.current_path, project.won = str(plan["target"]), bool(plan["won"])
        project.company_id, project.industry_id = plan["company_id"], plan["industry_id"]
        self._log(session, batch, "移动项目", "project", project.id, before,
                  {"current_path": project.current_path, "won": project.won, "company_id": project.company_id, "industry_id": project.industry_id,
                   "reason": batch.payload["params"]["reason"]})
        return []

    def _apply_purge(self, session: Session, batch: Batch, project: Project, stats: dict[int, int]) -> list[int]:
        params = batch.payload["params"]
        now = utcnow().isoformat()
        mark = {"deleted_at": now, "delete_reason": params["reason"], "delete_batch_id": batch.id,
                **({"exception_reason": params["exception_reason"]} if params.get("skipped_cooling") else {})}
        affected = []
        for file_id in params["file_ids"]:
            file = session.get(CatalogFile, file_id)
            # 记录保留原路径、指纹，只把状态改为已删除。
            file.status, file.lifecycle = "deleted", {**file.lifecycle, **mark}
        for product_id in params["product_ids"]:
            product = session.get(Product, product_id)
            product.status, product.lifecycle = "deleted", {**product.lifecycle, **mark}
            affected.append(product.id)
        if params.get("project"):
            project.status, project.lifecycle = "deleted", {**project.lifecycle, **mark}
        OplogService(session).write(batch.final_reviewer_id, "final_reviewer", "物理删除", "project", str(project.id),
            {"files": params["listing"]}, {"reason": params["reason"], "skipped_cooling": bool(params.get("skipped_cooling")),
                                           "exception_reason": params.get("exception_reason", ""), "deleted": len(params["listing"]), "whole_project": bool(params.get("project"))},
            "成功", "入库后操作任务")
        return affected

    def _apply_supplement(self, session: Session, batch: Batch, project: Project, stats: dict[int, int]) -> list[int]:
        plan = batch.payload["plan"]
        now = utcnow()
        kinds = batch.payload.get("fingerprint_kinds", {})
        owners = dict(session.execute(select(Product.file_id, Product.id).where(Product.project_id == project.id)).all())
        for item in plan["replaced"]:
            old = session.get(CatalogFile, item["old_file_id"])
            product = session.get(Product, owners[old.id]) if old.id in owners else None
            snapshot: dict[str, object] | None = None
            if product is not None:
                # 被替换商品的旧标签保存为历史。
                snapshot = {"display_name": product.display_name, "free_text": product.free_text, "tags": [
                    {"field_key": tag.field_key, "value": tag.value, "terms": list(tag.terms), "source": tag.source, "ai_value": tag.ai_value}
                    for tag in session.scalars(select(ProductTag).where(ProductTag.product_id == product.id).order_by(ProductTag.field_key))]}
            session.add(HistoryVersion(project_id=project.id, file_id=old.id, product_id=product.id if product else None, old_fingerprint=old.fingerprint,
                old_rel_path=old.rel_path, new_rel_path=str(item["history"]), reason="replaced", batch_id=batch.id, expected_missing=False, snapshot=snapshot))
            self._log(session, batch, "文件被替换", "file", old.id, {"rel_path": old.rel_path, "status": "active", "fingerprint": old.fingerprint},
                      {"rel_path": item["history"], "status": "replaced"})
            old.rel_path, old.status = str(item["history"]), "replaced"
            old.lifecycle = {**old.lifecycle, "replaced_at": now.isoformat()}
        session.flush()
        created: dict[int, CatalogFile] = {}
        for item in plan["imports"]:
            source = session.get(BatchFile, item["batch_file_id"])
            created[source.id] = CatalogFile(project_id=project.id, rel_path=str(item["to"]), size=source.size, mtime=stats.get(source.id, source.mtime),
                fingerprint=source.fingerprint or "", fingerprint_kind=str(kinds.get(str(source.id), "full")), category=category_of(str(item["to"])),
                status="active", first_batch_id=batch.id)
            session.add(created[source.id])
        session.flush()
        model = self.ingest.ai_settings.effective().model_vision or ("fake-vision" if self.auth.config.mode == "dev" else "")
        existing_archives = {row.archive_rel_path.casefold() for row in session.scalars(select(Product).where(Product.project_id == project.id)) if row.status != "deleted"}
        targets: dict[int, Product] = {}
        rows = [row for row in active_products(session, batch.id) if row.batch_file_id in created]
        for row in sorted(rows, key=lambda row: row.product_code or ""):
            file = created[row.batch_file_id]
            if row.existing_product_id is not None:
                product = session.get(Product, row.existing_product_id)
                before = {"file_id": product.file_id, "free_text": product.free_text}
                product.file_id, product.free_text, product.vision_model = file.id, row.free_text, model
                session.execute(delete(ProductTag).where(ProductTag.product_id == product.id))
                self._log(session, batch, "替换主图", "product", product.id, before, {"file_id": file.id, "product_code": product.product_code})
            else:
                name = file.rel_path.rsplit("/", 1)[-1]
                plain = archive.archive_name(row.kind, name, False)
                path = archive.archive_name(row.kind, name, plain.casefold() in existing_archives)
                existing_archives.add(path.casefold())
                product = Product(product_code=row.product_code, project_id=project.id, kind=row.kind, display_name=row.display_name, file_id=file.id,
                                  archive_rel_path=path, status="active", free_text=row.free_text, vision_model=model, ingested_at=now)
                session.add(product)
                session.flush()
                self._log(session, batch, "补充新商品", "product", product.id, None, {"product_code": product.product_code, "rel_path": file.rel_path})
            session.flush()
            for key, tag in row.tags.items():
                if isinstance(tag, dict):
                    session.add(ProductTag(product_id=product.id, field_key=key, value=str(tag.get("value", "")), terms=[str(term) for term in tag.get("terms") or []],
                        source=str(tag.get("source", "ai")), ai_value=str(tag.get("ai_value", "")), flag=None))
            targets[row.id] = product
        session.flush()
        for row in rows:
            if row.kind == "set" and row.existing_product_id is None:
                for item in row.relation_items:
                    if isinstance(item, dict) and item.get("batch_product_id") in targets:
                        session.add(ProductRelation(set_product_id=targets[row.id].id, item_product_id=targets[item["batch_product_id"]].id,
                                                    source=str(item.get("source", "ai")), confirmed_by=batch.owner_id, confirmed_at=now))
        self._log(session, batch, "补充落盘", "project", project.id, None,
                  {"imported": len(created), "replaced": len(plan["replaced"]), "new_products": sum(row.existing_product_id is None for row in rows)})
        return [product.id for product in targets.values()]

    # ---- 第 4 步：更新档案 ----
    def _archive(self, batch_id: int, fs: NasFs) -> None:
        with self.auth.lock, self.auth.database.session() as session:
            project = session.get(Project, session.get(Batch, batch_id).project_id)
            if project.status == "deleted":
                return
            root = archive.project_root(self.paths, project)
            expected = archive.documents(archive.from_catalog(session, project))
        archive.apply_documents(fs, root, expected)

    # ---- 第 5 步：完成 ----
    def _finish(self, batch_id: int, fs: NasFs) -> None:
        with self.auth.lock, self.auth.database.session() as session:
            batch = session.get(Batch, batch_id)
            if batch.status == DONE:
                return
            pairs: list[tuple[int, int]] = []
            purged = [int(value) for value in batch.payload.get("params", {}).get("product_ids", [])] if batch.kind == "purge" else []
            if batch.kind == "supplement":
                pairs = [(row.batch_file_id, product_id) for row, product_id in session.execute(select(BatchProduct, Product.id).join(
                    Product, Product.product_code == BatchProduct.product_code).where(BatchProduct.batch_id == batch.id))]
        # 新图和被替换主图的缩略图缓存迁到商品目录；重复执行时已迁移的跳过。
        for file_id, product_id in pairs:
            source, destination = self.thumb_root / "batch" / str(batch_id) / str(file_id), self.thumb_root / "product" / str(product_id)
            if source.is_dir():
                shutil.rmtree(destination, ignore_errors=True)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
        shutil.rmtree(self.thumb_root / "batch" / str(batch_id), ignore_errors=True)
        # 物理删除的商品连缩略图缓存一起清掉。
        for product_id in purged:
            shutil.rmtree(self.thumb_root / "product" / str(product_id), ignore_errors=True)
        with self.auth.lock, self.auth.database.session() as session:
            batch = session.get(Batch, batch_id)
            project = session.get(Project, batch.project_id)
            if batch.kind == "supplement":
                ensure_link(session, "project", project.id)
                for product_id in session.scalars(select(Product.id).where(Product.project_id == project.id)):
                    ensure_link(session, "product", product_id)
                for file_id in session.scalars(select(CatalogFile.id).where(CatalogFile.project_id == project.id)):
                    ensure_link(session, "file", file_id)
            # 标签、状态变了的商品重算或删除向量。
            for product_id in batch.payload.get("affected", []):
                self.search.enqueue(session, product_id)
            batch.status, batch.ingest_error = DONE, None
            self._log(session, batch, f"{kind_label(batch)}完成", "batch", batch.id, {"status": RUNNING}, {"status": DONE, "project_id": project.id})
            self.ingest.notify.emit(session, "postop_done", batch)
