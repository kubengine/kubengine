"""Lifecycle regressions; commands, Kubernetes and notification IO are mocked."""

import json
from pathlib import Path
import stat
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from core.config import Application
import core.orm.engine as engine_module
from core.orm.cluster import ClusterSchema, ClusterStatus, create_cluster, find_cluster_by_id, update_cluster_status
from core.orm.engine import Base, get_db
from core.orm.task import (
    APP_CLEANUP_TASK, APP_DEPLOY_TASK, Task, TaskStatus, create_task_record,
    execute_task_function,
)
import core.services.app_deployment as app_api

_real_notify = app_api._notify


class Result:
    def __init__(self, *, failed=False, output=""):
        self.failed = failed
        self.output = output

    def is_failure(self):
        return self.failed

    def get_output_lines(self):
        return self.output.splitlines()

    def get_error_lines(self):
        return ["simulated Helm failure"] if self.failed else []


@pytest.fixture
def lifecycle(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'lifecycle.db'}")
    sessions = scoped_session(sessionmaker(bind=engine))
    monkeypatch.setattr(engine_module, "SessionLocal", sessions)
    monkeypatch.setattr(Application, "ROOT_DIR", str(tmp_path))
    monkeypatch.setattr(app_api, "pendulum_sleep", lambda *args: None)
    monkeypatch.setattr(app_api, "_notify", lambda message: None)
    monkeypatch.setattr(app_api, "HelmResourceChecker", lambda **kwargs: SimpleNamespace(
        check_pods_with_polling=lambda: {"status": True},
    ))
    Base.metadata.create_all(engine)
    cluster = create_cluster(ClusterSchema(
        name="test", helm_chart="test-chart", helm_chart_version="1.0.0", config={},
    ))
    yield cluster
    sessions.remove()
    engine.dispose()


def task_for(cluster, operation):
    return create_task_record(operation, {"cluster_id": cluster.cluster_id}, cluster.cluster_id)


def run(task, **kwargs):
    execute_task_function(task.task_id, task.task_func_path, task.params, **kwargs)


def task_state(task):
    with get_db() as db:
        row = db.query(Task).filter_by(task_id=task.task_id).one()
        return row.status, row.error_msg


def test_failed_cleanup_retries_helm_before_removing_record(monkeypatch, lifecycle):
    calls = []
    uninstall_count = 0

    def command(argv, **kwargs):
        nonlocal uninstall_count
        calls.append((argv, kwargs))
        if argv[1] == "list":
            return Result(output=json.dumps([{"name": lifecycle.helm_name, "status": "deployed", "chart": "test-chart-1.0.0"}]))
        assert find_cluster_by_id(lifecycle.cluster_id).status == "cleaning"
        uninstall_count += 1
        return Result(failed=uninstall_count == 1)

    monkeypatch.setattr(app_api, "execute_command", command)
    first = task_for(lifecycle, APP_CLEANUP_TASK)
    with pytest.raises(RuntimeError, match="Helm"):
        run(first)
    assert find_cluster_by_id(lifecycle.cluster_id).status == "anomaly"
    assert task_state(first)[0] == TaskStatus.failed
    assert "simulated Helm failure" in task_state(first)[1]

    second = task_for(lifecycle, APP_CLEANUP_TASK)
    run(second)
    assert len(calls) == 4 and uninstall_count == 2
    assert all("--ignore-not-found" not in argv for argv, _ in calls)
    assert all("--wait" in argv for argv, _ in calls if argv[1] == "uninstall")
    assert all(options["env"] == {"KUBECONFIG": "/etc/kubernetes/admin.conf"} for _, options in calls)
    assert find_cluster_by_id(lifecycle.cluster_id) is None
    assert task_state(second) == (TaskStatus.success, None)


