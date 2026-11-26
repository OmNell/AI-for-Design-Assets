from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.database import Database
from app.db.models import Job, Oplog, User, UserSession, utcnow
from app.errors import AppError
from app.services.jobs import JobContext, JobFailure, JobQueue
from app.services.nas import Access, LocalFs, Region, SessionPool


def make_pool(tmp_path: Path) -> SessionPool:
    return SessionPool(lambda u, p: LocalFs(tmp_path / 'nas', {Region.TEMP: Access.WRITE}),
                       lambda: LocalFs(tmp_path / 'nas', {}))


def job_state(database: Database, job_id: int) -> Job:
    with database.session() as session:
        return session.get(Job, job_id)


def test_concurrent_claims_execute_each_job_once(database: Database, tmp_path: Path) -> None:
    seen: list[int] = []
    seen_lock = threading.Lock()
    with make_pool(tmp_path) as pool:
        queue = JobQueue(database, pool, workers=8)
        def handler(context: JobContext) -> None:
            with seen_lock:
                seen.append(context.job.id)
        queue.register('thumb', handler, concurrency=8)
        ids = [queue.enqueue('thumb', {'index': index}).id for index in range(40)]
        # 同一实例的并发工作线程与另一实例竞争，均必须经条件更新获得执行权。
        competitor = JobQueue(database, pool, workers=4)
        competitor.register('thumb', handler, concurrency=4)
        with ThreadPoolExecutor(max_workers=12) as workers:
            for _ in range(12):
                futures = [workers.submit(queue.run_once if index % 2 else competitor.run_once) for index in range(12)]
                for result in futures:
                    result.result()
        assert sorted(seen) == ids
        assert len(seen) == len(set(seen))
        assert all(job_state(database, job_id).status == 'done' for job_id in ids)


@pytest.mark.parametrize('kind,expected', [('thumb', 2), ('ai_tag_product', 4)])
def test_retry_budgets_and_safe_error(database: Database, tmp_path: Path, kind: str, expected: int) -> None:
    calls: list[int] = []
    with make_pool(tmp_path) as pool:
        queue = JobQueue(database, pool, retry_delay_seconds=0)
        def failing(context: JobContext) -> None:
            calls.append(context.job.id)
            raise RuntimeError('不应落库的密码和请求原文')
        queue.register(kind, failing)
        job = queue.enqueue(kind, {})
        for _ in range(expected + 2):
            queue.run_once()
        saved = job_state(database, job.id)
        assert len(calls) == expected and saved.attempts == expected and saved.status == 'failed'
        assert '密码' not in saved.error
        with database.session() as session:
            assert session.scalar(select(Oplog.id).where(Oplog.action == '任务执行失败'))


def test_permanent_failure_does_not_retry_and_callback_tracks_transitions(database: Database, tmp_path: Path) -> None:
    states: list[str] = []
    with make_pool(tmp_path) as pool:
        queue = JobQueue(database, pool, retry_delay_seconds=0)
        def broken(context: JobContext) -> None:
            raise JobFailure('图片损坏或无法解码', retryable=False)
        queue.register('thumb', broken, state_callback=lambda session, job: states.append(job.status))
        job = queue.enqueue('thumb', {})
        assert queue.run_once()
        assert not queue.run_once()
        assert states == ['queued', 'running', 'failed']
        assert job_state(database, job.id).error == '图片损坏或无法解码'


def test_retry_then_success(database: Database, tmp_path: Path) -> None:
    calls = [0]
    with make_pool(tmp_path) as pool:
        queue = JobQueue(database, pool, retry_delay_seconds=0)
        def handler(context: JobContext) -> None:
            calls[0] += 1
            if calls[0] == 1:
                raise JobFailure('临时失败')
        queue.register('thumb', handler)
        job = queue.enqueue('thumb', {})
        assert queue.run_once() and queue.run_once()
        assert job_state(database, job.id).status == 'done'
        assert job_state(database, job.id).attempts == 2


def test_type_concurrency_limits_and_global_workers(database: Database, tmp_path: Path) -> None:
    entered: dict[str, int] = {'thumb': 0, 'backup': 0}
    active: dict[str, int] = {'thumb': 0, 'backup': 0}
    peak: dict[str, int] = {'thumb': 0, 'backup': 0}
    guard = threading.Lock()
    ready = threading.Event()
    release = threading.Event()
    with make_pool(tmp_path) as pool:
        queue = JobQueue(database, pool, workers=4, poll_seconds=.005)
        def handler(context: JobContext) -> None:
            with guard:
                entered[context.job.kind] += 1
                active[context.job.kind] += 1
                peak[context.job.kind] = max(peak[context.job.kind], active[context.job.kind])
                if entered['thumb'] == 2 and entered['backup'] == 1:
                    ready.set()
            assert release.wait(3)
            with guard:
                active[context.job.kind] -= 1
        queue.register('thumb', handler)
        queue.register('backup', handler)
        for _ in range(4):
            queue.enqueue('thumb', {})
            queue.enqueue('backup', {})
        queue.start()
        try:
            assert ready.wait(2), '不同类型应独立获得并发配额'
            with guard:
                assert entered == {'thumb': 2, 'backup': 1}
                assert sum(active.values()) <= 4
            release.set()
            assert queue.wait_idle(3)
            assert peak == {'thumb': 2, 'backup': 1}
        finally:
            release.set()
            queue.shutdown()


