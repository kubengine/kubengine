"""Filter image import tasks using isolated SQLite and HTTP queries."""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

import core.orm.engine as engine_module
from core.config import Application
from core.orm.engine import Base, get_db
from core.orm.image_import import ImageImportTask, find_image_import_tasks
from web import main
from web.api import artifacts
from web.utils import auth

TASK_STATUSES = (
    "uploading",
    "pending",
    "processing",
    "success",
    "partial_success",
    "failed",
)


@pytest.fixture
def task_store(monkeypatch, tmp_path):
    test_engine = create_engine(f"sqlite:///{tmp_path / 'tasks.db'}")
    test_sessions = scoped_session(sessionmaker(bind=test_engine))
    monkeypatch.setattr(engine_module, "engine", test_engine)
    monkeypatch.setattr(engine_module, "SessionLocal", test_sessions)
    monkeypatch.setattr(Application, "ROOT_DIR", str(tmp_path))
    Base.metadata.create_all(bind=test_engine)

    async def authenticated(**kwargs):
        return SimpleNamespace(username="admin", role="platform_admin"), "test"

    monkeypatch.setattr(auth, "get_current_user", authenticated)

    def seed(statuses):
        task_ids = []
        with get_db() as db:
            for index, status in enumerate(statuses):
                task = ImageImportTask(
                    filename=f"images-{index}.tar",
                    file_path="",
                    status=status,
                    created_at=datetime(2026, 1, 1) + timedelta(seconds=index),
                )
                db.add(task)
                db.flush()
                task_ids.append(task.task_id)
            db.commit()
        return task_ids

    yield seed
    test_sessions.remove()
    test_engine.dispose()


def request_tasks(params):
    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://test",
        ) as client:
            return await client.get(
                "/api/v1/artifacts/image-import-tasks", params=params
            )

    return asyncio.run(request())


def query_tasks(transport, params):
    if transport == "orm":
        return find_image_import_tasks(**params)
    response = request_tasks(params)
    assert response.status_code == 200
    assert response.json()["code"] == 200
    return response.json()["data"]


@pytest.mark.parametrize("transport", ["orm", "api"])
def test_list_without_status_preserves_all_tasks_and_order(
    task_store, transport
):
    task_ids = task_store(TASK_STATUSES)

    result = query_tasks(transport, {"page": 1, "page_size": 10})

    assert result["total"] == len(task_ids)
    assert result["page"] == 1
    assert result["page_size"] == 10
    assert [task["task_id"] for task in result["data"]] == task_ids[::-1]


@pytest.mark.parametrize("transport", ["orm", "api"])
@pytest.mark.parametrize("status", TASK_STATUSES)
def test_list_filters_each_task_status(task_store, transport, status):
    task_ids = task_store(TASK_STATUSES)

    result = query_tasks(
        transport, {"page": 1, "page_size": 10, "status": status}
    )

    assert result["total"] == 1
    assert [task["task_id"] for task in result["data"]] == [
        task_ids[TASK_STATUSES.index(status)]
    ]
    assert result["data"][0]["status"] == status


@pytest.mark.parametrize("transport", ["orm", "api"])
@pytest.mark.parametrize(
    ("page", "expected_indices"), [(1, [4, 2]), (2, [0]), (3, [])]
)
def test_filter_precedes_total_and_pagination(
    task_store, transport, page, expected_indices
):
    task_ids = task_store(
        ["failed", "success", "failed", "processing", "failed", "pending"]
    )

    result = query_tasks(
        transport, {"page": page, "page_size": 2, "status": "failed"}
    )

    assert result["total"] == 3
    assert result["page"] == page
    assert result["page_size"] == 2
    assert [task["task_id"] for task in result["data"]] == [
        task_ids[index] for index in expected_indices
    ]


@pytest.mark.parametrize("transport", ["orm", "api"])
def test_filter_without_matches_returns_empty_result(task_store, transport):
    task_store(["pending", "success"])

    result = query_tasks(
        transport, {"page": 1, "page_size": 10, "status": "failed"}
    )

    assert result == {"total": 0, "page": 1, "page_size": 10, "data": []}


@pytest.mark.parametrize("status", ["invalid", "", "FAILED", " failed "])
def test_invalid_status_uses_standard_parameter_error(
    task_store, monkeypatch, status
):
    def unexpected_query(*args, **kwargs):
        pytest.fail("invalid status must be rejected before querying tasks")

    monkeypatch.setattr(artifacts, "find_image_import_tasks", unexpected_query)

    response = request_tasks({"status": status})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == 422
    assert "参数校验失败" in body["message"]
    assert "status" in body["message"]
    assert "data" not in body