def test_notification_failure_does_not_undo_successful_cleanup(monkeypatch, lifecycle):
    monkeypatch.setattr(app_api, "_notify", _real_notify)
    monkeypatch.setattr(app_api, "execute_command", lambda argv, **kwargs: Result(
        output=json.dumps([{"name": lifecycle.helm_name, "status": "deployed", "chart": "test-chart-1.0.0"}]) if argv[1] == "list" else "",
    ))

    calls = []

    def fail_notification():
        calls.append(True)
        raise RuntimeError("notification unavailable")

    monkeypatch.setattr("core.orm.notifications.publish_cluster_change", fail_notification)
    task = task_for(lifecycle, APP_CLEANUP_TASK)
    run(task)
    assert calls
    assert find_cluster_by_id(lifecycle.cluster_id) is None
    assert task_state(task) == (TaskStatus.success, None)


def test_deploy_recovery_checks_existing_release_without_reinstall(monkeypatch, lifecycle):
    calls = []

    def command(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["get", "values"]:
            return Result(output="{}")
        return Result(output=json.dumps([{"name": lifecycle.helm_name, "status": "deployed", "chart": "test-chart-1.0.0"}]))

    monkeypatch.setattr(app_api, "execute_command", command)
    task = task_for(lifecycle, APP_DEPLOY_TASK)
    with get_db() as db:
        db.query(Task).filter_by(task_id=task.task_id).update({"status": TaskStatus.running})
        db.commit()
    run(task, recover_running=True)
    assert [call[1] for call in calls] == ["list", "get"]
    assert find_cluster_by_id(lifecycle.cluster_id).status == "healthy"
    assert task_state(task) == (TaskStatus.success, None)


def test_deploy_install_failure_keeps_record_and_removes_private_values(monkeypatch, lifecycle):
    files = []

    def command(argv, **kwargs):
        assert isinstance(argv, list)
        if argv[1] == "list":
            return Result(output="[]")
        assert argv[1] == "install"
        values_path = Path(argv[argv.index("-f") + 1])
        assert stat.S_IMODE(values_path.stat().st_mode) == 0o600
        files.append(values_path)
        return Result(failed=True)

    monkeypatch.setattr(app_api, "execute_command", command)
    task = task_for(lifecycle, APP_DEPLOY_TASK)
    with pytest.raises(RuntimeError, match="Helm"):
        run(task)
    assert files and not files[0].exists()
    assert find_cluster_by_id(lifecycle.cluster_id).status == "unhealthy"
    assert task_state(task)[0] == TaskStatus.failed


@pytest.mark.parametrize("response", ["command-error", "invalid-json", "pending-install"])
def test_release_discovery_failures_never_run_install(monkeypatch, lifecycle, response):
    list_result = {
        "command-error": Result(failed=True),
        "invalid-json": Result(output="not JSON"),
        "pending-install": Result(output=json.dumps([
            {"name": lifecycle.helm_name, "status": "pending-install"},
        ])),
    }[response]
    calls = []

    def command(argv, **kwargs):
        calls.append(argv)
        return list_result

    monkeypatch.setattr(app_api, "execute_command", command)
    task = task_for(lifecycle, APP_DEPLOY_TASK)
    with pytest.raises((RuntimeError, ValueError)):
        run(task)
    assert len(calls) == 1 and calls[0][1] == "list"
    assert find_cluster_by_id(lifecycle.cluster_id) is not None
    assert task_state(task)[0] == TaskStatus.failed


def test_failed_health_check_marks_task_failed(monkeypatch, lifecycle):
    monkeypatch.setattr(app_api, "execute_command", lambda argv, **kwargs: Result(
        output="{}" if argv[1] == "get" else json.dumps([{"name": lifecycle.helm_name, "status": "deployed", "chart": "test-chart-1.0.0"}]),
    ))
    monkeypatch.setattr(app_api, "HelmResourceChecker", lambda **kwargs: SimpleNamespace(
        check_pods_with_polling=lambda: {"status": False},
    ))
    task = task_for(lifecycle, APP_DEPLOY_TASK)
    with pytest.raises(RuntimeError, match="健康检查"):
        run(task)
    assert task_state(task)[0] == TaskStatus.failed
    assert find_cluster_by_id(lifecycle.cluster_id).status == "unhealthy"


def test_ambiguous_legacy_task_is_failed_without_touching_resources(monkeypatch, lifecycle):
    monkeypatch.setattr(app_api, "execute_command", lambda *args, **kwargs: pytest.fail("must not execute Helm"))
    with get_db() as db:
        row = Task(task_func_path="api.app.deploy_app", params={"cluster_id": lifecycle.cluster_id},
                   resource_id=lifecycle.cluster_id, status=TaskStatus.running)
        db.add(row)
        db.commit()
        task_id = row.task_id
    with pytest.raises(ValueError, match="ambiguous"):
        execute_task_function(task_id, "api.app.deploy_app", {"cluster_id": lifecycle.cluster_id}, recover_running=True)
    assert task_state(SimpleNamespace(task_id=task_id))[0] == TaskStatus.failed
    assert find_cluster_by_id(lifecycle.cluster_id).status == "pending"


def test_live_task_is_not_replayed_by_recovery(monkeypatch, lifecycle):
    entered, release = threading.Event(), threading.Event()
    calls, errors = [], []

    def command(argv, **kwargs):
        if argv[1] == "list":
            return Result(output=json.dumps([{"name": lifecycle.helm_name, "status": "deployed", "chart": "test-chart-1.0.0"}]))
        calls.append(argv)
        entered.set()
        assert release.wait(5)
        return Result()

    monkeypatch.setattr(app_api, "execute_command", command)
    task = task_for(lifecycle, APP_CLEANUP_TASK)

    def first_run():
        try:
            run(task)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=first_run)
    thread.start()
    try:
        assert entered.wait(5)
        run(task, recover_running=True)
        assert len(calls) == 1
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive() and not errors
    run(task, recover_running=True)
    assert len(calls) == 1
    assert task_state(task) == (TaskStatus.success, None)


