"""KubeEngine 发起的资源 YAML 变更记录；配置快照仅保存密文。"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from core.orm.engine import Base


class ResourceRevision(Base):
    __tablename__ = "resource_yaml_revision"
    __table_args__ = (
        UniqueConstraint("scope", "version"),
        UniqueConstraint("scope", "request_id"),
    )

    id = Column(Integer, primary_key=True)
    scope = Column(String(64), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    request_id = Column(String(64), nullable=False)
    payload_hash = Column(String(64), nullable=False)
    actor = Column(String(255), nullable=False)
    comment = Column(String(500), nullable=False, default="")
    status = Column(String(24), nullable=False, default="pending")
    resource_uid = Column(String(255), nullable=False)
    before_rv = Column(String(255), nullable=False)
    after_rv = Column(String(255), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    paths = Column(JSON, nullable=False)
    snapshots = Column(Text, nullable=False)
    error = Column(String(500), nullable=False, default="")
