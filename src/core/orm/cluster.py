"""Cluster ORM models and database operations.

This module defines the cluster table model and provides database
operations for creating, reading, updating, and deleting application
clusters.
"""

import enum
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import HTTPException
from pydantic import BaseModel, field_serializer
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Enum,
    Integer,
    String,
    asc,
    desc,
    inspect,
    text,
)
from sqlalchemy.orm import Session

from core.logger import get_logger
from core.misc.properties import convert_dot_notation_to_dict, split_key_levels
from core.orm.app import App, find_application_by_id
from core.orm.engine import Base, get_db

logger = get_logger(__name__)


class ClusterStatus(enum.Enum):
    """Enumeration for cluster status values."""

    pending = "pending"  # Pending creation
    creating = "creating"  # Creating resources
    checking = "checking"  # Health checking
    cleaning = "cleaning"  # Resource cleanup in progress
    healthy = "healthy"  # Running healthy
    unhealthy = "unhealthy"  # Unhealthy state
    anomaly = "anomaly"  # Anomaly state


class Cluster(Base):
    """Cluster table model representing application deployment
    clusters.
    """

    __tablename__ = "cluster"

    cluster_id = Column(
        Integer, primary_key=True, index=True, comment="Cluster ID"
    )
    resource_uid = Column(
        String(32), nullable=False, default=lambda: uuid.uuid4().hex
    )
    operation_version = Column(Integer, nullable=False, default=0)
    name = Column(String, nullable=False, comment="Cluster name")
    helm_chart = Column(String, nullable=False, comment="Helm chart template")
    helm_chart_version = Column(
        String, nullable=False, comment="Helm chart template version"
    )
    helm_name = Column(String, nullable=False, comment="Helm install name")
    config = Column(JSON, nullable=True, comment="Submitted form data")
    helm_config = Column(JSON, nullable=True, comment="Helm values data")
    status = Column(
        Enum(ClusterStatus),
        default=ClusterStatus.pending,
        nullable=False,
        comment="Cluster status",
    )
    create_time = Column(
        DateTime, default=datetime.now, comment="Creation timestamp"
    )
    updated_time = Column(
        DateTime,
        default=datetime.now,
        onupdate=datetime.now,
        comment="Last update timestamp",
    )


class ClusterSchema(BaseModel):
    """Pydantic model for cluster serialization."""

    cluster_id: Optional[int] = None
    resource_uid: Optional[str] = None
    operation_version: int = 0
    name: Optional[str] = None
    helm_chart: Optional[str] = None
    helm_chart_version: Optional[str] = None
    helm_name: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    helm_config: Optional[Dict[str, Any]] = None
    status: Optional[str] = None
    create_time: Optional[datetime] = None
    updated_time: Optional[datetime] = None

    # Helper field for cluster creation
    app_id: Optional[int] = None

    class Config:
        from_attributes = True
        arbitrary_types_allowed = True

    @field_serializer("create_time", "updated_time")
    def serialize_datetime(self, dt: datetime, _info: Any) -> Optional[str]:
        """Serialize datetime to ISO format string."""
        return dt.isoformat() if dt else ""


def _merge_configurations(
    target: Dict[str, Any], source: Dict[str, Any]
) -> None:
    """Recursively merge source configuration into target.

    Args:
        target: Target dictionary to merge into
        source: Source dictionary to merge from
    """
    for key, value in source.items():
        if (
            key in target
            and isinstance(target[key], dict)
            and isinstance(value, dict)
        ):
            # Recursively merge nested configurations
            _merge_configurations(target[key], value)  # type: ignore
        else:
            # Override target with source value
            target[key] = value


def _format_helm_value(helm_type: str, helm_unit: str, value: Any) -> Any:
    """Format Helm configuration value based on type and unit.

    Args:
        helm_type: Expected value type
        helm_unit: Unit suffix (e.g., 'Mi', 'Gi')
        value: Value to format

    Returns:
        Formatted value
    """
    if helm_unit and helm_type != "array":
        return f"{value}{helm_unit}"

    try:
        if helm_type == "boolean":
            return bool(value)
        elif helm_type == "number":
            return int(value)
        elif helm_type == "array":
            if isinstance(value, str):
                return [v.strip() for v in value.split(",") if v.strip()]
            return value if isinstance(value, list) else [value]
        return value
    except (ValueError, TypeError):
        return value


