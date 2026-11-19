"""本机受限审计归档：先持久化与校验，随后才可清理数据库记录。"""

import hashlib
import json
import os
import time
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text

from core.config import Application, ConfigDict
from core.orm.audit import AuditEvent
from core.orm.engine import get_db
from core.private_files import private_directory, private_file


def archive_audit(*, prune=False, days=None):
    configured = (
        ConfigDict.get_instance().get("audit", {}).get("retention_days", 90)
    )
    days = int(configured if days is None else days)
    if not 1 <= days <= 3650:
        raise ValueError("审计保留天数必须为 1～3650")
    cutoff = int(time.time()) - days * 86400
    name = f"audit-{cutoff}-{uuid4().hex}.jsonl"
    with private_directory(
        Application.ROOT_DIR, "config", "audit-archives"
    ) as directory:
        with get_db() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            query = db.query(AuditEvent).filter(
                AuditEvent.created_at < cutoff, AuditEvent.result != "pending"
            )
            rows = query.order_by(
                AuditEvent.created_at, AuditEvent.event_id
            ).all()
            payload = b"".join(
                (
                    json.dumps(
                        {
                            column.name: getattr(row, column.name)
                            for column in AuditEvent.__table__.columns
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                ).encode()
                for row in rows
            )
            digest = hashlib.sha256(payload).hexdigest()
            with private_file(
                directory, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
            ) as fd:
                with os.fdopen(os.dup(fd), "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            with private_file(directory, name) as fd:
                with os.fdopen(os.dup(fd), "rb") as stream:
                    assert hashlib.sha256(stream.read()).hexdigest() == digest
            with private_file(
                directory,
                name + ".sha256",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            ) as fd:
                os.write(fd, f"{digest}  {name}\n".encode())
                os.fsync(fd)
            os.fsync(directory)
            if prune:
                query.delete(synchronize_session=False)
            db.commit()
    return {
        "path": str(
            Path(Application.ROOT_DIR) / "config/audit-archives" / name
        ),
        "sha256": digest,
        "count": len(rows),
        "pruned": prune,
        "retention_days": days,
    }