def test_cleanup_lookup_permission_error_preserves_record(monkeypatch, lifecycle):
    calls = []

    def command(argv, **kwargs):
        calls.append(argv)
        return Result(failed=True)

    monkeypatch.setattr(app_api, "execute_command", command)
    task = task_for(lifecycle, APP_CLEANUP_TASK)
    with pytest.raises(RuntimeError, match="Helm"):
        run(task)
    assert len(calls) == 1 and calls[0][1] == "list"
    assert find_cluster_by_id(lifecycle.cluster_id) is not None
    assert task_state(task)[0] == TaskStatus.failed


@pytest.mark.parametrize("state", [ClusterStatus.healthy, ClusterStatus.unhealthy, ClusterStatus.creating,
                                   ClusterStatus.checking, ClusterStatus.cleaning, ClusterStatus.anomaly])
def test_missing_release_keeps_previously_started_cluster(monkeypatch, lifecycle, state):
    update_cluster_status(lifecycle.cluster_id, state)
    calls = []

    def command(argv, **kwargs):
        calls.append(argv)
        return Result(output="[]")

    monkeypatch.setattr(app_api, "execute_command", command)
    task = task_for(lifecycle, APP_CLEANUP_TASK)
    with pytest.raises(RuntimeError, match="人工核实"):
        run(task)
    assert len(calls) == 1 and calls[0][1] == "list"
    assert find_cluster_by_id(lifecycle.cluster_id) is not None
    assert task_state(task)[0] == TaskStatus.failed


def test_pending_cluster_without_release_can_be_removed(monkeypatch, lifecycle):
    monkeypatch.setattr(app_api, "execute_command", lambda *args, **kwargs: Result(output="[]"))
    task = task_for(lifecycle, APP_CLEANUP_TASK)
    run(task)
    assert find_cluster_by_id(lifecycle.cluster_id) is None
    assert task_state(task)[0] == TaskStatus.success


def test_timeout_then_missing_release_keeps_record_on_retry(monkeypatch, lifecycle):
    release_exists = True
    uninstalls = []

    def command(argv, **kwargs):
        nonlocal release_exists
        if argv[1] == "list":
            return Result(output=json.dumps([
                {"name": lifecycle.helm_name, "status": "deployed", "chart": "test-chart-1.0.0"},
            ] if release_exists else []))
        assert find_cluster_by_id(lifecycle.cluster_id).status == "cleaning"
        uninstalls.append(argv)
        release_exists = False  # Helm purges history even after a wait timeout.
        return Result(failed=True)

    monkeypatch.setattr(app_api, "execute_command", command)
    with pytest.raises(RuntimeError, match="Helm"):
        run(task_for(lifecycle, APP_CLEANUP_TASK))
    with pytest.raises(RuntimeError, match="人工核实"):
        run(task_for(lifecycle, APP_CLEANUP_TASK))
    assert len(uninstalls) == 1
    assert find_cluster_by_id(lifecycle.cluster_id) is not None


