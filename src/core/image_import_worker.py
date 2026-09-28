"""
Unified durable worker for applications and offline image-import jobs.

The API only persists work. This process owns execution, renews a
database lease while a job is running, and can reclaim jobs after a
crash.
"""

import os
import signal
import socket
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from typing import Dict
from uuid import uuid4

from core.logger import get_logger, with_log_context

# Register the shared notification table before creating ORM metadata.
from core.orm import notifications  # noqa: F401
from core.orm.cluster import ensure_cluster_schema
from core.orm.engine import Base, engine
from core.orm.image_import import (
    ImageImportLease,
    ImageImportLeaseLost,
    claim_next_image_import_task,
    ensure_image_import_schema,
    renew_image_import_lease,
    update_image_import_task,
)
from core.orm.task import ensure_task_schema
from core.runtime_files import private_runtime_file, runtime_path
from core.task_runtime import lease_heartbeat
from core.task_worker import AppTaskWorker

logger = get_logger(__name__)
WORKER_HEARTBEAT_PATH = runtime_path("image-import-worker.heartbeat")


class TaskWorker:
    """
    Poll both task families with the shared lease lifecycle and bounded
    pools.
    """

    def __init__(
        self,
        concurrency: int = 1,
        poll_interval: float = 2.0,
        lease_seconds: int = 90,
    ) -> None:
        self.concurrency = max(1, concurrency)
        self.poll_interval = max(0.2, poll_interval)
        self.lease_seconds = max(30, lease_seconds)
        self.worker_id = (
            f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
        )
        self._stop = threading.Event()
        self._executor = ThreadPoolExecutor(
            max_workers=self.concurrency,
            thread_name_prefix="image-import",
        )
        self._futures: Dict[Future[None], int] = {}

    def stop(self) -> None:
        """Ask the worker to stop claiming jobs and finish active
        work.
        """
        self._stop.set()

    @with_log_context(task_id="task_id")
    def _run_task(
        self, task_id: int, retry_failed: bool, lease: ImageImportLease
    ) -> None:
        # Import lazily so the worker process, rather than every web
        # worker, loads the image-processing service and its CLI
        # dependencies.
        from web.api.artifacts import process_image_import_task

        with lease_heartbeat(
            lambda: renew_image_import_lease(
                task_id, lease.owner, self.lease_seconds, lease.attempt
            ),
            interval=max(10.0, self.lease_seconds / 3),
        ) as lease_lost:
            try:
                process_image_import_task(
                    task_id,
                    retry_failed=retry_failed,
                    lease=lease,
                    lease_lost=lease_lost,
                )
            except ImageImportLeaseLost:
                logger.warning(
                    "Stopped stale image-import attempt for task %s", task_id
                )
            except BaseException as exc:
                logger.exception(
                    "Image-import task %s escaped worker boundary", task_id
                )
                # Do not convert a process interruption into a completed
                # retry; keep its item state recoverable until a later
                # claim takes over.
                try:
                    update_image_import_task(
                        task_id,
                        lease=lease,
                        error_message=f"worker interrupted: {exc}",
                        lease_expires_at=datetime.now(),
                    )
                except ImageImportLeaseLost:
                    logger.warning(
                        "Ignored failure from stale attempt for task %s",
                        task_id,
                    )

    def run_forever(self) -> None:
        """Run until stopped, reclaiming expired leases
        automatically.
        """
        Base.metadata.create_all(bind=engine)
        ensure_image_import_schema()
        ensure_cluster_schema()
        ensure_task_schema()
        app_worker = AppTaskWorker(
            owner=self.worker_id, lease_seconds=self.lease_seconds
        )
        from core.image_maintenance import maintain_images

        maintenance_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="image-maintenance"
        )
        maintenance_future = None
        next_maintenance = 0.0
        logger.info(
            "Task worker %s started (image concurrency=%s, lease=%ss)",
            self.worker_id,
            self.concurrency,
            self.lease_seconds,
        )
        try:
            while not self._stop.is_set():
                try:
                    app_worker.poll()
                except Exception:
                    logger.exception("应用任务轮询失败，下轮重试")
                if time.monotonic() >= next_maintenance and (
                    maintenance_future is None or maintenance_future.done()
                ):
                    maintenance_future = maintenance_executor.submit(
                        maintain_images
                    )
                    next_maintenance = time.monotonic() + 30
                with private_runtime_file(
                    "image-import-worker.heartbeat", truncate=True
                ) as heartbeat:
                    heartbeat.write(self.worker_id)
                for future, task_id in list(self._futures.items()):
                    if future.done():
                        try:
                            future.result()
                        except BaseException:
                            logger.exception(
                                "Image-import future %s failed", task_id
                            )
                        del self._futures[future]

                while (
                    len(self._futures) < self.concurrency
                    and not self._stop.is_set()
                ):
                    task = claim_next_image_import_task(
                        self.worker_id, self.lease_seconds
                    )
                    if task is None:
                        break
                    task_id = int(task["task_id"])
                    future = self._executor.submit(
                        self._run_task,
                        task_id,
                        bool(task.get("retry_failed")),
                        ImageImportLease(
                            self.worker_id, int(task["attempt_count"])
                        ),
                    )
                    self._futures[future] = task_id
                    logger.info("Claimed image-import task %s", task_id)

                self._stop.wait(self.poll_interval)
        finally:
            logger.info("Task worker stopping; waiting for active jobs")
            maintenance_executor.shutdown(wait=True)
            app_worker.close()
            self._executor.shutdown(wait=True, cancel_futures=False)
            try:
                if (
                    WORKER_HEARTBEAT_PATH.read_text(encoding="utf-8")
                    == self.worker_id
                ):
                    WORKER_HEARTBEAT_PATH.unlink(missing_ok=True)
            except OSError:
                pass


# Backwards-compatible import and CLI entry for existing installations.
ImageImportWorker = TaskWorker


def run_image_import_worker(
    concurrency: int = 1,
    poll_interval: float = 2.0,
    lease_seconds: int = 90,
) -> None:
    """CLI-friendly worker entry point."""
    worker = ImageImportWorker(concurrency, poll_interval, lease_seconds)
    previous_handlers = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[sig] = signal.getsignal(sig)
        signal.signal(sig, lambda _signum, _frame: worker.stop())
    try:
        worker.run_forever()
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        # Give logging handlers a chance to flush after orderly
        # shutdown.
        time.sleep(0.1)
