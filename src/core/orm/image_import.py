"""Persistent records for offline image import tasks."""

from datetime import datetime, timedelta
import fcntl
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, String, Text, desc, inspect, text
)
from sqlalchemy.orm import Query, Session, relationship

from core.orm.engine import Base, get_db
from core.runtime_files import private_runtime_file


from core.task_runtime import TaskLease as ImageImportLease, TaskLeaseLost
from core.orm.task_lease import owned_query, renew_lease


class ImageImportLeaseLost(TaskLeaseLost):
    """The execution attempt no longer owns a live task lease."""


class ImageImportTask(Base):
    """One uploaded offline image archive."""

    __tablename__ = "image_import_task"

    task_id = Column(Integer, primary_key=True, index=True)
    filename = Column(String, nullable=False)
    content_type = Column(String)
    file_path = Column(String, nullable=False)
    file_size = Column(Integer, default=0, nullable=False)
    status = Column(String, default="pending", nullable=False, index=True)
    total_count = Column(Integer, default=0, nullable=False)
    success_count = Column(Integer, default=0, nullable=False)
    failed_count = Column(Integer, default=0, nullable=False)
    error_message = Column(Text)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
    )
    completed_at = Column(DateTime)
    lease_owner = Column(String)
    lease_expires_at = Column(DateTime, index=True)
    heartbeat_at = Column(DateTime)
    attempt_count = Column(Integer, default=0, nullable=False)
    retry_failed = Column(Boolean, default=False, nullable=False)
    cleanup_pending = Column(Boolean, default=False, nullable=False)
    cleanup_error = Column(Text)
    cleanup_retry_at = Column(DateTime)
    namespace = Column(String)


    items = relationship(
        "ImageImportItem",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="ImageImportItem.item_id",
    )


class ImageImportItem(Base):
    """Result of importing and pushing one image from an archive."""

    __tablename__ = "image_import_item"

    item_id = Column(Integer, primary_key=True, index=True)
    task_id = Column(
        Integer,
        ForeignKey("image_import_task.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    image_ref = Column(String, nullable=False)
    registry = Column(String, nullable=False)
    repository = Column(String, nullable=False)
    tag = Column(String, nullable=False)
    status = Column(String, default="pending", nullable=False, index=True)
    stage = Column(String, default="pending", nullable=False)
    error_message = Column(Text)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
    )

    task = relationship("ImageImportTask", back_populates="items")


def _item_to_dict(item: ImageImportItem) -> Dict[str, Any]:
    return {
        "item_id": item.item_id,
        "task_id": item.task_id,
        "image_ref": item.image_ref,
        "registry": item.registry,
        "repository": item.repository,
        "tag": item.tag,
        "status": item.status,
        "stage": item.stage,
        "error_message": item.error_message,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _task_to_dict(
    task: ImageImportTask,
    include_items: bool = False,
    include_file_path: bool = False,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "task_id": task.task_id,
        "filename": task.filename,
        "content_type": task.content_type,
        "file_size": task.file_size,
        "status": task.status,
        "total_count": task.total_count,
        "success_count": task.success_count,
        "failed_count": task.failed_count,
        "error_message": task.error_message,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "completed_at": task.completed_at,
        "heartbeat_at": task.heartbeat_at,
        "attempt_count": task.attempt_count,
        "cleanup_pending": task.cleanup_pending,
        "cleanup_error": task.cleanup_error,
    }
    if include_file_path:
        result["file_path"] = task.file_path
        result["namespace"] = task.namespace or "apps"
    if include_items:
        result["items"] = [_item_to_dict(item) for item in task.items]
    return result


def create_image_import_task(
    filename: str,
    content_type: Optional[str],
    file_path: str,
) -> Dict[str, Any]:
    with get_db() as db:
        task = ImageImportTask(
            filename=filename,
            content_type=content_type,
            file_path=file_path,
            # The durable worker must not claim the row until the potentially
            # large archive has been copied into its final path.
            status="uploading",
        )
        db.add(task)
        db.flush()
        task.namespace = f"kubengine-import-{task.task_id}"
        db.commit()
        db.refresh(task)
        return _task_to_dict(task)


def ensure_image_import_schema() -> None:
    """Add worker lease columns when upgrading an existing SQLite database."""
    from core.orm.engine import engine

    additions = {
        "lease_owner": "VARCHAR",
        "lease_expires_at": "DATETIME",
        "heartbeat_at": "DATETIME",
        "attempt_count": "INTEGER NOT NULL DEFAULT 0",
        "retry_failed": "BOOLEAN NOT NULL DEFAULT 0",
        "cleanup_pending": "BOOLEAN NOT NULL DEFAULT 0",
        "cleanup_error": "TEXT",
        "cleanup_retry_at": "DATETIME",
        "namespace": "VARCHAR",
    }
    # Multiple Uvicorn workers start concurrently. Serialize the lightweight
    # SQLite compatibility migration across processes.
    with private_runtime_file("image-import-schema.lock") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        existing = {
            column["name"]
            for column in inspect(engine).get_columns("image_import_task")
        }
        with engine.begin() as connection:
            for name, sql_type in additions.items():
                if name not in existing:
                    connection.execute(
                        text(
                            f"ALTER TABLE image_import_task ADD COLUMN {name} {sql_type}"
                        )
                    )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS "
                    "ix_image_import_task_lease_expires_at "
                    "ON image_import_task (lease_expires_at)"
                )
            )


