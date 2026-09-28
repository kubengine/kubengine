"""Security/resource audit regressions with all external IO mocked."""

import asyncio
import hashlib
import io
import os
import stat
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import (
    BackgroundTasks,
    FastAPI,
    HTTPException,
    Request,
    UploadFile,
)
from sqlalchemy import text
from starlette.datastructures import UploadFile as StarletteUploadFile

from core.command import CommandResult
from core.config import Application
from core.orm.cluster import (
    ClusterSchema,
    ClusterStatus,
    create_cluster,
    find_cluster_by_id,
    update_cluster_status,
)
from core.orm.engine import get_db
from core.orm.image_import import (
    ImageImportTask,
    create_image_import_task,
    find_image_import_task,
)
from core.orm.task import APP_CLEANUP_TASK, Task, TaskSchema
from core.services import app_deployment as app_service
from core.ssh import AsyncSSHClient
from test_app_task_safety import lifecycle as lifecycle_fixture
from test_app_task_safety import run, task_for
from test_auth_security_extra import rotation_config as rotation_config_fixture
from test_image_import_queue import (
    claim,
)
from test_image_import_queue import image_queue as image_queue_fixture
from test_image_import_queue import (
    mock_commands,
    queue_task,
)
from web.api import app as app_api
from web.api import artifacts, auth_routes
from web.api import ssh as ssh_api
from web.utils import auth

# Register shared fixtures under the names requested by these tests.
lifecycle = lifecycle_fixture
rotation_config = rotation_config_fixture
image_queue = image_queue_fixture


def test_delayed_cleanup_rejects_reused_cluster_id(lifecycle, monkeypatch):
    update_cluster_status(lifecycle.cluster_id, ClusterStatus.healthy)
    earlier = task_for(lifecycle, APP_CLEANUP_TASK)
    assert task_for(lifecycle, APP_CLEANUP_TASK).task_id == earlier.task_id
    # Simulate a duplicate already persisted by an older producer.
    with get_db() as db:
        row = Task(
            task_func_path=earlier.task_func_path,
            params=earlier.params,
            resource_id=lifecycle.cluster_id,
        )
        db.add(row)
        db.commit()
        delayed = TaskSchema.model_validate(row)
    removed = []
    monkeypatch.setattr(
        app_service,
        "_find_helm_release",
        lambda name: {"name": name, "status": "deployed"},
    )
    monkeypatch.setattr(
        app_service,
        "_helm_command",
        lambda argv: removed.append(argv[1]) or CommandResult(0, "", ""),
    )
    run(earlier)
    replacement = create_cluster(
        ClusterSchema(
            name="replacement",
            helm_chart="chart",
            helm_chart_version="1",
            config={},
        )
    )
    assert replacement.cluster_id == lifecycle.cluster_id
    with pytest.raises(ValueError, match="身份"):
        run(delayed)
    assert replacement.helm_name not in removed
    assert find_cluster_by_id(replacement.cluster_id) is not None


