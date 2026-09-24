"""Liveness and dependency-aware readiness checks."""
import os
import time

from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from core.config.application import Application
from core.image_import_worker import WORKER_HEARTBEAT_PATH
from core.orm.engine import engine

router = APIRouter()


@router.get("/health")
async def health_check() -> dict[str, str]:
    """Process liveness only; deliberately has no external dependencies."""
    return {"status": "healthy", "service": Application.DOMAIN}


@router.get("/ready")
def readiness_check() -> dict[str, str]:
    """Report whether this instance can accept production API traffic."""
    failures: list[str] = []
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        failures.append(f"database: {exc}")

    task_dir = os.path.join(Application.ROOT_DIR, "tmp", "image-imports")
    try:
        os.makedirs(task_dir, exist_ok=True)
        if not os.access(task_dir, os.W_OK):
            failures.append("image import storage is not writable")
    except OSError as exc:
        failures.append(f"image import storage: {exc}")

    try:
        heartbeat_age = time.time() - WORKER_HEARTBEAT_PATH.stat().st_mtime
        if heartbeat_age > 15:
            failures.append(f"image worker heartbeat is {heartbeat_age:.0f}s old")
    except OSError:
        failures.append("image worker heartbeat is missing")

    if failures:
        raise HTTPException(status_code=503, detail={"status": "not_ready", "failures": failures})
    return {"status": "ready", "service": Application.DOMAIN}
