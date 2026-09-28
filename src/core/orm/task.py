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
from sqlalchemy.orm import Query

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
    task_func_path: str,
    params: Dict[str, Any],
    resource_id: int
) -> TaskSchema:
    """Create a new task record in database.

    Args:
        task_func_path: Path to task function to execute
        params: Parameters for task execution
        resource_id: Associated resource ID

    Returns:
        Created task schema with generated ID

    Raises:
        Exception: When database operation fails
    """
    if task_func_path not in _TASK_HANDLERS:
        raise ValueError(f"Unknown task operation: {task_func_path}")
    try:
        with get_db() as db:
            task = Task(
                task_func_path=task_func_path,
                params=params,
                resource_id=resource_id,
                status=TaskStatus.pending
            )
            db.add(task)
            db.commit()
            db.refresh(task)

            logger.info(
                f"Task created: ID {task.task_id}, function {task_func_path}")
            return TaskSchema.model_validate(task)

    except Exception as e:
        logger.error(f"Failed to create task: {str(e)}")
        raise


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

            logger.info(f"Found {len(tasks)} unfinished tasks")
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
    with _operation_lock(f"task-{int(task_id)}", blocking=False) as acquired:
        if not acquired:
            return
        if not _claim_task(task_id, recover_running):
            return
        try:
            if task_func_path == "api.app.deploy_app":
                raise ValueError(
                    "Legacy task operation is ambiguous (deployment or deletion). "
                    "Resources and management records are retained; reconcile the release manually."
                )
            if task_func_path not in _TASK_HANDLERS:
                raise ValueError(f"Unknown task operation: {task_func_path}")
            cluster_id = task_params.get("cluster_id")
            if not isinstance(cluster_id, int) or isinstance(cluster_id, bool) or cluster_id <= 0:
                raise ValueError("Task requires a positive cluster_id")
            module_name, function_name = _TASK_HANDLERS[task_func_path]
            task_func = getattr(importlib.import_module(module_name), function_name)
            with _operation_lock(f"cluster-{cluster_id}"):
                task_func(task_id, **task_params)
            update_task_record_status(task_id, TaskStatus.success)
            logger.info("Task %s completed successfully", task_id)
        except Exception as exc:
            update_task_record_status(task_id, TaskStatus.failed, str(exc))
            logger.exception("Task %s failed", task_id)
            raise