def test_deploy_and_task_insert_roll_back_together(lifecycle, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("task insert failed")

    monkeypatch.setattr(app_api, "create_task_record", fail)
    with pytest.raises(RuntimeError, match="task insert"):
        app_api.deploy.__wrapped__(
            Request({"type": "http", "headers": []}),
            ClusterSchema(
                name="rollback",
                helm_chart="chart",
                helm_chart_version="1",
                config={},
            ),
            BackgroundTasks(),
        )
    assert find_cluster_by_id(lifecycle.cluster_id + 1) is None
    with get_db() as db:
        assert db.query(Task).count() == 0


def test_worker_picks_up_work_created_after_initial_poll(
    lifecycle, monkeypatch
):
    from core.task_worker import AppTaskWorker

    worker = AppTaskWorker(concurrency=1)
    monkeypatch.setattr(app_service, "_find_helm_release", lambda name: None)
    try:
        worker.poll()
        task = task_for(lifecycle, APP_CLEANUP_TASK)
        worker.poll()
        worker.futures[task.task_id].result(timeout=5)
        assert find_cluster_by_id(lifecycle.cluster_id) is None
    finally:
        worker.close()


@pytest.mark.asyncio
async def test_logout_revokes_renewal_already_minted(
    rotation_config, monkeypatch
):
    token, _ = auth.create_access_token(
        {"sub": "admin"}, expires_delta=timedelta(minutes=1)
    )
    issued = []
    mint = auth.create_access_token

    def record(*args, **kwargs):
        result = mint(*args, **kwargs)
        issued.append(result[0])
        return result

    monkeypatch.setattr(auth, "create_access_token", record)
    entered, finish = asyncio.Event(), asyncio.Event()

    @auth.auth_with_renew()
    async def slow(request: Request):
        entered.set()
        await finish.wait()
        return {"ok": True}

    def request():
        return Request(
            {
                "type": "http",
                "headers": [(b"authorization", f"Bearer {token}".encode())],
            }
        )

    pending = asyncio.create_task(slow(request()))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        await auth_routes.logout(request())
    finally:
        finish.set()
    response = await pending
    assert response.new_access_token is None
    assert issued
    for value in [token, *issued]:
        with pytest.raises(HTTPException) as error:
            await auth.get_current_user(authorization=f"Bearer {value}")
        assert error.value.status_code == 401


def test_cleanup_failure_is_persisted_and_retried_without_push(
    image_queue, monkeypatch
):
    from core.image_maintenance import maintain_images
    from core.orm.image_import import request_cleanup_retry

    _, archive = image_queue
    task_id = queue_task(archive)
    _, lease = claim()
    pushed = mock_commands(monkeypatch, ["example.test/app:v1"])
    results = iter([None, "simulated cleanup failure", None])
    monkeypatch.setattr(
        artifacts, "_prune_apps_namespace", lambda namespace: next(results)
    )
    artifacts.process_image_import_task(task_id, lease=lease)
    task = find_image_import_task(task_id)
    assert task["status"] == "failed" and task["cleanup_pending"]
    assert task["items"][0]["status"] == "success"
    assert task["error_message"] == "simulated cleanup failure"
    request_cleanup_retry(task_id)
    maintain_images()
    task = find_image_import_task(task_id)
    assert task["status"] == "success" and not task["cleanup_pending"]
    assert task["error_message"] is None
    assert pushed == ["example.test/app:v1"]


@pytest.mark.asyncio
async def test_unauthenticated_upload_never_spools(monkeypatch):
    copied = 0

    async def write(self, data):
        nonlocal copied
        copied += len(data)
        pytest.fail("unauthenticated data reached temporary storage")

    async def reject(**kwargs):
        raise HTTPException(status_code=401, detail="not authenticated")

    monkeypatch.setattr(StarletteUploadFile, "write", write)
    monkeypatch.setattr(auth, "get_current_user", reject)
    app = FastAPI()
    app.include_router(artifacts.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/upload/chart",
            files={"file": ("untrusted.tgz", b"x" * 3 * 1024**2)},
        )
    assert response.status_code == 401 and copied == 0


@pytest.mark.asyncio
async def test_chunked_upload_limit_closes_spool(monkeypatch):
    from web.utils import uploads

    opened = []
    from tempfile import SpooledTemporaryFile

    import starlette.formparsers as parsers

    def spool(*args, **kwargs):
        f = SpooledTemporaryFile(*args, **kwargs)
        opened.append(f)
        return f

    async def authenticated(**kwargs):
        return SimpleNamespace(username="admin"), "test"

    monkeypatch.setattr(auth, "get_current_user", authenticated)
    monkeypatch.setattr(parsers, "SpooledTemporaryFile", spool)
    monkeypatch.setattr(
        uploads,
        "upload_limit",
        lambda name: {
            "image_bytes": 1024,
            "reserve_bytes": 1,
            "concurrency": 2,
            "timeout_seconds": 10,
        }[name],
    )
    app = FastAPI()
    app.include_router(artifacts.router)

    async def chunks():
        yield (
            b"--boundary\r\nContent-Disposition: form-data; "
            b'name="file"; filename="archive.tar"\r\n\r\nabc'
        )
        yield b"x" * 70000
        yield b"\r\n--boundary--\r\n"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/upload/image",
            content=chunks(),
            headers={"Content-Type": "multipart/form-data; boundary=boundary"},
        )
    assert response.status_code == 413
    assert opened and all(f.closed for f in opened)


