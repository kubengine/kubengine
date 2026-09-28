"""Task management ORM models and execution utilities.

This module defines the task table model and provides utilities for
creating, updating, and executing background tasks with security
controls.
"""

import enum
import fcntl
import importlib
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Enum,
    Integer,
    String,
    Text,
    inspect,
    text,
)
from sqlalchemy.orm import Query, Session

from core.logger import get_logger, with_log_context
from core.orm.engine import Base, get_db
from core.orm.task_lease import owned_query, renew_lease
from core.runtime_files import private_runtime_file
from core.task_runtime import (
    ApplicationAttempt,
    TaskLease,
    TaskLeaseLost,
    TaskSuperseded,
    check_application_attempt,
    current_attempt,
    lease_heartbeat,
)

logger = get_logger(__name__)


class TaskStatus(enum.Enum):
    """Enumeration for task status values."""

    pending = "pending"  # Awaiting execution
    running = "running"  # Currently executing
    success = "success"  # Completed successfully
    failed = "failed"  # Execution failed


class Task(Base):
    """Task table model representing background jobs."""

    __tablename__ = "task"

    task_id = Column(Integer, primary_key=True, index=True, comment="Task ID")
    task_func_path = Column(
        String,
        nullable=False,
        comment="Task function path (e.g., task_functions.run_demo_task)",
    )
    params = Column(JSON, nullable=False, comment="Task function parameters")
    resource_id = Column(
        Integer,
        nullable=False,
        comment="Associated resource ID for frontend reference",
    )
    status = Column(
        Enum(TaskStatus),
        default=TaskStatus.pending,
        nullable=False,
        comment="Task status",
    )
    create_time = Column(
        DateTime, default=datetime.now, comment="Creation timestamp"
    )
    updated_time = Column(
        DateTime,
        default=datetime.now,
        onupdate=datetime.now,
        comment="Last update timestamp",
    )
    error_msg = Column(Text, comment="Error message if task failed")
    lease_owner = Column(String)
    lease_expires_at = Column(DateTime, index=True)
    heartbeat_at = Column(DateTime)
    attempt_count = Column(Integer, nullable=False, default=0)
    phase = Column(String, nullable=False, default="pending")


class TaskSchema(BaseModel):
    """Pydantic model for task serialization."""

    task_id: Optional[int] = None
    task_func_path: Optional[str] = None
    params: Optional[Dict[str, Any]] = None
    resource_id: Optional[int] = None
    status: Optional[str] = None
    create_time: Optional[datetime] = None
    updated_time: Optional[datetime] = None
    error_msg: Optional[str] = None
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[datetime] = None
    heartbeat_at: Optional[datetime] = None
    attempt_count: int = 0
    phase: str = "pending"

    class Config:
        from_attributes = True
        arbitrary_types_allowed = True


# Persist stable operation names, rather than HTTP handler import paths.
APP_DEPLOY_TASK = "app.deploy"
APP_CLEANUP_TASK = "app.cleanup"
_TASK_HANDLERS = {
    APP_DEPLOY_TASK: ("core.services.app_deployment", "deploy_app"),
    APP_CLEANUP_TASK: ("core.services.app_deployment", "clean_up_cluster"),
    # These unambiguous names were accepted by older recovery code.
    "web.api.app.deploy_app": ("core.services.app_deployment", "deploy_app"),
    "web.api.app.clean_up_cluster": (
        "core.services.app_deployment",
        "clean_up_cluster",
    ),
}


def create_task_record(
    task_func_path: str,
    params: Dict[str, Any],
    resource_id: int,
    *,
    db: Optional[Session] = None,
) -> TaskSchema:
    """
    Bind a task to a resource incarnation in the caller's transaction.
    """
    from sqlalchemy import text

    from core.orm.cluster import Cluster

    if task_func_path not in _TASK_HANDLERS:
        raise ValueError(f"Unknown task operation: {task_func_path}")
    if db is None:
        with get_db() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            result = create_task_record(
                task_func_path, params, resource_id, db=session
            )
            session.commit()
            return result
    cluster = db.get(Cluster, resource_id)
    if cluster is None or params.get("cluster_id") != resource_id:
        raise ValueError("Task resource does not exist")
    operation = (
        APP_DEPLOY_TASK
        if _TASK_HANDLERS[task_func_path][1] == "deploy_app"
        else APP_CLEANUP_TASK
    )
    existing = (
        db.query(Task)
        .filter_by(resource_id=resource_id)
        .filter(
            Task.status.in_(
                [TaskStatus.pending, TaskStatus.running, TaskStatus.failed]
            ),
        )
        .order_by(Task.task_id.desc())
        .all()
    )
    for row in existing:
        if (
            isinstance(row.params, dict)
            and row.task_func_path in _TASK_HANDLERS
            and _TASK_HANDLERS[row.task_func_path] == _TASK_HANDLERS[operation]
            and row.params.get("resource_uid") == cluster.resource_uid
            and row.params.get("resource_version") == cluster.operation_version
        ):
            if row.status == TaskStatus.failed:
                # Retry the same intent, retaining its confirmed phase
                # and monotonic attempt counter, just like an
                # image-import retry.
                row.status = TaskStatus.pending
                row.error_msg = None
                row.lease_owner = row.lease_expires_at = row.heartbeat_at = (
                    None
                )
                row.updated_time = datetime.now()
                db.flush()
            return TaskSchema.model_validate(row)
    cluster.operation_version += 1
    bound = {
        "cluster_id": resource_id,
        "resource_uid": cluster.resource_uid,
        "helm_name": cluster.helm_name,
        "resource_version": cluster.operation_version,
    }
    task = Task(
        task_func_path=operation,
        params=bound,
        resource_id=resource_id,
        status=TaskStatus.pending,
    )
    db.add(task)
    db.flush()
    return TaskSchema.model_validate(task)


