"""巡检、备份、清理、审计和参数管理。"""
from collections.abc import Iterator
from datetime import date
from typing import Annotated, BinaryIO, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from app.api.dependencies import client_host, require_login_context, require_role
from app.db.models import User
from app.errors import AppError
from app.schemas.auth import Role
from app.schemas.jobs import JobResponse
from app.schemas.maintenance import (BackupView, CleanupConfirm, CleanupPreview, CleanupTarget, ErrorItem, FindingView,
    LogFilter, LogPage, RepairCandidate, RepairRequest, RepairResult, RunView, SearchLogPage, ThumbRebuild)
from app.services.auth.service import LoginContext
from app.services.maintenance.settings import SystemJob, SystemParameters

router = APIRouter(prefix="/api")
admin = require_role(Role.ADMIN)


@router.get("/settings/system", response_model=SystemParameters)
def settings(request: Request, actor: User = Depends(admin)) -> SystemParameters:
    return request.app.state.system.parameters


@router.put("/settings/system", response_model=SystemParameters)
def update_settings(body: SystemParameters, request: Request, actor: User = Depends(admin)) -> SystemParameters:
    return request.app.state.system.save(body, actor, client_host(request))


@router.post("/admin/maintenance/{kind}/run", response_model=JobResponse, status_code=202)
def trigger(kind: SystemJob, request: Request, actor: User = Depends(admin)) -> JobResponse:
    return JobResponse.model_validate(request.app.state.system.trigger(kind, actor, client_host(request)))


@router.get("/admin/inspection/runs", response_model=list[RunView])
def runs(request: Request, page: Annotated[int, Query(ge=1)] = 1, start_date: date | None = None,
         end_date: date | None = None, actor: User = Depends(admin)) -> list[RunView]:
    return request.app.state.maintenance.runs(actor, page, start_date, end_date)


@router.get("/admin/inspection/findings", response_model=list[FindingView])
def findings(request: Request, run_id: int | None = None, kind: str | None = None, project: int | None = None,
             status: Literal["all", "open", "ack", "repaired"] = "all", page: Annotated[int, Query(ge=1)] = 1,
             start_date: date | None = None, end_date: date | None = None,
             actor: User = Depends(admin)) -> list[FindingView]:
    return request.app.state.maintenance.findings(actor, run_id, kind, project, status, page, start_date, end_date)


@router.get("/admin/inspection/findings/{identifier}", response_model=FindingView)
def finding(identifier: int, request: Request, actor: User = Depends(admin)) -> FindingView:
    return request.app.state.maintenance.finding(actor, identifier)


@router.post("/admin/inspection/findings/{identifier}/ack", response_model=FindingView)
def ack(identifier: int, request: Request, actor: User = Depends(admin)) -> FindingView:
    return request.app.state.maintenance.ack(actor, identifier, client_host(request))


@router.get("/admin/backups", response_model=list[BackupView])
def backups(request: Request, actor: User = Depends(admin)) -> list[BackupView]:
    return request.app.state.maintenance.backups(actor)


def stream_file(stream: BinaryIO) -> Iterator[bytes]:
    with stream:
        while chunk := stream.read(1024 * 1024):
            yield chunk


@router.get("/admin/backups/{identifier}/download")
def download_backup(identifier: int, request: Request, actor: User = Depends(admin)) -> StreamingResponse:
    row, stream = request.app.state.maintenance.download_backup(actor, identifier, client_host(request))
    return StreamingResponse(stream_file(stream), media_type="application/zip", background=BackgroundTask(stream.close),
        headers={"Content-Disposition": f'attachment; filename="{row.name}"', "Content-Length": str(row.size),
                 "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/admin/temp-cleanup", response_model=list[CleanupTarget])
def cleanup(request: Request, login: LoginContext = Depends(require_login_context)) -> list[CleanupTarget]:
    return request.app.state.cleanup.listing(login)


@router.post("/admin/temp-cleanup/preview/{key}", response_model=CleanupPreview)
def preview_cleanup(key: str, request: Request, login: LoginContext = Depends(require_login_context)) -> CleanupPreview:
    return CleanupPreview.model_validate(request.app.state.cleanup.preview(key, login, client_host(request)))


@router.post("/admin/temp-cleanup/execute/{identifier}", response_model=CleanupPreview)
def execute_cleanup(identifier: int, body: CleanupConfirm, request: Request,
                    login: LoginContext = Depends(require_login_context)) -> CleanupPreview:
    return CleanupPreview.model_validate(request.app.state.cleanup.execute(identifier, body.confirmation, login, client_host(request)))


@router.get("/admin/oplog", response_model=LogPage)
def logs(filters: Annotated[LogFilter, Query()], request: Request, actor: User = Depends(admin)) -> LogPage:
    return request.app.state.maintenance.logs(actor, filters)


@router.get("/admin/oplog/export")
def export_logs(filters: Annotated[LogFilter, Query()], request: Request, actor: User = Depends(admin)) -> Response:
    return Response(request.app.state.maintenance.logs(actor, filters, export=True), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="operations.csv"', "Cache-Control": "no-store"})


@router.get("/admin/search-logs", response_model=SearchLogPage)
def search_logs(filters: Annotated[LogFilter, Query()], request: Request, actor: User = Depends(admin)) -> SearchLogPage:
    return request.app.state.maintenance.searches(actor, filters)


@router.get("/admin/search-logs/export")
def export_search_logs(filters: Annotated[LogFilter, Query()], request: Request, actor: User = Depends(admin)) -> Response:
    return Response(request.app.state.maintenance.searches(actor, filters, export=True), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="unclicked-searches.csv"', "Cache-Control": "no-store"})


@router.get("/admin/maintenance/errors", response_model=list[ErrorItem])
def errors(request: Request, actor: User = Depends(admin)) -> list[ErrorItem]:
    return request.app.state.maintenance.errors(actor)


@router.get("/archive/repairs", response_model=list[RepairCandidate])
def repairs(request: Request, login: LoginContext = Depends(require_login_context)) -> list[RepairCandidate]:
    return request.app.state.maintenance.repairs(login)


@router.post("/archive/repairs", response_model=list[RepairResult], status_code=201)
def repair_many(body: RepairRequest, request: Request, login: LoginContext = Depends(require_login_context)) -> list[RepairResult]:
    return request.app.state.maintenance.request_repairs(body.project_ids, login, client_host(request))


@router.post("/projects/{project_id}/archive/repair", response_model=RepairResult, status_code=201)
def repair(project_id: int, request: Request, login: LoginContext = Depends(require_login_context)) -> RepairResult:
    result = request.app.state.maintenance.request_repairs([project_id], login, client_host(request))[0]
    if result.error:
        raise AppError("repair_unavailable", result.error, 409)
    return result


@router.post("/admin/thumbnails/rebuild", response_model=list[JobResponse], status_code=202)
def rebuild(body: ThumbRebuild, request: Request, login: LoginContext = Depends(require_login_context)) -> list[JobResponse]:
    return request.app.state.maintenance.rebuild_thumbs(body.product_ids, login, client_host(request))
