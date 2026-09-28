"""Task management ORM models and execution utilities.

This module defines the task table model and provides utilities for
creating, updating, and executing background tasks with security controls.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
import enum
import fcntl
import importlib
from typing import Any, Dict, List, Optional

from pydantic import BaseModel
from sqlalchemy import JSON, Column, DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Query, Session

from core.logger import get_logger, with_log_context
from core.runtime_files import private_runtime_file
from core.orm.engine import Base, get_db

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
        comment="Task function path (e.g., task_functions.run_demo_task)"
    )
    params = Column(
        JSON,
        nullable=False,
        comment="Task function parameters"
    )
    resource_id = Column(
        Integer,
        nullable=False,
        comment="Associated resource ID for frontend reference"
    )
    status = Column(
        Enum(TaskStatus),
        default=TaskStatus.pending,
        nullable=False,
        comment="Task status"
    )
    create_time = Column(
        DateTime,
        default=datetime.now,
        comment="Creation timestamp"
    )
    updated_time = Column(
        DateTime,
        default=datetime.now,
        onupdate=datetime.now,
        comment="Last update timestamp"
    )
    error_msg = Column(Text, comment="Error message if task failed")


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

    class Config:
        from_attributes = True
        arbitrary_types_allowed = True


# Persist stable operation names, rather than HTTP handler import paths.
APP_DEPLOY_TASK = "app.deploy"
APP_CLEANUP_TASK = "app.cleanup"
_TASK_HANDLERS = {
    APP_DEPLOY_TASK: ("web.api.app", "deploy_app"),
    APP_CLEANUP_TASK: ("web.api.app", "clean_up_cluster"),
    # These unambiguous names were accepted by older recovery code.
    "web.api.app.deploy_app": ("web.api.app", "deploy_app"),
    "web.api.app.clean_up_cluster": ("web.api.app", "clean_up_cluster"),
}


def create_task_record(
    task_func_path: str, params: Dict[str, Any], resource_id: int,
    *, db: Optional[Session] = None,
) -> TaskSchema:
    """Bind a task to a resource incarnation in the caller's transaction."""
    from core.orm.cluster import Cluster
    from sqlalchemy import text

    if task_func_path not in _TASK_HANDLERS:
        raise ValueError(f"Unknown task operation: {task_func_path}")
    if db is None:
        with get_db() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            result = create_task_record(task_func_path, params, resource_id, db=session)
            session.commit()
            return result
    cluster = db.get(Cluster, resource_id)
    if cluster is None or params.get("cluster_id") != resource_id:
        raise ValueError("Task resource does not exist")
    # Only derive identity from the row, never from client-supplied parameters.
    bound = {"cluster_id": resource_id, "resource_uid": cluster.resource_uid,
             "helm_name": cluster.helm_name}
    if task_func_path == APP_CLEANUP_TASK:
        existing = db.query(Task).filter_by(resource_id=resource_id, task_func_path=task_func_path).filter(
            Task.status.in_([TaskStatus.pending, TaskStatus.running]),
        ).all()
        for row in existing:
            if row.params == bound:
                return TaskSchema.model_validate(row)
    task = Task(task_func_path=task_func_path, params=bound,
                resource_id=resource_id, status=TaskStatus.pending)
    db.add(task)
    db.flush()
    return TaskSchema.model_validate(task)


def update_task_record_status(
    task_id: int,
    status: TaskStatus,
    error_msg: Optional[str] = None
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
            task = db.query(Task).filter(Task.task_id == task_id).first()
            if task:
                task.status = status  # type: ignore
                task.error_msg = error_msg  # type: ignore
                task.updated_time = datetime.now()  # type: ignore

                db.commit()
                db.refresh(task)

                logger.debug(
                    f"Task {task_id} status updated to {status.value}")
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
                Task.status.in_([TaskStatus.pending, TaskStatus.running])
            )
            tasks = query.all()

            logger.debug(f"Found {len(tasks)} unfinished tasks")
            return [TaskSchema.model_validate(task) for task in tasks]

    except Exception as e:
        logger.error(f"Failed to retrieve unfinished tasks: {str(e)}")
        raise


