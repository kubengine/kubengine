"""Application ORM models and database operations.

This module defines the application table model and provides database operations
for creating, reading, updating, and deleting applications with their field configurations.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel
from sqlalchemy import JSON, Column, DateTime, Integer, String, asc, desc
from sqlalchemy.orm import relationship, joinedload

from core.logger import get_logger
from core.orm.app_field_config import AppFieldConfig, AppFieldConfigSchema
from core.orm.engine import Base, get_db

logger = get_logger(__name__)


class App(Base):
    """Application table model representing application entities."""

    __tablename__ = "app"

    app_id = Column(Integer, primary_key=True,
                    index=True, comment="Application ID")
    name = Column(String, unique=True, nullable=False,
                  comment="Application name")
    category = Column(JSON, nullable=False, comment="Application category")
    description = Column(String, comment="Application description")
    helm_chart = Column(String, nullable=False,
                        comment="Associated Helm chart template")
    create_time = Column(DateTime, default=datetime.now,
                         comment="Creation timestamp")

    # Relationship with field configurations
    app_field_configs = relationship(
        "AppFieldConfig",
        back_populates="app",
        cascade="all, delete-orphan",
        passive_deletes=True
    )


class AppSchema(BaseModel):
    """Pydantic model for application data serialization.

    Includes nested field configurations for API responses.
    """
    app_id: Optional[int] = None
    name: Optional[str] = None
    category: Optional[List[str]] = None
    description: Optional[str] = None
    helm_chart: Optional[str] = None
    create_time: Optional[datetime] = None
    app_field_configs: Optional[List[AppFieldConfigSchema]] = None

    class Config:
        from_attributes = True
        arbitrary_types_allowed = True


def find_applications_paginated(
    page: int = 1,
    page_size: int = 10,
    sort_by: str = "create_time",
    sort_order: str = "desc",
    filters: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Find applications with pagination, sorting, and filtering.

    Args:
        page: Page number (1-based, default 1)
        page_size: Items per page (default 10)
        sort_by: Field name for sorting
        sort_order: Sort direction ('asc' or 'desc', default 'desc')
        filters: Optional filter conditions dictionary

    Returns:
        Dictionary containing total count, paginated data, current page, and page size
    """
    with get_db() as db:
        filter_conditions = filters or {}
        query = db.query(App)

        # Apply filters
        if "name" in filter_conditions and filter_conditions["name"]:
            name_filter = f"%{filter_conditions['name']}%"
            query = query.filter(App.name.like(name_filter))

        if "category" in filter_conditions and filter_conditions["category"]:
            query = query.filter(App.category == filter_conditions["category"])

        # Validate sort field to prevent SQL injection
        valid_sort_fields = ["app_id", "name", "category", "create_time"]
        if sort_by not in valid_sort_fields:
            sort_by = "create_time"

        # Apply sorting
        sort_direction = desc if sort_order.lower() == "desc" else asc
        sort_column = getattr(App, sort_by)
        query = query.order_by(sort_direction(sort_column))

        # Count total items
        total = query.count()

        # Apply pagination
        offset = (page - 1) * page_size
        paginated_items = query.offset(offset).limit(page_size).all()

        return {
            "total": total,
            "data": [AppSchema.model_validate(item) for item in paginated_items],
            "page": page,
            "page_size": page_size
        }


def remove_application_by_id(app_id: str) -> bool:
    """Remove an application by ID with cascade deletion of field configs.

    Args:
        app_id: Application ID to remove

    Returns:
        True if successfully removed, False if application not found

    Raises:
        ValueError: When business validation fails
        Exception: When database operation fails
    """
    try:
        with get_db() as db:
            # Find application with related field configs
            app = db.query(App).filter(App.app_id == app_id).options(
                joinedload(App.app_field_configs)
            ).first()

            if not app:
                logger.warning(
                    f"Application not found for deletion: ID {app_id}")
                return False

            # Delete with cascade (automatically deletes related field configs)
            db.delete(app)
            db.commit()

            logger.info(
                f"Application deleted successfully: {app.name} (ID: {app_id}), "
                "related field configurations auto-cascaded"
            )
            return True

    except ValueError as e:
        # Business validation error - re-raise without rollback
        logger.error(f"Business validation failed for app deletion: {str(e)}")
        raise
    except Exception as e:
        # Database error - ensure rollback
        logger.error(
            f"Database error during app deletion (ID: {app_id}): {str(e)}")
        raise


