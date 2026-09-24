"""Persistent records for offline image import tasks."""

from datetime import datetime, timedelta
import fcntl
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, String, Text, desc, inspect, text
)
from sqlalchemy.orm import relationship

from core.orm.engine import Base, get_db


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
    }
    if include_file_path:
        result["file_path"] = task.file_path
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
    }
    # Multiple Uvicorn workers start concurrently. Serialize the lightweight
    # SQLite compatibility migration across processes.
    with open("/tmp/kubengine-image-import-schema.lock", "w", encoding="utf-8") as lock:
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
    now = datetime.now()
    expires_at = now + timedelta(seconds=lease_seconds)
    with get_db() as db:
        db.execute(text("BEGIN IMMEDIATE"))
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
        db.commit()
        db.refresh(task)
        result = _task_to_dict(task, include_items=False, include_file_path=True)
        result["retry_failed"] = bool(task.retry_failed)
        return result


def renew_image_import_lease(
    task_id: int, worker_id: str, lease_seconds: int
) -> bool:
    """Renew a task lease if it is still owned by this worker."""
    now = datetime.now()
    with get_db() as db:
        updated = (
            db.query(ImageImportTask)
            .filter_by(task_id=task_id, lease_owner=worker_id, status="processing")
            .update(
                {
                    "heartbeat_at": now,
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                    "updated_at": now,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return bool(updated == 1)


def requeue_image_import_task(task_id: int) -> bool:
    """Atomically queue a completed task for retry without duplicate scheduling."""
    with get_db() as db:
        updated = (
            db.query(ImageImportTask)
            .filter(
                ImageImportTask.task_id == task_id,
                ImageImportTask.status.notin_(["pending", "processing"]),
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


def update_image_import_task(task_id: int, **values: Any) -> None:
    with get_db() as db:
        task = db.query(ImageImportTask).filter_by(task_id=task_id).first()
        if task is None:
            return
        for key, value in values.items():
            if hasattr(task, key):
                setattr(task, key, value)
        task.updated_at = datetime.now()
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


def replace_image_import_items(task_id: int, image_refs: List[Dict[str, str]]) -> None:
    with get_db() as db:
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
) -> None:
    with get_db() as db:
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
