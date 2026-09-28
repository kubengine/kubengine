from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import scoped_session, sessionmaker

import core.orm.engine as engine_module
from core.command import CommandResult
from core.config import Application
from core.orm.engine import Base
from core.orm.image_import import (
    ImageImportLease,
    ImageImportLeaseLost,
    ImageImportTask,
    claim_next_image_import_task,
    create_image_import_task,
    find_image_import_task,
    renew_image_import_lease,
    replace_image_import_items,
    requeue_image_import_task,
    update_image_import_item,
    update_image_import_task,
)
from web.api import artifacts


@pytest.fixture
def image_queue(monkeypatch, tmp_path):
    test_engine = create_engine(f"sqlite:///{tmp_path / 'queue.db'}")
    test_sessions = scoped_session(sessionmaker(bind=test_engine))
    monkeypatch.setattr(engine_module, "engine", test_engine)
    monkeypatch.setattr(engine_module, "SessionLocal", test_sessions)
    monkeypatch.setattr(Application, "ROOT_DIR", str(tmp_path))
    Base.metadata.create_all(bind=test_engine)
    archive = tmp_path / "images.tar"
    archive.write_bytes(b"isolated archive fixture")
    monkeypatch.setattr(artifacts, "_inspect_image_archive", lambda path: {})
    monkeypatch.setattr(
        artifacts, "HarborClient",
        lambda: SimpleNamespace(create_project=lambda *args, **kwargs: True),
    )
    yield test_engine, archive
    test_sessions.remove()
    test_engine.dispose()


def queue_task(archive):
    task = create_image_import_task(archive.name, "application/x-tar", str(archive))
    task_id = int(task["task_id"])
    update_image_import_task(task_id, status="pending")
    return task_id


def claim(owner="worker-a"):
    result = claim_next_image_import_task(owner, 60)
    assert result is not None
    return result, ImageImportLease(owner, result["attempt_count"])


def expire(test_engine, task_id):
    with test_engine.begin() as connection:
        connection.execute(
            update(ImageImportTask).where(ImageImportTask.task_id == task_id).values(
                lease_expires_at=datetime.now() - timedelta(seconds=1)
            )
        )


def seed_items(task_id, lease, refs):
    replace_image_import_items(
        task_id, [artifacts._parse_image_ref(ref) for ref in refs], lease=lease
    )


def mock_commands(monkeypatch, refs, on_push=None):
    pushed = []

    def run(argv, **kwargs):
        assert isinstance(argv, list)
        if argv[0] == "ctr" and argv[3:5] == ["i", "ls"]:
            return CommandResult(0, "\n".join(refs), "")
        if argv[0] == "ctr" and argv[3:5] == ["i", "push"]:
            pushed.append(argv[-1])
            if on_push:
                on_push(argv[-1])
        return CommandResult(0, "", "")

    monkeypatch.setattr(artifacts, "execute_command", run)
    return pushed


def test_expired_image_task_lease_can_be_reclaimed(image_queue):
    test_engine, archive = image_queue
    task = create_image_import_task(archive.name, "application/x-tar", str(archive))
    assert claim_next_image_import_task("worker-a", 60) is None
    task_id = int(task["task_id"])
    update_image_import_task(task_id, status="pending")
    claimed, lease = claim()
    assert claimed["attempt_count"] == 1
    assert not renew_image_import_lease(task_id, "worker-b", 60, lease.attempt)
    expire(test_engine, task_id)
    assert not renew_image_import_lease(task_id, lease.owner, 60, lease.attempt)
    reclaimed, _ = claim("worker-b")
    assert reclaimed["task_id"] == task_id
    assert reclaimed["attempt_count"] == 2


def test_concurrent_claims_have_one_winner(image_queue):
    _, archive = image_queue
    task_id = queue_task(archive)
    ready = threading.Barrier(2, timeout=5)

    def contender(owner):
        ready.wait()
        return claim_next_image_import_task(owner, 60)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(contender, ["worker-a", "worker-b"]))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0]["task_id"] == task_id
    assert winners[0]["attempt_count"] == 1