@pytest.mark.asyncio
async def test_ssh_rejects_host_paths_and_existing_files(
    rotation_config, tmp_path, monkeypatch
):
    monkeypatch.setattr(Application, "ROOT_DIR", str(tmp_path))
    target = tmp_path / "server-config"
    target.write_text("original")
    transferred = []

    class SFTP:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, remote, local, *, follow_symlinks):
            assert follow_symlinks is True
            Path(local).write_text("downloaded")

        async def put(self, local, remote, *, follow_symlinks):
            assert follow_symlinks is True
            transferred.append(Path(local).read_text())

    async def connection(self, *args, **kwargs):
        return SimpleNamespace(start_sftp_client=lambda: SFTP())

    monkeypatch.setattr(AsyncSSHClient, "_get_connection", connection)
    app = FastAPI()
    app.include_router(ssh_api.router)
    token, _ = auth.create_access_token({"sub": "admin"})
    body = {
        "host": "trusted.test",
        "username": "operator",
        "password": "dummy",
        "local_path": str(target),
        "remote_path": "/fixture",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        for path in ["/download-file", "/upload-file"]:
            assert (await client.post(path, json=body)).status_code == 400
        assert target.read_text() == "original"
        body["local_path"] = "safe.tar"
        assert (
            await client.post("/download-file", json=body)
        ).status_code == 200
        assert (
            await client.post("/download-file", json=body)
        ).status_code == 409
        assert (
            await client.post("/upload-file", json=body)
        ).status_code == 200
        directory = (
            tmp_path
            / "tmp/ssh-transfers"
            / hashlib.sha256(b"admin").hexdigest()
        )
        (directory / "link.tar").symlink_to(target)
        os.link(target, directory / "hardlink.tar")
        for name in ["link.tar", "hardlink.tar", "../server-config", "a\\b"]:
            body["local_path"] = name
            assert (
                await client.post("/upload-file", json=body)
            ).status_code == 400
        assert (
            transferred == ["downloaded"] and target.read_text() == "original"
        )


def test_mount_failure_removes_created_container(monkeypatch, tmp_path):
    from builder.image.base_builder import BaseBuilder

    class Builder(BaseBuilder):
        def _custom_step(self, context):
            pass

    config = tmp_path / "builder.yaml"
    config.write_text("{}")
    builder = Builder("fixture", config_file=config)
    owned = []
    monkeypatch.setattr(builder, "_validate_version", lambda version: None)
    monkeypatch.setattr(builder, "_load_config", lambda version: {})
    monkeypatch.setattr(
        builder,
        "_create_base_container",
        lambda image: owned.append("container") or owned[-1],
    )

    def mount_failure(container):
        raise RuntimeError("mount failure")

    monkeypatch.setattr(builder, "_mount_container", mount_failure)
    monkeypatch.setattr(builder, "_umount_container", lambda container: None)
    monkeypatch.setattr(
        builder, "_del_container", lambda container: owned.remove(container)
    )
    with pytest.raises(RuntimeError, match="mount failure"):
        builder.build("1")
    assert not owned


def test_database_permissions_and_links(tmp_path):
    from core.private_files import prepare_database

    path = prepare_database(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    path.unlink()
    target = tmp_path / "elsewhere"
    target.write_text("unchanged")
    path.symlink_to(target)
    with pytest.raises(OSError):
        prepare_database(tmp_path)
    assert target.read_text() == "unchanged"


def test_archive_limits_recover_partial_copy(image_queue, monkeypatch):
    from core import image_storage

    task = create_image_import_task("images.tar", None, "")
    monkeypatch.setattr(
        image_storage,
        "upload_limit",
        lambda name: {
            "image_bytes": 4,
            "archive_bytes": 100,
            "reserve_bytes": 1,
            "timeout_seconds": 30,
        }[name],
    )
    with pytest.raises(HTTPException) as error:
        image_storage.store_archive(
            task["task_id"], io.BytesIO(b"12345"), threading.Event()
        )
    assert error.value.status_code == 413
    result = find_image_import_task(task["task_id"])
    assert result["status"] == "failed"
    with image_storage.archive_directory(task["task_id"]) as directory:
        assert os.listdir(directory) == []


def test_archive_reaper_recovers_crash_and_retention(image_queue):
    from core.image_storage import archive_directory, maintain_archives
    from core.private_files import private_file

    task = create_image_import_task("interrupted.tar", None, "")
    with archive_directory(task["task_id"]) as directory:
        with private_file(
            directory, ".uploading", os.O_WRONLY | os.O_CREAT | os.O_EXCL
        ) as fd:
            os.write(fd, b"partial")
    with get_db() as db:
        row = db.get(ImageImportTask, task["task_id"])
        row.created_at = datetime.now() - timedelta(days=1)
        db.commit()
    maintain_archives()
    assert find_image_import_task(task["task_id"])["status"] == "failed"
    with archive_directory(task["task_id"]) as directory:
        assert os.listdir(directory) == []
    archived = create_image_import_task("old.tar", None, "ignored-db-path")
    with archive_directory(archived["task_id"]) as directory:
        with private_file(
            directory, "archive.tar", os.O_WRONLY | os.O_CREAT | os.O_EXCL
        ) as fd:
            os.write(fd, b"expired archive")
    with get_db() as db:
        row = db.get(ImageImportTask, archived["task_id"])
        row.status = "success"
        row.completed_at = datetime.now() - timedelta(days=8)
        db.commit()
    maintain_archives()
    assert (
        find_image_import_task(archived["task_id"], include_file_path=True)[
            "file_path"
        ]
        == ""
    )
    with archive_directory(archived["task_id"]) as directory:
        assert os.listdir(directory) == []


@pytest.mark.asyncio
async def test_cancelled_copy_finishes_cleanup_before_closing_source(
    image_queue,
):
    entered, release = threading.Event(), threading.Event()

    class BlockingSource(io.BytesIO):
        def read(self, *args):
            entered.set()
            assert release.wait(5)
            return super().read(*args)

    source = BlockingSource(b"partial upload")
    pending = asyncio.create_task(
        artifacts._create_image_import(
            UploadFile(filename="cancel.tar", file=source)
        )
    )
    for _ in range(100):
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert entered.is_set()
    pending.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert source.closed
    with get_db() as db:
        row = db.query(ImageImportTask).one()
        assert row.status == "failed"
        task_id = row.task_id
    from core.image_storage import archive_directory

    with archive_directory(task_id) as directory:
        assert os.listdir(directory) == []


def test_shared_notifications_visible_from_new_connection(image_queue):
    from core.orm.notifications import (
        read_cluster_revision,
    )

    start = read_cluster_revision()
    app_api._notify({"action": "refresh_clusters"})
    assert read_cluster_revision() == start + 1
    # A fresh SQL connection models a separate worker's view.
    engine, _ = image_queue
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT revision FROM cluster_revision WHERE id=1")
            ).scalar_one()
            == start + 1
        )


