"""Idempotent deployment submission in the same transaction as its
task.
"""

import hashlib
import json
import re

from fastapi import HTTPException
from sqlalchemy import Column, Integer, String

from core.orm.cluster import Cluster, ClusterSchema
from core.orm.engine import Base


class DeploymentSubmission(Base):
    __tablename__ = "deployment_submission"
    scope_key = Column(String(64), primary_key=True)
    payload_hash = Column(String(64), nullable=False)
    cluster_id = Column(Integer, nullable=False)
    resource_uid = Column(String(32), nullable=False)


def submission_identity(username, key, data):
    if key is None:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
        raise HTTPException(status_code=400, detail="Idempotency-Key 格式无效")
    payload = data.model_dump(
        include={
            "name",
            "app_id",
            "helm_chart",
            "helm_chart_version",
            "config",
        },
        mode="json",
    )
    try:
        body = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400, detail="部署参数必须是有效的 JSON"
        ) from None
    scope = hashlib.sha256(
        json.dumps([username, "app.deploy", key]).encode()
    ).hexdigest()
    return scope, hashlib.sha256(body.encode()).hexdigest()


def replay_submission(db, identity):
    if identity is None:
        return None
    row = db.get(DeploymentSubmission, identity[0])
    if row is None:
        return None
    if row.payload_hash != identity[1]:
        raise HTTPException(
            status_code=409, detail="该幂等键已用于不同的部署参数"
        )
    cluster = db.get(Cluster, row.cluster_id)
    if cluster is None or cluster.resource_uid != row.resource_uid:
        raise HTTPException(
            status_code=410,
            detail="该请求对应的应用已删除，请为新部署使用新的幂等键",
        )
    return ClusterSchema.model_validate(cluster)


def record_submission(db, identity, cluster):
    if identity is not None:
        db.add(
            DeploymentSubmission(
                scope_key=identity[0],
                payload_hash=identity[1],
                cluster_id=cluster.cluster_id,
                resource_uid=cluster.resource_uid,
            )
        )