def test_startup_recovery_is_type_specific_and_keeps_retry_budget(database: Database, tmp_path: Path) -> None:
    with make_pool(tmp_path) as pool:
        queue = JobQueue(database, pool)
        queue.register('thumb', lambda ctx: None)
        queue.register('ingest', lambda ctx: None)
        with database.session() as session:
            thumb = Job(kind='thumb', payload={}, status='running', attempts=1)
            ingest = Job(kind='ingest', payload={}, status='running', attempts=1)
            ai = Job(kind='ai_tag_product', payload={}, status='running', attempts=2)
            session.add_all([thumb, ingest, ai]); session.flush()
        queue.recover()
        assert job_state(database, thumb.id).status == 'queued'
        assert job_state(database, thumb.id).attempts == 0
        assert job_state(database, ai.id).status == 'queued'
        assert job_state(database, ai.id).attempts == 1
        assert job_state(database, ingest.id).status == 'failed'
        assert '服务重启' in job_state(database, ingest.id).error
        assert queue.run_once()
        assert job_state(database, thumb.id).status == 'done'


def test_missing_session_fails_without_running_handler_or_retry(database: Database, tmp_path: Path) -> None:
    ran: list[int] = []
    with database.session() as session:
        user = User(username='one', roles=['uploader']); session.add(user); session.flush()
    with make_pool(tmp_path) as pool:
        queue = JobQueue(database, pool)
        queue.register('thumb', lambda ctx: ran.append(ctx.job.id))
        job = queue.enqueue('thumb', {}, run_as_user_id=user.id)
        assert queue.run_once()
        assert job_state(database, job.id).status == 'failed'
        assert job_state(database, job.id).error == '操作人会话已失效'
        assert job_state(database, job.id).attempts == 1 and not ran


def test_handler_receives_only_own_fs(database: Database, tmp_path: Path) -> None:
    with database.session() as session:
        user = User(username='one', roles=['uploader']); session.add(user); session.flush()
        first = UserSession(user_id=user.id, token_hash="first", client_host="设备甲", expires_at=utcnow() + timedelta(hours=1))
        second = UserSession(user_id=user.id, token_hash="second", client_host="设备乙", expires_at=utcnow() + timedelta(hours=1))
        session.add_all([first, second]); session.flush()
    with make_pool(tmp_path) as pool:
        pool.open(first.id, 'one', 'fake')
        own = pool.open(second.id, 'one', 'fake')
        queue = JobQueue(database, pool)
        def handler(context: JobContext) -> None:
            assert context.fs is own
            with context.fs.open_write('工作台临时区/own') as stream:
                stream.write(b'own')
        queue.register('thumb', handler)
        job = queue.enqueue('thumb', {}, user.id, run_as_session_id=second.id)
        assert queue.run_once()
        assert job_state(database, job.id).status == 'done'
        with own.open_read('工作台临时区/own') as stream:
            assert stream.read() == b'own'


def test_scheduled_and_transactional_enqueue(database: Database, tmp_path: Path) -> None:
    seen: list[int] = []
    with make_pool(tmp_path) as pool:
        queue = JobQueue(database, pool)
        queue.register('thumb', lambda ctx: seen.append(ctx.job.id))
        later = queue.enqueue('thumb', {}, scheduled_at=utcnow() + timedelta(hours=1))
        assert not queue.run_once()
        with pytest.raises(ValueError), database.session() as session:
            queue.enqueue('thumb', {}, session=session)
            raise ValueError('事务回滚')
        with database.session() as session:
            assert list(session.scalars(select(Job.id))) == [later.id]
        assert not seen


def test_admin_retry_checks_current_role_and_is_atomic(database: Database, tmp_path: Path) -> None:
    with database.session() as session:
        admin = User(username='admin', roles=['admin'])
        uploader = User(username='one', roles=['uploader'])
        session.add_all([admin, uploader]); session.flush()
    with make_pool(tmp_path) as pool:
        queue = JobQueue(database, pool, retry_delay_seconds=0)
        def broken(ctx: JobContext) -> None:
            raise JobFailure('坏图片', False)
        queue.register('thumb', broken)
        job = queue.enqueue('thumb', {})
        queue.run_once()
        with pytest.raises(AppError) as denial:
            queue.retry(job.id, uploader, '本机')
        assert denial.value.status_code == 403
        assert queue.list_jobs(admin)[0].id == job.id
        restored = queue.retry(job.id, admin, '本机')
        assert restored.status == 'queued' and restored.attempts == 0 and restored.error is None
        with pytest.raises(AppError) as conflict:
            queue.retry(job.id, admin, '本机')
        assert conflict.value.status_code == 409
        with database.session() as session:
            session.get(User, admin.id).roles = ['uploader']
        with pytest.raises(AppError):
            queue.list_jobs(admin)


def test_exclusive_recovery_drains_older_jobs_and_defers_new_writes(database, tmp_path):
    previous_running, release_previous = threading.Event(), threading.Event()
    recovery_running, release_recovery = threading.Event(), threading.Event()
    next_running = threading.Event()
    with make_pool(tmp_path) as pool:
        queue = JobQueue(database, pool, workers=3, poll_seconds=.005)

        def ordinary(context):
            if context.job.payload["prior"]:
                previous_running.set()
                assert release_previous.wait(3)
            else:
                next_running.set()

        def recover(context):
            recovery_running.set()
            assert release_recovery.wait(3)

        queue.register("publish", ordinary, concurrency=3)
        queue.register("asset_recovery_apply", recover, exclusive=True)
        queue.enqueue("publish", {"prior": True})
        queue.start()
        try:
            assert previous_running.wait(2)
            queue.enqueue("asset_recovery_apply", {})
            queue.enqueue("publish", {"prior": False})
            assert not recovery_running.wait(.08)
            assert not next_running.is_set()
            release_previous.set()
            assert recovery_running.wait(2)
            assert not next_running.wait(.08)
            release_recovery.set()
            assert next_running.wait(2)
            assert queue.wait_idle(2)
        finally:
            release_previous.set()
            release_recovery.set()
            queue.shutdown()