def test_stale_attempt_cannot_modify_new_owner_or_items(image_queue):
    test_engine, archive = image_queue
    task_id = queue_task(archive)
    _, old = claim("same-worker")
    ref = "example.test/library/app:latest"
    seed_items(task_id, old, [ref])
    expire(test_engine, task_id)
    _, current = claim("same-worker")
    assert current.attempt == old.attempt + 1
    update_image_import_item(task_id, ref, "success", "completed", lease=current)
    assert not renew_image_import_lease(task_id, old.owner, 60, old.attempt)
    for mutation in (
        lambda: update_image_import_task(task_id, lease=old, status="success", lease_owner=None),
        lambda: update_image_import_item(task_id, ref, "failed", "stale", lease=old),
        lambda: replace_image_import_items(task_id, [], lease=old),
        lambda: update_image_import_task(task_id, status="failed"),
    ):
        with pytest.raises(ImageImportLeaseLost):
            mutation()
    record = find_image_import_task(task_id)
    assert record["status"] == "processing"
    assert record["attempt_count"] == current.attempt
    assert record["items"][0]["status"] == "success"
    update_image_import_task(task_id, lease=current, status="success", lease_owner=None)
    with pytest.raises(ImageImportLeaseLost):
        update_image_import_item(task_id, ref, "failed", "stale", lease=old)
    assert find_image_import_task(task_id)["status"] == "success"


def test_expired_attempt_cannot_write_even_before_reclaim(image_queue):
    test_engine, archive = image_queue
    task_id = queue_task(archive)
    _, lease = claim()
    ref = "example.test/app:one"
    seed_items(task_id, lease, [ref])
    expire(test_engine, task_id)
    with pytest.raises(ImageImportLeaseLost):
        update_image_import_task(task_id, lease=lease, status="success")
    with pytest.raises(ImageImportLeaseLost):
        update_image_import_item(task_id, ref, "success", "completed", lease=lease)


def test_interrupted_retry_resumes_processing_item_and_preserves_success(image_queue, monkeypatch):
    test_engine, archive = image_queue
    task_id = queue_task(archive)
    _, initial = claim()
    complete, unfinished = "example.test/app:done", "example.test/app:retry"
    seed_items(task_id, initial, [complete, unfinished])
    update_image_import_item(task_id, complete, "success", "completed", lease=initial)
    update_image_import_item(task_id, unfinished, "failed", "push", lease=initial)
    artifacts._finish_image_import_task(task_id, lease=initial)
    assert requeue_image_import_task(task_id)
    task, interrupted = claim("retry-worker")
    assert task["retry_failed"]

    def crash(ref):
        raise KeyboardInterrupt("simulated worker interruption")

    mock_commands(monkeypatch, [complete, unfinished], crash)
    with pytest.raises(KeyboardInterrupt):
        artifacts.process_image_import_task(task_id, retry_failed=True, lease=interrupted)
    assert find_image_import_task(task_id)["items"][1]["status"] == "processing"
    expire(test_engine, task_id)
    reclaimed, recovered = claim("recovery-worker")
    assert reclaimed["retry_failed"]
    assert find_image_import_task(task_id)["items"][1]["status"] == "pending"
    pushed = mock_commands(monkeypatch, [complete, unfinished])
    artifacts.process_image_import_task(task_id, retry_failed=True, lease=recovered)
    final = find_image_import_task(task_id)
    assert pushed == [unfinished]
    assert final["status"] == "success"
    assert (final["success_count"], final["failed_count"]) == (2, 0)
    assert all(item["status"] == "success" for item in final["items"])