def update_task_record_status(
    task_id: int,
    status: TaskStatus,
    error_msg: Optional[str] = None,
    *,
    lease: Optional[TaskLease] = None,
) -> Optional[TaskSchema]:
    """Update task status, progress, or error information.

    Args:
        task_id: Task ID to update
        status: New task status
        error_msg: Optional error message for failed tasks

    Returns:
        Updated task schema if found, None otherwise

    Raises:
        Exception: When database operation fails
    """
    try:
        with get_db() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            if lease is None:
                raise TaskLeaseLost("任务状态写入必须提供执行租约")
            task = owned_task_query(db, task_id, lease).first()
            if task is None:
                raise TaskLeaseLost("任务租约已失效，拒绝写入状态")
            if task:
                task.status = status  # type: ignore
                task.error_msg = error_msg  # type: ignore
                task.updated_time = datetime.now()  # type: ignore
                if status in {TaskStatus.success, TaskStatus.failed}:
                    task.lease_owner = None
                    task.lease_expires_at = None
                    task.heartbeat_at = None

                db.commit()
                db.refresh(task)

                logger.debug(
                    f"Task {task_id} status updated to {status.value}"
                )
                return TaskSchema.model_validate(task)

            logger.warning(f"Task not found for status update: ID {task_id}")
            return None

    except Exception as e:
        logger.error(f"Failed to update task status: {str(e)}")
        raise


def find_unfinished_tasks() -> List[TaskSchema]:
    """Retrieve all unfinished tasks (pending or running).

    Returns:
        List of unfinished task schemas

    Raises:
        Exception: When database operation fails
    """
    try:
        with get_db() as db:
            query: Query[Task] = db.query(Task).filter(
                (Task.status == TaskStatus.pending)
                | (
                    (Task.status == TaskStatus.running)
                    & (
                        (Task.lease_expires_at.is_(None))
                        | (Task.lease_expires_at <= datetime.now())
                    )
                )
            )
            tasks = query.all()

            logger.debug(f"Found {len(tasks)} unfinished tasks")
            return [TaskSchema.model_validate(task) for task in tasks]

    except Exception as e:
        logger.error(f"Failed to retrieve unfinished tasks: {str(e)}")
        raise


@contextmanager
def _operation_lock(key: str, *, blocking: bool = True):
    """Coordinate API threads and the recovery process on this
    installation.
    """
    with private_runtime_file(f"{key}.lock") as lock:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(lock.fileno(), flags)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def owned_task_query(db, task_id, lease):
    return owned_query(db, Task, task_id, lease, TaskStatus.running)


def renew_task_lease(task_id, lease, lease_seconds=90):
    return renew_lease(
        Task, task_id, lease, TaskStatus.running, lease_seconds, "updated_time"
    )


def _claim_task(
    task_id: int, recover_running: bool, owner: str, lease_seconds: int
):
    with get_db() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        eligible = Task.status == TaskStatus.pending
        if recover_running:
            eligible = eligible | (
                (Task.status == TaskStatus.running)
                & (
                    (Task.lease_expires_at.is_(None))
                    | (Task.lease_expires_at <= datetime.now())
                )
            )
        task = db.query(Task).filter(Task.task_id == task_id, eligible).first()
        if task is None:
            return None
        task.status = TaskStatus.running
        task.error_msg = None
        task.lease_owner = owner
        task.attempt_count += 1
        task.heartbeat_at = task.updated_time = datetime.now()
        task.lease_expires_at = datetime.now() + timedelta(
            seconds=lease_seconds
        )
        result = TaskLease(owner, task.attempt_count)
        db.commit()
        return result


def ensure_task_schema():
    from core.orm.engine import engine

    additions = {
        "lease_owner": "VARCHAR",
        "lease_expires_at": "DATETIME",
        "heartbeat_at": "DATETIME",
        "attempt_count": "INTEGER NOT NULL DEFAULT 0",
        "phase": "VARCHAR NOT NULL DEFAULT 'pending'",
    }
    with _operation_lock("task-schema"):
        with engine.begin() as connection:
            existing = {
                c["name"] for c in inspect(connection).get_columns("task")
            }
            for name, definition in additions.items():
                if name not in existing:
                    connection.execute(
                        text(
                            f"ALTER TABLE task ADD COLUMN {name} {definition}"
                        )
                    )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_task_lease_expires_at ON"
                    " task(lease_expires_at)"
                )
            )
        # Upgrade only tasks already bound to this exact resource
        # incarnation. Older ID-only tasks remain untrusted and fail
        # closed during recovery.
        from core.orm.cluster import Cluster

        with get_db() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            for task in db.query(Task).order_by(Task.task_id).all():
                params = task.params
                if (
                    not isinstance(params, dict)
                    or "resource_version" in params
                ):
                    continue
                cluster = db.get(Cluster, task.resource_id)
                if (
                    cluster is None
                    or params.get("resource_uid") != cluster.resource_uid
                    or params.get("helm_name") != cluster.helm_name
                ):
                    continue
                cluster.operation_version += 1
                task.params = {
                    **params,
                    "resource_version": cluster.operation_version,
                }
            db.commit()