def recover_unfinished_tasks_async() -> None:
    """Recover unfinished tasks on service startup with concurrent execution.

    Executes pending tasks and recovers running tasks on service restart.
    """
    try:
        unfinished_tasks = find_unfinished_tasks()

        if not unfinished_tasks:
            logger.info("No unfinished tasks to recover")
            return

        logger.info(f"Recovering {len(unfinished_tasks)} unfinished tasks...")

        # Create thread pool with controlled concurrency
        with ThreadPoolExecutor(max_workers=4) as executor:
            for task in unfinished_tasks:
                if task.task_id and task.task_func_path and task.params is not None:
                    executor.submit(
                        _execute_and_log_task,
                        task.task_id,
                        task.task_func_path,
                        task.params
                    )

    except Exception as e:
        logger.error(f"Failed to recover unfinished tasks: {str(e)}")
        raise


@with_log_context(task_id="task_id")
def _execute_and_log_task(
    task_id: int,
    task_func_path: str,
    task_params: Dict[str, Any]
) -> None:
    """Execute task and log result (internal helper function).

    Args:
        task_id: Task ID
        task_func_path: Function path to execute
        task_params: Function parameters
    """
    try:
        execute_task_function(task_id, task_func_path, task_params, recover_running=True)
        logger.info(f"Task {task_id} recovered and executed successfully")
    except Exception as e:
        logger.error(f"Task {task_id} recovery failed: {str(e)}")


@contextmanager
def _operation_lock(key: str, *, blocking: bool = True):
    """Coordinate API threads and the recovery process on this installation."""
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


def _claim_task(task_id: int, recover_running: bool) -> bool:
    statuses = [TaskStatus.pending]
    if recover_running:
        statuses.append(TaskStatus.running)
    with get_db() as db:
        count = db.query(Task).filter(
            Task.task_id == task_id, Task.status.in_(statuses),
        ).update({"status": TaskStatus.running, "error_msg": None, "updated_time": datetime.now()},
                 synchronize_session=False)
        db.commit()
        return count == 1


@with_log_context(task_id="task_id")
def execute_task_function(
    task_id: int,
    task_func_path: str,
    task_params: Dict[str, Any],
    *,
    recover_running: bool = False,
) -> None:
    """Run an explicitly registered operation and own its terminal task state.

    The task lock prevents recovery from replaying a still-running API task.
    The resource lock serializes deploy/delete actions for the same cluster.
    OS locks are released after crashes, so interrupted work can be reclaimed.
    """
    from core.orm.cluster import find_cluster_by_id

    with _operation_lock(f"task-{int(task_id)}", blocking=False) as acquired:
        if not acquired:
            return
        with get_db() as db:
            row = db.get(Task, task_id)
            if row is None:
                return
            task_func_path = row.task_func_path
            task_params = dict(row.params) if isinstance(row.params, dict) else {}
        cluster_id = task_params.get("cluster_id")
        # Invalid/legacy work must fail without touching a resource.
        lock_key = f"cluster-{cluster_id}" if isinstance(cluster_id, int) else f"invalid-task-{task_id}"
        with _operation_lock(lock_key, blocking=False) as resource_acquired:
            if not resource_acquired or not _claim_task(task_id, recover_running):
                return
            try:
                if task_func_path == "api.app.deploy_app":
                    raise ValueError("Legacy task operation is ambiguous; resources retained for manual reconciliation")
                if task_func_path not in _TASK_HANDLERS:
                    raise ValueError(f"Unknown task operation: {task_func_path}")
                if not isinstance(cluster_id, int) or isinstance(cluster_id, bool) or cluster_id <= 0:
                    raise ValueError("Task requires a positive cluster_id")
                if not task_params.get("resource_uid") or not task_params.get("helm_name"):
                    raise ValueError("旧任务缺少资源身份，拒绝自动执行；请核实资源后重新提交")
                cluster = find_cluster_by_id(cluster_id)
                if cluster is None and _TASK_HANDLERS[task_func_path][1] == "clean_up_cluster":
                    update_task_record_status(task_id, TaskStatus.success)
                    return
                if (cluster is None or cluster.resource_uid != task_params["resource_uid"]
                        or cluster.helm_name != task_params["helm_name"]):
                    raise ValueError("任务对应的资源身份已变化，拒绝操作当前资源")
                module_name, function_name = _TASK_HANDLERS[task_func_path]
                task_func = getattr(importlib.import_module(module_name), function_name)
                task_func(task_id, cluster_id=cluster_id)
                update_task_record_status(task_id, TaskStatus.success)
                logger.info("Task %s completed successfully", task_id)
            except Exception as exc:
                update_task_record_status(task_id, TaskStatus.failed, str(exc))
                logger.exception("Task %s failed", task_id)
                raise
