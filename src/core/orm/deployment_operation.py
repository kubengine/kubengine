"""Deployment 快捷运维的独立持久化状态与租约。"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    text,
)

from core.orm.engine import Base


class DeploymentOperation(Base):
    __tablename__ = "deployment_operation"
    __table_args__ = (UniqueConstraint("target", "request_id"),)

    id = Column(String(32), primary_key=True)
    target = Column(String(64), nullable=False, index=True)
    cluster = Column(String(64), nullable=False)
    namespace = Column(String(63), nullable=False)
    name = Column(String(253), nullable=False)
    uid = Column(String(255), nullable=False)
    request_id = Column(String(64), nullable=False)
    payload_hash = Column(String(64), nullable=False)
    action = Column(String(16), nullable=False)
    actor = Column(String(64), nullable=False)
    auth_context = Column(JSON, nullable=False)
    status = Column(String(32), nullable=False, default="pending")
    phase = Column(String(32), nullable=False, default="queued")
    write_status = Column(String(24), nullable=False, default="pending")
    before_rv = Column(String(255), nullable=False)
    before_generation = Column(Integer, nullable=False)
    target_generation = Column(Integer, nullable=False)
    before_hash = Column(String(64), nullable=False)
    desired_hash = Column(String(64), nullable=False)
    before_replicas = Column(Integer, nullable=False)
    replicas = Column(Integer, nullable=False)
    marker = Column(String(64), nullable=False, default="")
    revision_id = Column(Integer, nullable=False)
    audit_id = Column(String(32), nullable=False)
    progress = Column(JSON, nullable=False, default=dict)
    release_context = Column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    reason = Column(String(500), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    deadline = Column(DateTime, nullable=False)
    completed_at = Column(DateTime)
    lease_owner = Column(String(64), nullable=False, default="")
    lease_until = Column(DateTime)
    next_poll = Column(DateTime, nullable=False, default=datetime.utcnow)


ACTIVE = ("pending", "running", "timed_out")


def ensure_deployment_operation_schema():
    from core.orm.engine import get_db

    with get_db() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        DeploymentOperation.__table__.create(db.connection(), checkfirst=True)
        columns = {
            row[1]
            for row in db.execute(
                text("PRAGMA table_info(deployment_operation)")
            )
        }
        if "release_context" not in columns:
            db.execute(
                text(
                    "ALTER TABLE deployment_operation ADD COLUMN "
                    "release_context JSON NOT NULL DEFAULT '{}'"
                )
            )
        db.commit()
