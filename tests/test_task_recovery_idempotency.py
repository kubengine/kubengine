"""Recovery must fence stale attempts and reconcile durable intent."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import BackgroundTasks, HTTPException, Request
from sqlalchemy import inspect, text

from core.orm.cluster import (
    Cluster,
    ClusterSchema,
    ClusterStatus,
    find_cluster_by_id,
    update_cluster_status,
)
from core.orm.engine import get_db
from core.orm.task import (
    APP_CLEANUP_TASK,
    APP_DEPLOY_TASK,
    Task,
    TaskStatus,
    _claim_task,
    ensure_task_schema,
    renew_task_lease,
    update_task_record_status,
)
from core.services import app_deployment as service
from core.task_runtime import (
    ApplicationAttempt,
    TaskLeaseLost,
    TaskSuperseded,
    checkpoint_application,
    current_attempt,
)
from test_app_task_safety import Result
from test_app_task_safety import lifecycle as lifecycle_fixture
from test_app_task_safety import run, task_for, task_state
from web.api import app as api

# Register the shared fixture under the name requested by these tests.
lifecycle = lifecycle_fixture


def submit(key, *, username="admin", name="database"):
    request = Request(
        {"type": "http", "headers": [(b"idempotency-key", key.encode())]}
    )
    data = ClusterSchema(
        name=name,
        helm_chart="test-chart",
        helm_chart_version="1.0.0",
        config={},
    )
    return api.deploy.__wrapped__(
        request, data, BackgroundTasks(), SimpleNamespace(username=username)
    )


def test_concurrent_duplicate_submissions_create_one_resource_and_task(
    lifecycle,
):
    barrier = threading.Barrier(2)

    def request():
        barrier.wait()
        return submit("concurrent-key")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: request(), range(2)))
    assert first.cluster_id == second.cluster_id
    with get_db() as db:
        assert (
            db.query(Cluster).count() == 2
        )  # lifecycle fixture plus the one submission
        assert db.query(Task).count() == 1


def test_idempotency_key_is_scoped_and_parameter_bound(lifecycle):
    first = submit("intent")
    assert submit("intent").cluster_id == first.cluster_id
    assert (
        submit("intent", username="another-user").cluster_id
        != first.cluster_id
    )
    with pytest.raises(HTTPException) as error:
        submit("intent", name="changed")
    assert error.value.status_code == 409


def test_authenticated_http_deployment_reuses_the_users_submission(
    lifecycle, monkeypatch
):
    from web.main import app
    from web.utils.auth import create_access_token

    monkeypatch.setattr(api, "_notify", lambda message: None)
    token, _ = create_access_token({"sub": "admin"})
    expected = submit("http-replay", name="from-api")

    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post(
                "/api/v1/app/deploy",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Idempotency-Key": "http-replay",
                },
                json={
                    "name": "from-api",
                    "helm_chart": "test-chart",
                    "helm_chart_version": "1.0.0",
                    "config": {},
                },
            )

    response = asyncio.run(request())
    assert response.status_code == 200, response.text
    assert response.json()["data"]["cluster_id"] == expected.cluster_id
    with get_db() as db:
        assert db.query(Task).count() == 1


def test_replaying_deleted_submission_does_not_recreate_resource(lifecycle):
    first = submit("deleted")
    with get_db() as db:
        db.delete(db.get(Cluster, first.cluster_id))
        db.commit()
    with pytest.raises(HTTPException) as error:
        submit("deleted")
    assert error.value.status_code == 410


def test_active_deploy_task_submission_is_idempotent(lifecycle):
    first = task_for(lifecycle, APP_DEPLOY_TASK)
    second = task_for(lifecycle, APP_DEPLOY_TASK)
    assert first.task_id == second.task_id
    assert (
        first.params["resource_version"]
        == second.params["resource_version"]
        == 1
    )


def test_expired_attempt_cannot_renew_or_finish(lifecycle):
    task = task_for(lifecycle, APP_DEPLOY_TASK)
    old = _claim_task(task.task_id, False, "worker", 90)
    assert _claim_task(task.task_id, True, "other", 90) is None
    with get_db() as db:
        db.get(Task, task.task_id).lease_expires_at = (
            datetime.now() - timedelta(seconds=1)
        )
        db.commit()
    new = _claim_task(task.task_id, True, "worker", 90)
    assert new.attempt == old.attempt + 1
    assert not renew_task_lease(task.task_id, old)
    with pytest.raises(TaskLeaseLost):
        update_task_record_status(task.task_id, TaskStatus.success, lease=old)
    update_task_record_status(task.task_id, TaskStatus.success, lease=new)


def test_expired_attempt_cannot_write_resource_or_checkpoint(lifecycle):
    task = task_for(lifecycle, APP_DEPLOY_TASK)
    old = _claim_task(task.task_id, False, "worker", 90)
    with get_db() as db:
        db.get(Task, task.task_id).lease_expires_at = (
            datetime.now() - timedelta(seconds=1)
        )
        db.commit()
    _claim_task(task.task_id, True, "worker", 90)
    token = current_attempt.set(
        ApplicationAttempt(
            task.task_id,
            old,
            lifecycle.cluster_id,
            lifecycle.resource_uid,
            task.params["resource_version"],
            threading.Event(),
        )
    )
    try:
        for write in (
            lambda: update_cluster_status(
                lifecycle.cluster_id, ClusterStatus.healthy
            ),
            lambda: checkpoint_application("verified"),
        ):
            with pytest.raises(TaskLeaseLost):
                write()
    finally:
        current_attempt.reset(token)
    assert find_cluster_by_id(lifecycle.cluster_id).status == "pending"
    with get_db() as db:
        assert db.get(Task, task.task_id).phase == "pending"


def test_pending_cleanup_supersedes_old_deploy_before_any_helm_call(
    lifecycle, monkeypatch
):
    deploy = task_for(lifecycle, APP_DEPLOY_TASK)
    cleanup = task_for(lifecycle, APP_CLEANUP_TASK)
    monkeypatch.setattr(
        service,
        "execute_command",
        lambda *a, **k: pytest.fail("superseded task must not touch Helm"),
    )
    with pytest.raises(TaskSuperseded):
        run(deploy, recover_running=True)
    assert (
        cleanup.params["resource_version"] > deploy.params["resource_version"]
    )
    assert find_cluster_by_id(lifecycle.cluster_id).status == "pending"


def test_cleanup_submitted_during_install_blocks_stale_status_write(
    lifecycle, monkeypatch
):
    task = task_for(lifecycle, APP_DEPLOY_TASK)
    calls = []

    def helm(argv, **kwargs):
        calls.append(argv[1])
        if argv[1] == "list":
            return Result(output="[]")
        assert argv[1] == "install"
        task_for(lifecycle, APP_CLEANUP_TASK)
        return Result()

    monkeypatch.setattr(service, "execute_command", helm)
    with pytest.raises(TaskSuperseded):
        run(task)
    assert calls == ["list", "install"]
    assert find_cluster_by_id(lifecycle.cluster_id).status == "creating"
    assert task_state(task)[0] == TaskStatus.failed


def test_cleanup_confirmation_survives_crash_before_record_removal(
    lifecycle, monkeypatch
):
    release = True
    removals = 0
    remove = service.remove_cluster_by_id

    def helm(argv, **kwargs):
        nonlocal release
        if argv[1] == "list":
            return Result(
                output=json.dumps(
                    [{"name": lifecycle.helm_name, "status": "deployed"}]
                    if release
                    else []
                )
            )
        assert argv[1] == "uninstall"
        release = False
        return Result()

    def crash(cluster_id):
        nonlocal removals
        removals += 1
        if removals == 1:
            raise SystemExit("crash after confirmed uninstall")
        return remove(cluster_id)

    monkeypatch.setattr(service, "execute_command", helm)
    monkeypatch.setattr(service, "remove_cluster_by_id", crash)
    task = task_for(lifecycle, APP_CLEANUP_TASK)
    with pytest.raises(SystemExit):
        run(task)
    with get_db() as db:
        assert db.get(Task, task.task_id).phase == "cleanup_confirmed"
    run(task, recover_running=True)
    assert find_cluster_by_id(lifecycle.cluster_id) is None
    assert task_state(task)[0] == TaskStatus.success


@pytest.mark.parametrize(
    "chart,values",
    [("different-1.0", "{}"), ("test-chart-1.0.0", '{"changed":true}')],
)
def test_existing_release_must_match_desired_inputs(
    lifecycle, monkeypatch, chart, values
):
    calls = []

    def helm(argv, **kwargs):
        calls.append(argv[1])
        if argv[1] == "list":
            return Result(
                output=json.dumps(
                    [
                        {
                            "name": lifecycle.helm_name,
                            "status": "deployed",
                            "chart": chart,
                        }
                    ]
                )
            )
        assert argv[1:3] == ["get", "values"]
        return Result(output=values)

    monkeypatch.setattr(service, "execute_command", helm)
    with pytest.raises(RuntimeError, match="不匹配"):
        run(task_for(lifecycle, APP_DEPLOY_TASK))
    assert "install" not in calls


def test_task_migration_preserves_bound_identity_and_orders_operations(
    lifecycle, monkeypatch
):
    import core.orm.engine as engine_module

    with get_db() as db:
        engine = db.get_bind()
        db.add(
            Task(
                task_func_path=APP_DEPLOY_TASK,
                resource_id=lifecycle.cluster_id,
                params={
                    "cluster_id": lifecycle.cluster_id,
                    "resource_uid": lifecycle.resource_uid,
                    "helm_name": lifecycle.helm_name,
                },
            )
        )
        db.add(
            Task(
                task_func_path=APP_CLEANUP_TASK,
                resource_id=lifecycle.cluster_id,
                params={
                    "cluster_id": lifecycle.cluster_id,
                    "resource_uid": lifecycle.resource_uid,
                    "helm_name": lifecycle.helm_name,
                },
            )
        )
        db.commit()
    monkeypatch.setattr(engine_module, "engine", engine)
    ensure_task_schema()
    ensure_task_schema()
    with get_db() as db:
        tasks = db.query(Task).order_by(Task.task_id).all()
        assert [task.params["resource_version"] for task in tasks] == [1, 2]
        assert db.get(Cluster, lifecycle.cluster_id).operation_version == 2


def test_old_task_table_migrates_twice_without_trusting_id_only_work(
    lifecycle, monkeypatch
):
    import core.orm.engine as engine_module

    with get_db() as db:
        engine = db.get_bind()
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE task"))
        connection.execute(
            text(
                (
                    "CREATE TABLE task (\n            task_id INTEGER PRIMARY "
                    "KEY, task_func_path VARCHAR NOT NULL,\n"
                    "            params "
                    "JSON NOT NULL, resource_id INTEGER NOT NULL, status "
                    "VARCHAR NOT NULL,\n            create_time DATETIME, "
                    "updated_time DATETIME, error_msg TEXT)"
                )
            )
        )
        connection.execute(
            text(
                (
                    "INSERT INTO task (task_id, task_func_path, "
                    "params, resource_id, status)\n            VALUES "
                    "(1, :operation, :params, :resource, 'pending')"
                )
            ),
            {
                "operation": APP_DEPLOY_TASK,
                "params": json.dumps({"cluster_id": lifecycle.cluster_id}),
                "resource": lifecycle.cluster_id,
            },
        )
    monkeypatch.setattr(engine_module, "engine", engine)
    ensure_task_schema()
    ensure_task_schema()
    assert {
        "lease_owner",
        "lease_expires_at",
        "heartbeat_at",
        "attempt_count",
        "phase",
    } <= {column["name"] for column in inspect(engine).get_columns("task")}
    with get_db() as db:
        task = db.get(Task, 1)
        assert task.attempt_count == 0 and task.phase == "pending"
        assert "resource_version" not in task.params
    monkeypatch.setattr(
        service,
        "execute_command",
        lambda *a, **k: pytest.fail("untrusted legacy task"),
    )
    from core.orm.task import execute_task_function

    with pytest.raises(ValueError, match="旧任务缺少资源身份"):
        execute_task_function(1, APP_DEPLOY_TASK, {})


def test_retry_cannot_cancel_pending_cleanup_intent(lifecycle):
    task_for(lifecycle, APP_CLEANUP_TASK)
    with pytest.raises(HTTPException) as error:
        api.retry_cluster.__wrapped__(
            Request({"type": "http", "headers": []}), lifecycle.cluster_id
        )
    assert error.value.status_code == 409


def test_replayed_submission_reports_current_resource_version(lifecycle):
    first = submit("versioned")
    assert first.operation_version == 1
    assert submit("versioned").operation_version == first.operation_version


def test_failed_cleanup_retry_keeps_confirmed_checkpoint(
    lifecycle, monkeypatch
):
    present = True
    uninstall_calls = []
    remove = service.remove_cluster_by_id

    def helm(argv, **kwargs):
        nonlocal present
        if argv[1] == "list":
            return Result(
                output=json.dumps(
                    [{"name": lifecycle.helm_name, "status": "deployed"}]
                    if present
                    else []
                )
            )
        uninstall_calls.append(argv)
        present = False
        return Result()

    def fail_once(cluster_id):
        monkeypatch.setattr(service, "remove_cluster_by_id", remove)
        raise RuntimeError("temporary database write failure")

    monkeypatch.setattr(service, "execute_command", helm)
    monkeypatch.setattr(service, "remove_cluster_by_id", fail_once)
    original = task_for(lifecycle, APP_CLEANUP_TASK)
    with pytest.raises(RuntimeError, match="temporary database"):
        run(original)
    retry = task_for(lifecycle, APP_CLEANUP_TASK)
    assert (
        retry.task_id == original.task_id
        and retry.phase == "cleanup_confirmed"
    )
    run(retry)
    assert len(uninstall_calls) == 1
    assert find_cluster_by_id(lifecycle.cluster_id) is None