def claim_next_image_import_task(
    worker_id: str, lease_seconds: int
) -> Optional[Dict[str, Any]]:
    """Atomically claim one pending task or a task whose worker lease expired."""
    with get_db() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        now = datetime.now()
        expires_at = now + timedelta(seconds=lease_seconds)
        task = (
            db.query(ImageImportTask)
            .filter(
                (ImageImportTask.status == "pending")
                | (
                    (ImageImportTask.status == "processing")
                    & (
                        (ImageImportTask.lease_expires_at.is_(None))
                        | (ImageImportTask.lease_expires_at < now)
                    )
                )
            )
            .order_by(ImageImportTask.created_at, ImageImportTask.task_id)
            .first()
        )
        if task is None:
            db.commit()
            return None
        task.status = "processing"
        task.lease_owner = worker_id
        task.lease_expires_at = expires_at
        task.heartbeat_at = now
        task.attempt_count = int(task.attempt_count or 0) + 1
        task.updated_at = now
        # A crashed attempt may leave items at the push stage. They are work to
        # resume, not successful items and not permanently in-progress results.
        db.query(ImageImportItem).filter(
            ImageImportItem.task_id == task.task_id,
            ImageImportItem.status == "processing",
        ).update({"status": "pending", "stage": "recover", "updated_at": now})
        db.commit()
        db.refresh(task)
        result = _task_to_dict(task, include_items=False, include_file_path=True)
        result["retry_failed"] = bool(task.retry_failed)
        result["lease_owner"] = worker_id
        return result


def renew_image_import_lease(
    task_id: int, worker_id: str, lease_seconds: int, attempt_count: int
) -> bool:
    """Renew a task lease if it is still owned by this worker."""
    return renew_lease(ImageImportTask, task_id, ImageImportLease(worker_id, attempt_count),
                       "processing", lease_seconds, "updated_at")


