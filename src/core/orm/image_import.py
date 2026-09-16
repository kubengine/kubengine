"""Persistent records for offline image import tasks."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, desc
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
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        return _task_to_dict(task)


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