def build_helm_config(
    cluster_schema: ClusterSchema, *, db: Optional[Session] = None
) -> Dict[str, Any]:
    """Build Helm configuration from application field configurations.

    Args:
        cluster_schema: Cluster schema containing app configuration

    Returns:
        Assembled Helm configuration dictionary
    """
    helm_config: Dict[str, Any] = {}

    if cluster_schema.app_id is None:
        return helm_config

    app = (
        db.get(App, cluster_schema.app_id)
        if db is not None
        else find_application_by_id(cluster_schema.app_id)
    )
    if not app:
        return helm_config

    # Build Helm config from app field configurations
    for field in app.app_field_configs or []:
        if (
            field.name
            and cluster_schema.config
            and field.name in cluster_schema.config
            and field.helm_props is not None
        ):

            helm_keys = field.helm_props.get("keys", [])
            for helm_key in helm_keys:
                value = _format_helm_value(
                    field.helm_props.get("type", "string"),
                    field.helm_props.get("unit", ""),
                    cluster_schema.config[field.name],
                )

                # Handle array values (list) vs scalar values
                # (string/number/boolean)
                if isinstance(value, list):
                    # Array type: traverse dot notation and assign list
                    # directly. Honor [[ ]] escapes so flat keys with
                    # dots are preserved.
                    sub = helm_config
                    keys = split_key_levels(helm_key)
                    for k in keys[:-1]:
                        sub = sub.setdefault(k, {})
                    sub[keys[-1]] = value
                else:
                    # Scalar: convert dot notation string to nested dict
                    # and merge
                    key_config = convert_dot_notation_to_dict(
                        f"{helm_key}={value}"
                    )
                    _merge_configurations(helm_config, key_config)

    return helm_config


def create_cluster(
    cluster_schema: ClusterSchema, *, db: Optional[Session] = None
) -> ClusterSchema:
    """Create a new cluster record.

    Args:
        cluster_schema: Cluster data for creation

    Returns:
        Created cluster schema with generated ID

    Raises:
        Exception: When database operation fails
    """
    if db is None:
        with get_db() as session:
            result = create_cluster(cluster_schema, db=session)
            session.commit()
            return result
    try:
        # Generate unique Helm name 限制在12字符以内，避免与 chart 资源名拼接后超过 K8s 63
        # 字符 label 限制 （如 elasticsearch:
        # {name}-elasticsearch-coordinating-{hash}） 首字符必须为字母：K8s 资源名遵循
        # DNS-1035 规范（如 8d22297b-etcd 会因 以数字开头被拒绝），把首位的 0-9 映射成
        # a-j，既保留随机性又满足规范
        raw = uuid.uuid4().hex[:12]
        if raw[0].isdigit():
            raw = chr(ord("a") + int(raw[0])) + raw[1:]
        helm_name = raw

        # Build Helm configuration
        helm_config = build_helm_config(cluster_schema, db=db)

        # Create cluster record
        cluster_orm = Cluster(
            name=cluster_schema.name,
            helm_chart=cluster_schema.helm_chart,
            helm_chart_version=cluster_schema.helm_chart_version,
            helm_name=helm_name,
            config=cluster_schema.config,
            helm_config=helm_config,
            create_time=cluster_schema.create_time or datetime.now(),
        )

        db.add(cluster_orm)
        db.flush()
        db.refresh(cluster_orm)

        return ClusterSchema.model_validate(cluster_orm)

    except Exception as e:
        logger.error(f"Failed to create cluster: {str(e)}")
        raise


def ensure_cluster_schema() -> None:
    """
    Assign identities to existing resources, without binding old tasks
    to them.
    """
    import fcntl

    from core.orm.engine import engine
    from core.runtime_files import private_runtime_file

    with private_runtime_file("cluster-schema.lock") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with engine.begin() as connection:
            if "resource_uid" not in {
                c["name"] for c in inspect(connection).get_columns("cluster")
            }:
                connection.execute(
                    text(
                        "ALTER TABLE cluster ADD COLUMN resource_uid"
                        " VARCHAR(32)"
                    )
                )
            if "operation_version" not in {
                c["name"] for c in inspect(connection).get_columns("cluster")
            }:
                connection.execute(
                    text(
                        "ALTER TABLE cluster ADD COLUMN operation_version"
                        " INTEGER NOT NULL DEFAULT 0"
                    )
                )
            rows = connection.execute(
                text(
                    "SELECT cluster_id FROM cluster WHERE resource_uid IS NULL"
                    " OR resource_uid = ''"
                )
            )
            for row in rows.fetchall():
                connection.execute(
                    text(
                        "UPDATE cluster SET resource_uid = :uid WHERE"
                        " cluster_id = :id"
                    ),
                    {"uid": uuid.uuid4().hex, "id": row[0]},
                )
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_cluster_resource_uid"
                    " ON cluster(resource_uid)"
                )
            )


