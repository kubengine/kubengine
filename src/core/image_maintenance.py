"""Retry cleanup independently of pushes, then reclaim abandoned archives."""

from core.logger import get_logger
from core.orm.image_import import pending_cleanup_tasks, record_cleanup_result, find_image_import_task
from core.image_storage import maintain_archives

logger = get_logger(__name__)


def maintain_images():
    from web.api.artifacts import _image_import_lock, _cleanup_image_namespace
    try:
        with _image_import_lock(blocking=False) as acquired:
            if acquired:
                for task in pending_cleanup_tasks():
                    current = find_image_import_task(task["task_id"], include_file_path=True)
                    if (not current or current["status"] in {"uploading", "pending", "processing"}
                            or not current["cleanup_pending"] or current["attempt_count"] != task["attempt_count"]):
                        continue
                    error = _cleanup_image_namespace(current["namespace"])
                    record_cleanup_result(task["task_id"], task["attempt_count"], error)
        maintain_archives()
    except Exception:
        logger.exception("镜像资源回收失败，将在下一轮重试")
