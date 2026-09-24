from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

import core.orm.engine as engine_module
from core.orm.engine import Base
from core.orm.image_import import (
    claim_next_image_import_task,
    create_image_import_task,
    ensure_image_import_schema,
    renew_image_import_lease,
    update_image_import_task,
)


def test_expired_image_task_lease_can_be_reclaimed(monkeypatch, tmp_path) -> None:
    test_engine = create_engine(f"sqlite:///{tmp_path / 'queue.db'}")
    test_sessions = scoped_session(sessionmaker(bind=test_engine))
    monkeypatch.setattr(engine_module, "engine", test_engine)
    monkeypatch.setattr(engine_module, "SessionLocal", test_sessions)

    Base.metadata.create_all(bind=test_engine)
    ensure_image_import_schema()
    task = create_image_import_task("images.tar", "application/x-tar", "/tmp/images.tar")
    task_id = int(task["task_id"])

    # Uploading rows are deliberately invisible to the worker.
    assert claim_next_image_import_task("worker-a", 60) is None
    update_image_import_task(task_id, status="pending")
    claimed = claim_next_image_import_task("worker-a", 60)
    assert claimed is not None
    assert claimed["attempt_count"] == 1
    assert renew_image_import_lease(task_id, "worker-b", 60) is False

    update_image_import_task(
        task_id, lease_expires_at=datetime.now() - timedelta(seconds=1)
    )
    reclaimed = claim_next_image_import_task("worker-b", 60)
    assert reclaimed is not None
    assert reclaimed["task_id"] == task_id
    assert reclaimed["attempt_count"] == 2

    test_sessions.remove()
    test_engine.dispose()
