"""应用层追加审计。只存经过白名单选择的元信息，不存业务请求正文。"""

import time
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import Column, Integer, String, text

from core.orm.engine import Base, get_db


class AuditEvent(Base):
    __tablename__ = "audit_event"
    event_id = Column(String(32), primary_key=True)
    created_at = Column(Integer, nullable=False, index=True)
    completed_at = Column(Integer)
    actor_id = Column(String(64), nullable=False, index=True)
    username = Column(String(64), nullable=False, index=True)
    action = Column(String(100), nullable=False, index=True)
    namespace = Column(String(63), nullable=False, default="", index=True)
    resource_type = Column(String(100), nullable=False, default="")
    resource_name = Column(String(255), nullable=False, default="", index=True)
    source_ip = Column(String(255), nullable=False, default="")
    request_id = Column(String(128), nullable=False, index=True)
    result = Column(String(32), nullable=False, index=True)
    status_code = Column(Integer)
    task_id = Column(String(64), nullable=False, default="")
    batch_id = Column(String(128), nullable=False, default="")
    revision_id = Column(String(64), nullable=False, default="")
    parent_id = Column(String(32), nullable=False, default="")


def begin_event(
    *,
    actor_id="",
    username="",
    action,
    namespace="",
    resource_type="",
    resource_name="",
    source_ip="",
    request_id="",
    parent_id="",
    task_id="",
    batch_id="",
):
    try:
        with get_db() as db:
            # 支持单独挂载 router 的客户端，建表同样受 SQLite 跨进程写锁保护。
            db.execute(text("BEGIN IMMEDIATE"))
            AuditEvent.__table__.create(db.connection(), checkfirst=True)
            row = AuditEvent(
                event_id=uuid4().hex,
                created_at=int(time.time()),
                actor_id=str(actor_id or "")[:64],
                username=str(username or "")[:64],
                action=action[:100],
                namespace=str(namespace or "")[:63],
                resource_type=str(resource_type or "")[:100],
                resource_name=str(resource_name or "")[:255],
                source_ip=str(source_ip or "")[:255],
                request_id=str(request_id or uuid4().hex)[:128],
                result="pending",
                parent_id=parent_id or "",
                task_id=str(task_id or ""),
                batch_id=batch_id or "",
            )
            db.add(row)
            event_id = row.event_id
            db.commit()
            return event_id
    except Exception as exc:
        raise HTTPException(503, "无法建立操作审计，操作尚未执行") from exc


def finish_event(
    event_id,
    status=200,
    *,
    result=None,
    task_id="",
    batch_id="",
    revision_id="",
):
    try:
        with get_db() as db:
            row = db.get(AuditEvent, event_id)
            if row is None or row.result != "pending":
                raise ValueError("Audit event missing or already closed")
            row.completed_at = int(time.time())
            row.status_code = status
            row.result = result or (
                "success"
                if 200 <= status < 300
                else "denied" if status in {401, 403} else "failed"
            )
            if task_id:
                row.task_id = str(task_id)[:64]
            if batch_id:
                row.batch_id = str(batch_id)[:128]
            if revision_id:
                row.revision_id = str(revision_id)[:64]
            db.commit()
    except Exception as exc:
        raise HTTPException(
            503,
            {
                "message": "操作结果待核实，请使用请求编号查询审计与资源状态，勿直接重试",
                "audit_id": event_id,
                "error_code": "AUDIT_FINALIZE_UNCERTAIN",
            },
        ) from exc


def query_events(page=1, page_size=20, **filters):
    with get_db() as db:
        query = db.query(AuditEvent)
        for key in (
            "username",
            "actor_id",
            "action",
            "namespace",
            "resource_type",
            "resource_name",
            "result",
            "request_id",
            "task_id",
            "batch_id",
        ):
            if filters.get(key):
                query = query.filter(getattr(AuditEvent, key) == filters[key])
        if filters.get("since") is not None:
            query = query.filter(AuditEvent.created_at >= filters["since"])
        if filters.get("until") is not None:
            query = query.filter(AuditEvent.created_at <= filters["until"])
        total = query.count()
        rows = (
            query.order_by(
                AuditEvent.created_at.desc(), AuditEvent.event_id.desc()
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return {
            "total": total,
            "items": [
                {
                    column.name: getattr(row, column.name)
                    for column in AuditEvent.__table__.columns
                }
                for row in rows
            ],
        }
