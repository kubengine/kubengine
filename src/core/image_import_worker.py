"""Durable worker for offline image-import jobs.

The API only persists work. This process owns execution, renews a database
lease while a job is running, and can reclaim jobs after a crash.
"""

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
import os
import signal
import socket
import threading
import time
from typing import Dict
from uuid import uuid4

from core.logger import get_logger, with_log_context
from core.runtime_files import private_runtime_file, runtime_path
from core.orm.engine import Base, engine
from core.orm.image_import import (
    ImageImportLease,
    ImageImportLeaseLost,
    claim_next_image_import_task,
    ensure_image_import_schema,
    renew_image_import_lease,
    update_image_import_task,
)

logger = get_logger(__name__)
WORKER_HEARTBEAT_PATH = runtime_path("image-import-worker.heartbeat")


class ImageImportWorker:
    """Poll and execute image-import jobs with bounded concurrency."""

    def __init__(
        self,
        concurrency: int = 1,
        poll_interval: float = 2.0,
        lease_seconds: int = 90,
    ) -> None:
        self.concurrency = max(1, concurrency)
        self.poll_interval = max(0.2, poll_interval)
        self.lease_seconds = max(30, lease_seconds)
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
        self._stop = threading.Event()
        self._executor = ThreadPoolExecutor(
            max_workers=self.concurrency,
            thread_name_prefix="image-import",
        )
        self._futures: Dict[Future[None], int] = {}

    def stop(self) -> None:
        """Ask the worker to stop claiming jobs and finish active work."""
        self._stop.set()

    @with_log_context(task_id="task_id")
    def _heartbeat(
        self, task_id: int, lease: ImageImportLease,
        done: threading.Event, lease_lost: threading.Event,
    ) -> None:
        interval = max(10.0, self.lease_seconds / 3)
        while not done.wait(interval):
            try:
                if not renew_image_import_lease(
                    task_id, lease.owner, self.lease_seconds, lease.attempt
                ):
                    logger.warning("Lost lease for image-import task %s", task_id)
                    lease_lost.set()
                    return
            except Exception:
                logger.exception("Failed to renew lease for image-import task %s", task_id)
                # Fail closed; the durable row can be reclaimed on expiry.
                lease_lost.set()
                return

    @with_log_context(task_id="task_id")
    def _run_task(
        self, task_id: int, retry_failed: bool, lease: ImageImportLease
    ) -> None:
        # Import lazily so the worker process, rather than every web worker,
        # loads the image-processing service and its CLI dependencies.
        from web.api.artifacts import process_image_import_task

        heartbeat_done = threading.Event()
        lease_lost = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat,
            args=(task_id, lease, heartbeat_done, lease_lost),
            daemon=True,
            name=f"image-import-heartbeat-{task_id}",
        )
        heartbeat.start()
        try:
            process_image_import_task(
                task_id, retry_failed=retry_failed, lease=lease, lease_lost=lease_lost
            )
        except ImageImportLeaseLost:
            logger.warning("Stopped stale image-import attempt for task %s", task_id)
        except BaseException as exc:
            logger.exception("Image-import task %s escaped worker boundary", task_id)
            # Do not convert a process interruption into a completed retry;
            # keep its item state recoverable until a later claim takes over.
            try:
                update_image_import_task(
                    task_id, lease=lease,
                    error_message=f"worker interrupted: {exc}",
                    lease_expires_at=datetime.now(),
                )
            except ImageImportLeaseLost:
                logger.warning("Ignored failure from stale attempt for task %s", task_id)
        finally:
            heartbeat_done.set()
            heartbeat.join(timeout=2)

    def run_forever(self) -> None:
        """Run until stopped, reclaiming expired leases automatically."""
        Base.metadata.create_all(bind=engine)
        ensure_image_import_schema()
        logger.info(
            "Image-import worker %s started (concurrency=%s, lease=%ss)",
            self.worker_id,
            self.concurrency,
            self.lease_seconds,
        )
        try:
            while not self._stop.is_set():
                with private_runtime_file("image-import-worker.heartbeat", truncate=True) as heartbeat:
                    heartbeat.write(self.worker_id)
                for future, task_id in list(self._futures.items()):
                    if future.done():
                        try:
                            future.result()
                        except BaseException:
                            logger.exception("Image-import future %s failed", task_id)
                        del self._futures[future]

                while len(self._futures) < self.concurrency and not self._stop.is_set():
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
                        ImageImportLease(self.worker_id, int(task["attempt_count"])),
                    )
                    self._futures[future] = task_id
                    logger.info("Claimed image-import task %s", task_id)

                self._stop.wait(self.poll_interval)
        finally:
            logger.info("Image-import worker stopping; waiting for active jobs")
            self._executor.shutdown(wait=True, cancel_futures=False)
            try:
                if WORKER_HEARTBEAT_PATH.read_text(encoding="utf-8") == self.worker_id:
                    WORKER_HEARTBEAT_PATH.unlink(missing_ok=True)
            except OSError:
                pass


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
        # Give logging handlers a chance to flush after orderly shutdown.
        time.sleep(0.1)