def test_legacy_schema_migration_is_repeatable(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, inspect

    import core.orm.engine as engine_module
    from core.orm.cluster import ensure_cluster_schema
    from core.orm.image_import import ensure_image_import_schema

    engine = create_engine(f'sqlite:///{tmp_path / "legacy.db"}')
    monkeypatch.setattr(engine_module, "engine", engine)
    monkeypatch.setattr(Application, "ROOT_DIR", str(tmp_path))
    try:
        with engine.begin() as conn:
            conn.execute(
                text("CREATE TABLE cluster (cluster_id INTEGER PRIMARY KEY)")
            )
            conn.execute(text("INSERT INTO cluster VALUES (1), (2)"))
            conn.execute(
                text(
                    (
                        "CREATE TABLE image_import_task (task_id "
                        "INTEGER PRIMARY KEY)"
                    )
                )
            )
        ensure_cluster_schema()
        ensure_image_import_schema()
        with engine.connect() as conn:
            first = (
                conn.execute(
                    text(
                        "SELECT resource_uid FROM cluster ORDER BY cluster_id"
                    )
                )
                .scalars()
                .all()
            )
        assert len(set(first)) == 2 and all(len(uid) == 32 for uid in first)
        ensure_cluster_schema()
        ensure_image_import_schema()
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text(
                        "SELECT resource_uid FROM cluster ORDER BY cluster_id"
                    )
                )
                .scalars()
                .all()
                == first
            )
        columns = {
            c["name"] for c in inspect(engine).get_columns("image_import_task")
        }
        assert {
            "cleanup_pending",
            "cleanup_error",
            "cleanup_retry_at",
            "namespace",
            "lease_owner",
        } <= columns
    finally:
        engine.dispose()