def test_crash_during_uninstall_leaves_cleaning_marker_for_recovery(monkeypatch, lifecycle):
    release_exists = True

    def command(argv, **kwargs):
        nonlocal release_exists
        if argv[1] == "list":
            return Result(output=json.dumps([
                {"name": lifecycle.helm_name, "status": "deployed", "chart": "test-chart-1.0.0"},
            ] if release_exists else []))
        release_exists = False
        assert find_cluster_by_id(lifecycle.cluster_id).status == "cleaning"
        raise SystemExit("simulate worker process exiting before error persistence")

    monkeypatch.setattr(app_api, "execute_command", command)
    task = task_for(lifecycle, APP_CLEANUP_TASK)
    with pytest.raises(SystemExit):
        run(task)
    assert find_cluster_by_id(lifecycle.cluster_id).status == "cleaning"
    assert task_state(task)[0] == TaskStatus.running
    with pytest.raises(RuntimeError, match="人工核实"):
        run(task, recover_running=True)
    assert find_cluster_by_id(lifecycle.cluster_id) is not None
    assert task_state(task)[0] == TaskStatus.failed


def test_uninstalled_helm_metadata_is_not_proof_resources_were_deleted(monkeypatch, lifecycle):
    calls = []

    def command(argv, **kwargs):
        calls.append(argv)
        return Result(output=json.dumps([{"name": lifecycle.helm_name, "status": "uninstalled"}]))

    monkeypatch.setattr(app_api, "execute_command", command)
    with pytest.raises(RuntimeError, match="人工核实"):
        run(task_for(lifecycle, APP_CLEANUP_TASK))
    assert len(calls) == 1 and calls[0][1] == "list"
    assert find_cluster_by_id(lifecycle.cluster_id) is not None


def test_old_deployment_cannot_reinstall_after_later_cleanup_failed(monkeypatch, lifecycle):
    old_deployment = task_for(lifecycle, APP_DEPLOY_TASK)
    with get_db() as db:
        db.query(Task).filter_by(task_id=old_deployment.task_id).update({"status": TaskStatus.running})
        db.commit()
    release_exists = True
    calls = []

    def command(argv, **kwargs):
        nonlocal release_exists
        calls.append(argv)
        if argv[1] == "list":
            return Result(output=json.dumps([
                {"name": lifecycle.helm_name, "status": "deployed", "chart": "test-chart-1.0.0"},
            ] if release_exists else []))
        assert argv[1] == "uninstall", "old deploy must never reinstall the release"
        release_exists = False
        return Result(failed=True)

    monkeypatch.setattr(app_api, "execute_command", command)
    with pytest.raises(RuntimeError, match="Helm"):
        run(task_for(lifecycle, APP_CLEANUP_TASK))
    assert find_cluster_by_id(lifecycle.cluster_id).status == "anomaly"
    with pytest.raises(RuntimeError, match="拒绝恢复旧部署任务"):
        run(old_deployment, recover_running=True)
    assert len(calls) == 2
    assert find_cluster_by_id(lifecycle.cluster_id).status == "anomaly"
    assert task_state(old_deployment)[0] == TaskStatus.failed


def test_deployment_recovery_preserves_interrupted_cleaning_state(monkeypatch, lifecycle):
    update_cluster_status(lifecycle.cluster_id, ClusterStatus.cleaning)
    monkeypatch.setattr(app_api, "execute_command", lambda *args, **kwargs: pytest.fail("must not execute Helm"))
    task = task_for(lifecycle, APP_DEPLOY_TASK)
    with pytest.raises(RuntimeError, match="拒绝恢复旧部署任务"):
        run(task, recover_running=True)
    assert find_cluster_by_id(lifecycle.cluster_id).status == "cleaning"
    assert task_state(task)[0] == TaskStatus.failed
