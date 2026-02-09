"""Task management ORM models and execution utilities.

This module defines the task table model and provides utilities for
creating, updating, and executing background tasks with security controls.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel
from sqlalchemy import JSON, Column, DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Query

from core.logger import get_logger
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


# Security whitelist for allowed task function paths
_ALLOWED_TASK_FUNCTIONS = {
    "web.api.app.deploy_app",
    "web.api.app.update_app",
    "web.api.app.delete_app",
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
                if error_msg is not None:
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
                if task.task_id and task.task_func_path and task.params:
                    executor.submit(
                        _execute_and_log_task,
                        task.task_id,
                        task.task_func_path,
                        task.params
                    )

    except Exception as e:
        logger.error(f"Failed to recover unfinished tasks: {str(e)}")
        raise


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
        execute_task_function(task_id, task_func_path, task_params)
        logger.info(f"Task {task_id} recovered and executed successfully")
    except Exception as e:
        logger.error(f"Task {task_id} recovery failed: {str(e)}")


def execute_task_function(
    task_id: int,
    task_func_path: str,
    task_params: Dict[str, Any]
) -> None:
    """Dynamically execute specified task function with security validation.

    Args:
        task_id: Task ID for status tracking
        task_func_path: Task function path (e.g., "task_functions.run_demo_task")
        task_params: Task function parameters

    Raises:
        ValueError: When function path is invalid or unauthorized
        ImportError: When module or function cannot be loaded
        Exception: When function execution fails
    """
    # 1. Security validation: check against whitelist
    if task_func_path not in _ALLOWED_TASK_FUNCTIONS:
        error_msg = f"Unauthorized function execution attempt: {task_func_path}"
        logger.error(error_msg)
        update_task_record_status(task_id, TaskStatus.failed, error_msg)
        raise ValueError(error_msg)

    # 2. Parse module and function name
    try:
        module_name, func_name = task_func_path.rsplit(".", 1)
    except ValueError:
        error_msg = f"Invalid function path format: {task_func_path}"
        logger.error(error_msg)
        update_task_record_status(task_id, TaskStatus.failed, error_msg)
        raise ValueError(error_msg)

    # 3. Dynamic module and function import
    try:
        module = __import__(module_name, fromlist=[func_name])
        task_func = getattr(module, func_name)
    except (ImportError, AttributeError) as e:
        error_msg = f"Failed to load function {task_func_path}: {str(e)}"
        logger.error(error_msg)
        update_task_record_status(task_id, TaskStatus.failed, error_msg)
        raise ImportError(error_msg)

    # 4. Update status to running before execution
    update_task_record_status(task_id, TaskStatus.running)

    # 5. Execute task function
    try:
        logger.info(f"Executing task {task_id}: {task_func_path}")
        task_func(task_id, **task_params)

        # Mark as successful
        update_task_record_status(task_id, TaskStatus.success)
        logger.info(f"Task {task_id} completed successfully")

    except Exception as e:
        error_msg = f"Task function execution failed: {str(e)}"
        logger.error(error_msg)
        update_task_record_status(task_id, TaskStatus.failed, error_msg)
        raise