def test_missing_unfinished_image_finishes_as_failed(image_queue, monkeypatch):
    _, archive = image_queue
    task_id = queue_task(archive)
    _, lease = claim()
    existing, missing = "example.test/app:existing", "example.test/app:missing"
    seed_items(task_id, lease, [existing, missing])
    update_image_import_item(task_id, existing, "success", "completed", lease=lease)
    pushed = mock_commands(monkeypatch, [existing])
    artifacts.process_image_import_task(task_id, retry_failed=True, lease=lease)
    final = find_image_import_task(task_id)
    assert pushed == []
    assert final["status"] == "partial_success"
    assert final["items"][1]["status"] == "failed"
    assert final["failed_count"] == 1


def test_old_and_new_attempts_never_overlap_namespace_or_cleanup(image_queue, monkeypatch):
    test_engine, archive = image_queue
    task_id = queue_task(archive)
    _, old = claim("old")
    ref = "example.test/app:one"
    entered_push, release_push, new_command = (threading.Event() for _ in range(3))
    local = threading.local()
    operations = []

    def run(argv, **kwargs):
        actor = local.actor
        action = (argv[4] if argv[1] == "-n" else "namespace-" + argv[2]) if argv[0] == "ctr" else "proxy"
        operations.append((actor, action))
        if actor == "new":
            new_command.set()
        if action == "ls":
            return CommandResult(0, ref, "")
        if action == "push" and actor == "old":
            entered_push.set()
            assert release_push.wait(5)
        return CommandResult(0, "", "")

    monkeypatch.setattr(artifacts, "execute_command", run)

    def execute(actor, lease):
        local.actor = actor
        artifacts.process_image_import_task(task_id, lease=lease)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(execute, "old", old)
        try:
            assert entered_push.wait(5)
            expire(test_engine, task_id)
            _, new = claim("new")
            second = pool.submit(execute, "new", new)
            assert not new_command.wait(.1)
        finally:
            release_push.set()
        with pytest.raises(ImageImportLeaseLost):
            first.result(timeout=5)
        second.result(timeout=5)
    first_new = next(index for index, entry in enumerate(operations) if entry[0] == "new")
    assert ("old", "prune") in operations[:first_new]
    assert operations[first_new - 1][0] == "old"
    assert all(actor == "new" for actor, _ in operations[first_new:])
    final = find_image_import_task(task_id)
    assert final["status"] == "success"
    assert final["attempt_count"] == 2
    assert final["items"][0]["status"] == "success"


def test_worker_failure_cannot_overwrite_reclaimed_attempt(image_queue, monkeypatch):
    from core.image_import_worker import ImageImportWorker

    test_engine, archive = image_queue
    task_id = queue_task(archive)
    _, old = claim("old-worker")

    def lose_lease_then_fail(*args, **kwargs):
        expire(test_engine, task_id)
        _, new = claim("new-worker")
        update_image_import_task(task_id, lease=new, status="success", lease_owner=None)
        raise RuntimeError("late failure from old worker")

    monkeypatch.setattr(artifacts, "process_image_import_task", lose_lease_then_fail)
    worker = ImageImportWorker()
    try:
        worker._run_task(task_id, False, old)
    finally:
        worker._executor.shutdown()
    record = find_image_import_task(task_id)
    assert record["status"] == "success"
    assert record["attempt_count"] == old.attempt + 1
    assert record["error_message"] is None


def test_worker_interruption_keeps_items_recoverable(image_queue, monkeypatch):
    from core.image_import_worker import ImageImportWorker

    _, archive = image_queue
    task_id = queue_task(archive)
    _, interrupted = claim()
    ref = "example.test/app:interrupted"
    seed_items(task_id, interrupted, [ref])
    update_image_import_item(task_id, ref, "processing", "push", lease=interrupted)

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt("simulated process interruption")

    monkeypatch.setattr(artifacts, "process_image_import_task", interrupt)
    worker = ImageImportWorker()
    try:
        worker._run_task(task_id, True, interrupted)
    finally:
        worker._executor.shutdown()
    reclaimed, _ = claim("recovery-worker")
    assert reclaimed["attempt_count"] == interrupted.attempt + 1
    assert find_image_import_task(task_id)["items"][0]["status"] == "pending"
