"""Bounded archive storage, crash recovery and retention."""

import fcntl
import os
import stat
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import HTTPException

from core.config import Application
from core.private_files import private_directory, private_file
from core.runtime_files import private_runtime_file
from core.upload_limits import upload_limit


@contextmanager
def archive_lock(task_id, *, blocking=True):
    with private_runtime_file(f"image-upload-{int(task_id)}.lock") as lock:
        try:
            fcntl.flock(
                lock.fileno(),
                fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB),
            )
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def archive_directory(task_id):
    return private_directory(
        Application.ROOT_DIR, "tmp", "image-imports", str(int(task_id))
    )


def remove_archive(task_id):
    # Never follow database-supplied file paths or remove links to other
    # files.
    with archive_directory(task_id) as directory:
        for name in os.listdir(directory):
            metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != os.geteuid()
            ):
                raise PermissionError("Unsafe archive storage entry")
            os.unlink(name, dir_fd=directory)
    with private_directory(
        Application.ROOT_DIR, "tmp", "image-imports"
    ) as root:
        os.rmdir(str(int(task_id)), dir_fd=root)


def store_archive(task_id, source, cancelled):
    from core.orm.image_import import update_image_import_task

    path = (
        Path(Application.ROOT_DIR)
        / "tmp"
        / "image-imports"
        / str(task_id)
        / "archive.tar"
    )
    with archive_lock(task_id):
        with private_runtime_file("image-archive-quota.lock") as quota:
            fcntl.flock(quota.fileno(), fcntl.LOCK_EX)
            try:
                update_image_import_task(task_id, file_path=str(path))
                with private_directory(
                    Application.ROOT_DIR, "tmp", "image-imports"
                ) as root:
                    total = 0
                    for name in os.listdir(root):
                        if not name.isdecimal():
                            raise PermissionError(
                                "Unexpected archive directory"
                            )
                        with archive_directory(int(name)) as directory:
                            for entry in os.listdir(directory):
                                with private_file(directory, entry) as fd:
                                    total += os.fstat(fd).st_size
                size = 0
                deadline = time.monotonic() + upload_limit("timeout_seconds")
                with archive_directory(task_id) as directory:
                    with private_file(
                        directory,
                        ".uploading",
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    ) as fd:
                        with os.fdopen(os.dup(fd), "wb") as output:
                            while True:
                                if (
                                    cancelled.is_set()
                                    or time.monotonic() >= deadline
                                ):
                                    raise RuntimeError("上传中断或保存超时")
                                chunk = source.read(1024**2)
                                if not chunk:
                                    break
                                size += len(chunk)
                                if size > upload_limit("image_bytes"):
                                    raise HTTPException(
                                        status_code=413,
                                        detail="镜像文件超过大小限制",
                                    )
                                free = os.fstatvfs(directory)
                                archive_full = total + size > upload_limit(
                                    "archive_bytes"
                                )
                                free_bytes = free.f_bavail * free.f_frsize
                                if archive_full or free_bytes < (
                                    upload_limit("reserve_bytes") + len(chunk)
                                ):
                                    raise HTTPException(
                                        status_code=507,
                                        detail="镜像归档空间不足",
                                    )
                                output.write(chunk)
                            output.flush()
                            os.fsync(output.fileno())
                    # No overwrite; the target must belong to this new
                    # upload.
                    try:
                        os.stat(
                            "archive.tar",
                            dir_fd=directory,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        pass
                    else:
                        raise FileExistsError("Archive already exists")
                    os.rename(
                        ".uploading",
                        "archive.tar",
                        src_dir_fd=directory,
                        dst_dir_fd=directory,
                    )
                    os.fsync(directory)
                if cancelled.is_set():
                    raise RuntimeError("上传已取消")
                update_image_import_task(
                    task_id,
                    file_path=str(path),
                    file_size=size,
                    status="pending",
                )
            except BaseException as exc:
                cleanup_error = None
                try:
                    remove_archive(task_id)
                except Exception as cleanup:
                    cleanup_error = str(cleanup)
                update_image_import_task(
                    task_id,
                    status="failed",
                    file_path=str(path) if cleanup_error else "",
                    error_message=f"保存上传文件失败：{exc}"
                    + (
                        f"；临时文件待回收：{cleanup_error}"
                        if cleanup_error
                        else ""
                    ),
                    completed_at=datetime.now(),
                )
                raise


def maintain_archives():
    from core.orm.engine import get_db
    from core.orm.image_import import ImageImportTask

    now = datetime.now()
    stale = now - timedelta(seconds=upload_limit("timeout_seconds"))
    expired = now - timedelta(seconds=upload_limit("retention_seconds"))
    with get_db() as db:
        ids = [
            row[0]
            for row in (
                db.query(ImageImportTask.task_id)
                .filter(
                    (
                        (ImageImportTask.status == "uploading")
                        & (ImageImportTask.created_at < stale)
                    )
                    | (
                        (
                            ImageImportTask.status.in_(
                                ["failed", "success", "partial_success"]
                            )
                        )
                        & (ImageImportTask.completed_at < expired)
                        & (ImageImportTask.file_path != "")
                        & (ImageImportTask.cleanup_pending.is_(False))
                    )
                )
                .limit(100)
                .all()
            )
        ]
    for task_id in ids:
        with archive_lock(task_id, blocking=False) as acquired:
            if not acquired:
                continue
            # Match the writer's task-lock -> quota-lock -> DB order. Do
            # not remove files while another process is scanning the
            # quota.
            with private_runtime_file("image-archive-quota.lock") as quota:
                try:
                    fcntl.flock(quota.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                with get_db() as db:
                    from sqlalchemy import text

                    db.execute(text("BEGIN IMMEDIATE"))
                    task = db.get(ImageImportTask, task_id)
                    if task.status == "uploading" and task.created_at < stale:
                        remove_archive(task_id)
                        task.status = "failed"
                        task.error_message = "上传中断，临时文件已回收"
                        task.completed_at = now
                        task.file_path = ""
                    elif (
                        task.status in {"failed", "success", "partial_success"}
                        and task.completed_at
                        and task.completed_at < expired
                        and not task.cleanup_pending
                    ):
                        remove_archive(task_id)
                        task.file_path = ""
                    db.commit()
