from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.database import Database
from app.db.models import Batch, BatchFile, BatchProduct, CompanyIndustryMap, Job, User
from app.services.ai.orchestrator import AiService
from app.services.ai.settings import AiSettingsService
from app.services.auth.backends import FakeAuthBackend
from app.services.auth.service import AuthService
from app.services.crypto import SecretCipher
from app.services.jobs import JobContext, JobQueue
from app.services.nas.factory import create_session_pool
from app.services.thumbs import ThumbService


@pytest.fixture
def service(database: Database, settings: Settings, tmp_path: Path) -> Iterator[tuple[AiService, Batch, User]]:
    settings.paths.dev_nas_dir = tmp_path / 'nas'
    with create_session_pool(settings) as pool:
        auth = AuthService(database, pool, FakeAuthBackend([]), settings)
        cipher = SecretCipher(settings.paths.data_dir)
        queue = JobQueue(database, pool, workers=1, auth_lock=auth.lock)
        thumbs = ThumbService(auth, queue)
        ai = AiService(auth, queue, AiSettingsService(auth, cipher), cipher, thumbs)
        def thumbnail_changed(session: Session, job: Job) -> None:
            thumbs.on_job_state(session, job)
            ai.thumb_changed(session, job)
        queue.register('thumb', thumbs.handle, state_callback=thumbnail_changed)
        queue.register('ai_tag_batch', ai.handle_batch, state_callback=ai.job_changed)
        queue.register('ai_tag_product', ai.handle_product, retries=0, state_callback=ai.job_changed)
        with database.session() as session:
            user = User(username='one', roles=['uploader'])
            session.add(user); session.flush()
            batch = Batch(batch_no='B1', project_code='P', kind='ingest', status='已暂存', uploader_id=user.id, owner_id=user.id,
                temp_path='B1/P_企业_项目', won=True, payload={'folder_name': 'P_企业_项目'})
            session.add(batch); session.flush()
            session.add(BatchFile(batch_id=batch.id, rel_path='单品/主图.jpg', size=1, mtime=1, bytes_received=1, upload_status='done', fingerprint='a' * 64, fingerprint_kind='full'))
        try:
            yield ai, batch, user
        finally:
            ai.shutdown(); queue.shutdown(); ai.close_clients()


def test_orchestration_returns_while_thumb_pending_and_failure_advances(service: tuple[AiService, Batch, User]) -> None:
    ai, batch, user = service
    with ai.auth.lock, ai.auth.database.session() as session:
        ai.start_batch(session, session.get(Batch, batch.id), user, '本机')
    with ai.auth.database.session() as session:
        job = session.scalar(select(Job).where(Job.kind == 'ai_tag_batch'))
    ai.handle_batch(JobContext(job, None))
    with ai.auth.lock, ai.auth.database.session() as session:
        assert session.scalar(select(BatchProduct)).ai_status == 'pending'
        assert session.scalar(select(Job).where(Job.kind == 'ai_tag_product')) is None
        # 不占用唯一工作线程等待子任务，缩略图终态由回调推进。
        thumb = session.scalar(select(Job).where(Job.kind == 'thumb'))
        thumb.status, thumb.error = 'failed', '图片损坏或无法解码'
        ai.thumbs.on_job_state(session, thumb)
        ai.thumb_changed(session, thumb)
    with ai.auth.database.session() as session:
        assert session.get(Batch, batch.id).status == 'AI 处理失败'
        assert session.scalar(select(BatchProduct)).ai_error == '图片损坏或无法解码'


def test_whole_batch_retry_fences_old_generation(service: tuple[AiService, Batch, User]) -> None:
    ai, batch, user = service
    with ai.auth.lock, ai.auth.database.session() as session:
        ai.start_batch(session, session.get(Batch, batch.id), user, '本机')
    with ai.auth.database.session() as session:
        old_job = session.scalar(select(Job).where(Job.kind == 'ai_tag_batch'))
    with ai.auth.lock, ai.auth.database.session() as session:
        current = session.get(Batch, batch.id)
        ai.start_batch(session, current, user, '本机')
        new_run = current.payload['ai_run']
    ai.handle_batch(JobContext(old_job, None))
    with ai.auth.database.session() as session:
        assert session.get(Batch, batch.id).payload['ai_run'] == new_run
        assert session.scalar(select(BatchProduct)) is None
        assert session.get(Job, old_job.id).status == 'failed'


def test_ambiguous_company_alias_does_not_invent_location(service: tuple[AiService, Batch, User]) -> None:
    ai, batch, _ = service
    with ai.auth.database.session() as session:
        session.add_all([CompanyIndustryMap(company_name=name, aliases=['企业'], company_seq=seq, industry_name='行业', industry_seq=1) for seq, name in enumerate(['甲企业', '乙企业'], 1)])
    with ai.auth.database.session() as session:
        assert ai._recommend(session, session.get(Batch, batch.id)) is None
