"""Common database fences for application and image-import attempts."""

from datetime import datetime, timedelta
from sqlalchemy import text
from core.orm.engine import get_db


def owned_query(db, model, task_id, lease, running_status):
    return db.query(model).filter(
        model.task_id == task_id, model.status == running_status,
        model.lease_owner == lease.owner, model.attempt_count == lease.attempt,
        model.lease_expires_at > datetime.now(),
    )


def renew_lease(model, task_id, lease, running_status, lease_seconds, updated_field):
    with get_db() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        now = datetime.now()
        updated = owned_query(db, model, task_id, lease, running_status).update({
            "heartbeat_at": now, "lease_expires_at": now + timedelta(seconds=lease_seconds),
            updated_field: now,
        }, synchronize_session=False)
        db.commit()
        return updated == 1