def find_clusters_paginated(
    page: int = 1,
    page_size: int = 10,
    sort_by: str = "create_time",
    sort_order: str = "desc",
    filters: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Find clusters with pagination, sorting, and filtering.

    Args:
        page: Page number (1-based, default 1)
        page_size: Items per page (default 10)
        sort_by: Field name for sorting
        sort_order: Sort direction ('asc' or 'desc', default 'desc')
        filters: Optional filter conditions dictionary

    Returns:
        Dictionary containing total count, paginated data, current page,
        and page size
    """
    try:
        with get_db() as db:
            filter_conditions = filters or {}
            query = db.query(Cluster)

            # Apply filters
            if "name" in filter_conditions and filter_conditions["name"]:
                name_filter = f"%{filter_conditions['name']}%"
                query = query.filter(Cluster.name.like(name_filter))

            # Validate sort field to prevent SQL injection
            valid_sort_fields = ["cluster_id", "name", "create_time", "status"]
            if sort_by not in valid_sort_fields:
                sort_by = "create_time"

            # Apply sorting
            sort_direction = desc if sort_order.lower() == "desc" else asc
            sort_column = getattr(Cluster, sort_by)
            query = query.order_by(sort_direction(sort_column))

            # Count total items
            total = query.count()

            # Apply pagination
            offset = (page - 1) * page_size
            paginated_items = query.offset(offset).limit(page_size).all()

            return {
                "total": total,
                "data": [
                    ClusterSchema.model_validate(item)
                    for item in paginated_items
                ],
                "page": page,
                "page_size": page_size,
            }

    except Exception as e:
        logger.error(f"Failed to find clusters: {str(e)}")
        raise


def update_cluster_name(cluster_id: int, new_name: str) -> ClusterSchema:
    """Update cluster name.

    Args:
        cluster_id: Cluster ID to update
        new_name: New cluster name

    Returns:
        Updated cluster schema

    Raises:
        HTTPException: When cluster not found
        Exception: When database operation fails
    """
    try:
        with get_db() as db:
            cluster_orm = (
                db.query(Cluster)
                .filter(Cluster.cluster_id == cluster_id)
                .first()
            )

            if not cluster_orm:
                raise HTTPException(
                    status_code=404,
                    detail=f"Cluster with ID {cluster_id} not found",
                )

            cluster_orm.name = new_name  # type: ignore
            db.commit()
            db.refresh(cluster_orm)

            return ClusterSchema.model_validate(cluster_orm)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update cluster name: {str(e)}")
        raise


def update_cluster_status(
    cluster_id: int, status: ClusterStatus
) -> ClusterSchema:
    """Update cluster status.

    Args:
        cluster_id: Cluster ID to update
        status: New cluster status

    Returns:
        Updated cluster schema

    Raises:
        HTTPException: When cluster not found
        Exception: When database operation fails
    """
    try:
        with get_db() as db:
            from core.task_runtime import check_application_attempt

            db.execute(text("BEGIN IMMEDIATE"))
            check_application_attempt(db, cluster_id)
            cluster_orm = (
                db.query(Cluster)
                .filter(Cluster.cluster_id == cluster_id)
                .first()
            )

            if not cluster_orm:
                raise HTTPException(
                    status_code=404,
                    detail=f"Cluster with ID {cluster_id} not found",
                )

            cluster_orm.status = status  # type: ignore
            db.commit()
            db.refresh(cluster_orm)

            return ClusterSchema.model_validate(cluster_orm)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update cluster status: {str(e)}")
        raise


def find_cluster_by_id(cluster_id: int) -> Optional[ClusterSchema]:
    """Find cluster by ID.

    Args:
        cluster_id: Cluster ID to search for

    Returns:
        Cluster schema if found, None otherwise

    Raises:
        Exception: When database operation fails
    """
    try:
        with get_db() as db:
            cluster_orm = (
                db.query(Cluster)
                .filter(Cluster.cluster_id == cluster_id)
                .first()
            )

            if cluster_orm:
                return ClusterSchema.model_validate(cluster_orm)
            return None

    except Exception as e:
        logger.error(f"Failed to find cluster by ID {cluster_id}: {str(e)}")
        raise


def remove_cluster_by_id(cluster_id: int) -> bool:
    """Remove a cluster by ID.

    Args:
        cluster_id: Cluster ID to remove

    Returns:
        True if successfully removed

    Raises:
        HTTPException: When cluster not found
        Exception: When database operation fails
    """
    try:
        with get_db() as db:
            from core.task_runtime import check_application_attempt

            db.execute(text("BEGIN IMMEDIATE"))
            check_application_attempt(db, cluster_id)
            cluster_orm = (
                db.query(Cluster)
                .filter(Cluster.cluster_id == cluster_id)
                .first()
            )

            if not cluster_orm:
                raise HTTPException(
                    status_code=404,
                    detail=f"Cluster with ID {cluster_id} not found",
                )

            db.delete(cluster_orm)
            db.commit()

            return True

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to remove cluster: {str(e)}")
        raise