def requeue_image_import_task(task_id: int) -> bool:
    """Atomically queue a completed task for retry without duplicate scheduling."""
    with get_db() as db:
        updated = (
            db.query(ImageImportTask)
            .filter(
                ImageImportTask.task_id == task_id,
                ImageImportTask.status.in_(["failed", "success", "partial_success"]),
                ImageImportTask.cleanup_pending == False,
                ImageImportTask.file_path != "",
            )
            .update(
                {
                    "status": "pending",
                    "retry_failed": True,
                    "completed_at": None,
                    "error_message": None,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "updated_at": datetime.now(),
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return bool(updated == 1)


def request_cleanup_retry(task_id: int) -> None:
    with get_db() as db:
        db.query(ImageImportTask).filter(
            ImageImportTask.task_id == task_id, ImageImportTask.cleanup_pending == True,
            ImageImportTask.status.in_(["failed", "partial_success", "success"]),
        ).update({"cleanup_retry_at": datetime.now()}, synchronize_session=False)
        db.commit()


def pending_cleanup_tasks():
    with get_db() as db:
        return [_task_to_dict(task, include_file_path=True) for task in db.query(ImageImportTask).filter(
            ImageImportTask.cleanup_pending == True,
            ImageImportTask.status.in_(["failed", "partial_success", "success"]),
            ImageImportTask.cleanup_retry_at <= datetime.now(),
        ).order_by(ImageImportTask.cleanup_retry_at, ImageImportTask.task_id).limit(1).all()]


def record_cleanup_result(task_id: int, attempt: int, error: Optional[str]):
    with get_db() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        task = db.query(ImageImportTask).filter(
            ImageImportTask.task_id == task_id, ImageImportTask.attempt_count == attempt,
            ImageImportTask.cleanup_pending == True,
            ImageImportTask.status.in_(["failed", "partial_success", "success"]),
        ).first()
        if task is None:
            return
        if not error and task.error_message == task.cleanup_error:
            task.error_message = None
        task.cleanup_pending = bool(error)
        task.cleanup_error = error
        task.cleanup_retry_at = datetime.now() + timedelta(seconds=60) if error else None
        if not error:
            if task.items and all(item.status == "success" for item in task.items):
                task.status = "success"
            elif any(item.status == "success" for item in task.items):
                task.status = "partial_success"
            else:
                task.status = "failed"
        task.updated_at = datetime.now()
        db.commit()


def _owned_task_query(
    db: Session, task_id: int, lease: ImageImportLease
) -> Query[ImageImportTask]:
    return owned_query(db, ImageImportTask, task_id, lease, "processing")


def assert_image_import_lease(task_id: int, lease: ImageImportLease) -> None:
    """Fail before another external step if this attempt has expired or moved."""
    with get_db() as db:
        if _owned_task_query(db, task_id, lease).first() is None:
            raise ImageImportLeaseLost(f"Image-import task {task_id} lease was lost")


def update_image_import_task(
    task_id: int, *, lease: Optional[ImageImportLease] = None, **values: Any
) -> None:
    with get_db() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        # Only an unfinished upload may be written without a worker identity.
        # A late upload/error callback must never overwrite a claimed task.
        query = (
            _owned_task_query(db, task_id, lease) if lease is not None
            else db.query(ImageImportTask).filter_by(task_id=task_id, status="uploading")
        )
        updated = query.update(
            {**values, "updated_at": datetime.now()}, synchronize_session=False
        )
        if updated != 1:
            raise ImageImportLeaseLost(f"Image-import task {task_id} is no longer owned")
        db.commit()


def find_image_import_task(
    task_id: int,
    include_items: bool = True,
    include_file_path: bool = False,
) -> Optional[Dict[str, Any]]:
    with get_db() as db:
        task = db.query(ImageImportTask).filter_by(task_id=task_id).first()
        return _task_to_dict(task, include_items, include_file_path) if task else None


def find_image_import_tasks(page: int, page_size: int) -> Dict[str, Any]:
    with get_db() as db:
        query = db.query(ImageImportTask).order_by(desc(ImageImportTask.created_at))
        total = query.count()
        tasks = query.offset((page - 1) * page_size).limit(page_size).all()
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "data": [_task_to_dict(task) for task in tasks],
        }


def replace_image_import_items(
    task_id: int, image_refs: List[Dict[str, str]], *, lease: ImageImportLease
) -> None:
    with get_db() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        if _owned_task_query(db, task_id, lease).first() is None:
            raise ImageImportLeaseLost(f"Image-import task {task_id} lease was lost")
        db.query(ImageImportItem).filter_by(task_id=task_id).delete()
        for image in image_refs:
            db.add(ImageImportItem(task_id=task_id, **image))
        db.commit()


def update_image_import_item(
    task_id: int,
    image_ref: str,
    status: str,
    stage: str,
    error_message: Optional[str] = None,
    *,
    lease: ImageImportLease,
) -> None:
    with get_db() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        if _owned_task_query(db, task_id, lease).first() is None:
            raise ImageImportLeaseLost(f"Image-import task {task_id} lease was lost")
        item = (
            db.query(ImageImportItem)
            .filter_by(task_id=task_id, image_ref=image_ref)
            .first()
        )
        if item is None:
            return
        item.status = status
        item.stage = stage
        item.error_message = error_message
        item.updated_at = datetime.now()
        db.commit()