def test_legacy_task_without_identity_is_never_dispatched(
    lifecycle, monkeypatch
):
    def forbidden(*args, **kwargs):
        pytest.fail("legacy task must not touch Helm")

    monkeypatch.setattr(app_service, "clean_up_cluster", forbidden)
    with get_db() as db:
        row = Task(
            task_func_path=APP_CLEANUP_TASK,
            resource_id=lifecycle.cluster_id,
            params={"cluster_id": lifecycle.cluster_id},
        )
        db.add(row)
        db.commit()
        task = TaskSchema.model_validate(row)
    with pytest.raises(ValueError, match="缺少资源身份"):
        run(task)
    assert find_cluster_by_id(lifecycle.cluster_id) is not None


def test_archive_total_quota_preserves_existing_upload(
    image_queue, monkeypatch
):
    from core import image_storage

    monkeypatch.setattr(
        image_storage,
        "upload_limit",
        lambda name: {
            "image_bytes": 10,
            "archive_bytes": 3,
            "reserve_bytes": 1,
            "timeout_seconds": 30,
        }[name],
    )
    first = create_image_import_task("first.tar", None, "")
    image_storage.store_archive(
        first["task_id"], io.BytesIO(b"12"), threading.Event()
    )
    second = create_image_import_task("second.tar", None, "")
    with pytest.raises(HTTPException) as error:
        image_storage.store_archive(
            second["task_id"], io.BytesIO(b"34"), threading.Event()
        )
    assert error.value.status_code == 507
    assert find_image_import_task(first["task_id"])["status"] == "pending"
    assert find_image_import_task(second["task_id"])["status"] == "failed"
    path = find_image_import_task(first["task_id"], include_file_path=True)[
        "file_path"
    ]
    assert Path(path).read_bytes() == b"12"


def test_upload_concurrency_releases_slots_on_exception(tmp_path, monkeypatch):
    from web.utils import uploads

    monkeypatch.setattr(Application, "ROOT_DIR", str(tmp_path))
    monkeypatch.setattr(uploads, "upload_limit", lambda name: 1)
    with pytest.raises(RuntimeError, match="interrupted"):
        with uploads.upload_slot():
            with pytest.raises(HTTPException) as error:
                with uploads.upload_slot():
                    pytest.fail("limit bypassed")
            assert error.value.status_code == 429
            raise RuntimeError("interrupted")
    with uploads.upload_slot():
        pass


def test_temporary_namespace_removal_failure_remains_retryable(monkeypatch):
    seen = []
    namespace = "kubengine-import-17"
    monkeypatch.setattr(artifacts, "_prune_apps_namespace", lambda name: None)

    def command(argv, **kwargs):
        seen.append(argv)
        if argv[2] == "list":
            return CommandResult(0, namespace, "")
        return CommandResult(1, "", "namespace still has content")

    monkeypatch.setattr(artifacts, "execute_command", command)
    assert "namespace still has content" in artifacts._cleanup_image_namespace(
        namespace
    )
    assert seen[-1] == ["ctr", "namespaces", "remove", namespace]


def test_failed_partial_cleanup_keeps_durable_recovery_path(
    image_queue, monkeypatch
):
    from core import image_storage

    remove = image_storage.remove_archive

    def interrupted(*args):
        raise OSError("temporary filesystem failure")

    monkeypatch.setattr(image_storage, "remove_archive", interrupted)
    task = create_image_import_task("interrupted.tar", None, "")
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(RuntimeError, match="上传中断"):
        image_storage.store_archive(
            task["task_id"], io.BytesIO(b"partial"), cancelled
        )
    record = find_image_import_task(task["task_id"], include_file_path=True)
    assert record["status"] == "failed" and record["file_path"]
    assert "待回收" in record["error_message"]
    monkeypatch.setattr(image_storage, "remove_archive", remove)
    with get_db() as db:
        db.get(ImageImportTask, task["task_id"]).completed_at = (
            datetime.now() - timedelta(days=8)
        )
        db.commit()
    image_storage.maintain_archives()
    assert (
        find_image_import_task(task["task_id"], include_file_path=True)[
            "file_path"
        ]
        == ""
    )