def find_application_by_id(app_id: int) -> Optional[AppSchema]:
    """Find application by ID with preloaded field configurations.

    Args:
        app_id: Application ID to search for

    Returns:
        Application schema if found, None otherwise

    Raises:
        Exception: When database operation fails
    """
    try:
        with get_db() as db:
            query = (
                db.query(App)
                .filter(App.app_id == app_id)
                .options(joinedload(App.app_field_configs))
            )

            app = query.first()
            if app:
                return AppSchema.model_validate(app)
            return None

    except Exception as e:
        logger.error(f"Database error fetching app by ID {app_id}: {str(e)}")
        raise


def create_application(app_schema: AppSchema) -> AppSchema:
    """Create a new application with its field configurations.

    Args:
        app_schema: Pydantic model containing application data

    Returns:
        Created application schema with generated ID

    Raises:
        Exception: When database operation fails
    """
    try:
        with get_db() as db:
            # Create main application record
            app_orm = App(
                name=app_schema.name,
                category=app_schema.category,
                description=app_schema.description,
                helm_chart=app_schema.helm_chart,
                create_time=app_schema.create_time or datetime.now()
            )

            # Create field configuration records if provided
            if app_schema.app_field_configs:
                field_configs: list[AppFieldConfig] = []
                for config in app_schema.app_field_configs:
                    field_config = AppFieldConfig(
                        config_type=config.config_type,
                        name=config.name,
                        label=config.label,
                        type=config.type,
                        extra=config.extra,
                        order=config.order,
                        form_item_props=config.form_item_props,
                        initial_value=config.initial_value,
                        rules=config.rules,
                        field_props=config.field_props
                    )
                    field_configs.append(field_config)

                app_orm.app_field_configs = field_configs

            # Save to database
            db.add(app_orm)
            db.commit()
            db.refresh(app_orm)  # Refresh to get generated ID

            logger.info(
                f"Application created successfully: {app_orm.name} (ID: {app_orm.app_id})")
            return AppSchema.model_validate(app_orm)

    except Exception as e:
        logger.error(f"Database error creating application: {str(e)}")
        raise


def update_application(app_schema: AppSchema) -> Optional[AppSchema]:
    """Update an existing application and its field configurations.

    Args:
        app_schema: Pydantic model containing updated application data

    Returns:
        Updated application schema if found, None if application doesn't exist

    Raises:
        Exception: When database operation fails
    """
    try:
        with get_db() as db:
            # Find existing application
            app_orm = (
                db.query(App)
                .filter(App.app_id == app_schema.app_id)
                .first()
            )

            if not app_orm:
                logger.warning(
                    f"Application not found for update: ID {app_schema.app_id}")
                return None

            # Update main fields (only non-None values)
            update_data = app_schema.model_dump(
                exclude_unset=True,
                exclude={"app_id", "app_field_configs"}
            )
            for field_name, field_value in update_data.items():
                if hasattr(app_orm, field_name):
                    setattr(app_orm, field_name, field_value)

            # Update field configurations (full replacement strategy)
            if app_schema.app_field_configs is not None:
                # New field configurations will replace existing ones due to cascade
                new_field_configs: list[AppFieldConfig] = []
                for config in app_schema.app_field_configs:
                    field_config = AppFieldConfig(
                        field_id=config.field_id,
                        app_id=app_schema.app_id,
                        config_type=config.config_type,
                        name=config.name,
                        label=config.label,
                        type=config.type,
                        extra=config.extra,
                        order=config.order,
                        form_item_props=config.form_item_props,
                        initial_value=config.initial_value,
                        rules=config.rules,
                        field_props=config.field_props,
                        helm_props=config.helm_props,
                    )
                    new_field_configs.append(field_config)

                app_orm.app_field_configs = new_field_configs

            # Commit changes
            db.commit()
            db.refresh(app_orm)

            logger.info(
                f"Application updated successfully: ID {app_schema.app_id}")
            return AppSchema.model_validate(app_orm)

    except Exception as e:
        logger.error(
            f"Database error updating application (ID: {app_schema.app_id}): {str(e)}")
        raise