@with_log_context(task_id="task_id")
def execute_task_function(
    task_id: int,
    task_func_path: str,
    task_params: Dict[str, Any],
    *,
    recover_running: bool = False,
    owner: Optional[str] = None,
    lease_seconds: int = 90,
) -> None:
    """Run an explicitly registered operation and own its terminal task
    state.

    The task lock prevents recovery from replaying a still-running API
    task. The resource lock serializes deploy/delete actions for the
    same cluster. OS locks are released after crashes, so interrupted
    work can be reclaimed.
    """
    with _operation_lock(f"task-{int(task_id)}", blocking=False) as acquired:
        if not acquired:
            return
        with get_db() as db:
            row = db.get(Task, task_id)
            if row is None:
                return
            task_func_path = row.task_func_path
            task_params = (
                dict(row.params) if isinstance(row.params, dict) else {}
            )
        cluster_id = task_params.get("cluster_id")
        # Invalid/legacy work must fail without touching a resource.
        lock_key = (
            f"cluster-{cluster_id}"
            if isinstance(cluster_id, int)
            else f"invalid-task-{task_id}"
        )
        with _operation_lock(lock_key, blocking=False) as resource_acquired:
            if not resource_acquired:
                return
            lease = _claim_task(
                task_id, recover_running, owner or uuid4().hex, lease_seconds
            )
            if lease is None:
                return
            with lease_heartbeat(
                lambda: renew_task_lease(task_id, lease, lease_seconds),
                interval=max(1, lease_seconds / 3),
            ) as lost:
                _run_claimed_task(
                    task_id,
                    task_func_path,
                    task_params,
                    cluster_id,
                    lease,
                    lost,
                )


def _run_claimed_task(
    task_id, task_func_path, task_params, cluster_id, lease, lost
):
    from core.orm.cluster import find_cluster_by_id

    token = None
    try:
        if task_func_path == "api.app.deploy_app":
            raise ValueError(
                "Legacy task operation is ambiguous; resources retained for"
                " manual reconciliation"
            )
        if task_func_path not in _TASK_HANDLERS:
            raise ValueError(f"Unknown task operation: {task_func_path}")
        if (
            not isinstance(cluster_id, int)
            or isinstance(cluster_id, bool)
            or cluster_id <= 0
        ):
            raise ValueError("Task requires a positive cluster_id")
        if (
            not task_params.get("resource_uid")
            or not task_params.get("helm_name")
            or not isinstance(task_params.get("resource_version"), int)
        ):
            raise ValueError(
                "旧任务缺少资源身份，拒绝自动执行；请核实资源后重新提交"
            )
        cluster = find_cluster_by_id(cluster_id)
        if (
            cluster is None
            and _TASK_HANDLERS[task_func_path][1] == "clean_up_cluster"
        ):
            update_task_record_status(task_id, TaskStatus.success, lease=lease)
            return
        if (
            cluster is None
            or cluster.resource_uid != task_params["resource_uid"]
            or cluster.helm_name != task_params["helm_name"]
        ):
            raise ValueError("任务对应的资源身份已变化，拒绝操作当前资源")
        if cluster.operation_version != task_params["resource_version"]:
            raise TaskSuperseded("资源已有更新的操作，拒绝恢复旧部署任务")
        token = current_attempt.set(
            ApplicationAttempt(
                task_id,
                lease,
                cluster_id,
                cluster.resource_uid,
                task_params["resource_version"],
                lost,
            )
        )
        check_application_attempt()
        module_name, function_name = _TASK_HANDLERS[task_func_path]
        task_func = getattr(
            importlib.import_module(module_name), function_name
        )
        task_func(task_id, cluster_id=cluster_id)
        update_task_record_status(task_id, TaskStatus.success, lease=lease)
        logger.info("Task %s completed successfully", task_id)
    except TaskLeaseLost:
        raise
    except Exception as exc:
        update_task_record_status(
            task_id, TaskStatus.failed, str(exc), lease=lease
        )
        logger.exception("Task %s failed", task_id)
        raise
    except BaseException:
        # Process interruption leaves the phase recoverable, but revokes
        # this attempt before releasing the operation locks.
        with get_db() as db:
            owned_task_query(db, task_id, lease).update(
                {"lease_expires_at": datetime.now()}, synchronize_session=False
            )
            db.commit()
        raise
    finally:
        if token is not None:
            current_attempt.reset(token)
