"""三角色、真实 SDK 范围、敏感边界、任务撤权及审计故障的允许/拒绝矩阵。"""

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import scoped_session, sessionmaker

from core import authorization as policy
from core.auth_credentials import load_auth_user
from core.orm import audit
from core.orm import engine as engine_module
from core.orm import identity
from core.orm.engine import Base
from core.services.resource_create import ResourceCreator
from core.services.resource_yaml import ResourceYaml
from test_identity_management import add_user, client, headers
from test_identity_management import store as identity_store
from test_identity_management import update
from test_kubernetes_view_client import readable, resource
from test_kubernetes_view_client import view as sdk_view
from test_resource_create import Cluster, payload
from test_resource_create import resource as manifest
from web.api import k8s

store = identity_store
view = sdk_view


@pytest.fixture
def secured(store, monkeypatch):
    engine, _, _ = store
    sessions = scoped_session(sessionmaker(bind=engine))
    monkeypatch.setattr(engine_module, "engine", engine)
    monkeypatch.setattr(engine_module, "SessionLocal", sessions)
    Base.metadata.create_all(engine)
    yield
    sessions.remove()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["readonly", "namespace_operator"])
async def test_scope_counts_search_pagination_and_legacy_aliases(
    secured, view, monkeypatch, role
):
    add_user(role)
    view.core.list_namespaced_config_map.side_effect = lambda namespace, **_: {
        "items": [resource("ConfigMap", name="allowed", namespace=namespace)]
    }
    monkeypatch.setattr(k8s, "get_kubernetes_client", lambda: view)
    async with client() as http:
        h = headers("reader")
        for prefix in ("resource", "dashboard/resource"):
            for alias in ("cm", "ConfigMap", "configmaps"):
                response = await http.get(
                    f"/api/v1/k8s/{prefix}/{alias}?namespace=_all&page_size=1",
                    headers=h,
                )
                assert response.status_code == 200, response.text
                data = response.json()["data"]
                assert data["listMeta"]["totalItems"] == 1
                assert data["items"][0]["objectMeta"]["namespace"] == "team-a"
        assert (
            await http.get(
                "/api/v1/k8s/resource/configmap?name=not-present", headers=h
            )
        ).json()["data"]["listMeta"]["totalItems"] == 0
        for path in (
            "resource/pod?namespace=team-b",
            "resource/pod?namespace=_cluster",
            "resource/node",
            "resourcedetail/configmap/team-b/x",
            "manifest/pod/team-b/x",
            "events/pod/team-b/x",
            "resourcepod/deployment/team-b/x",
            "pods/team-b/x/logs",
        ):
            assert (
                await http.get("/api/v1/k8s/" + path, headers=h)
            ).status_code == 403, path
        response = await http.get("/api/v1/k8s/resource/namespace", headers=h)
        assert [
            r["objectMeta"]["name"]
            for r in response.json()["data"]["namespaces"]
        ] == ["team-a"]
    view.core.list_config_map_for_all_namespaces.assert_not_called()
    view.core.list_namespace.assert_not_called()


@pytest.mark.asyncio
async def test_empty_scope_never_queries_global_cluster(
    secured, view, monkeypatch
):
    user = add_user()
    update(load_auth_user("admin")["user_id"], user, namespaces=[])
    monkeypatch.setattr(k8s, "get_kubernetes_client", lambda: view)
    async with client() as http:
        response = await http.get(
            "/api/v1/k8s/resource/pod", headers=headers("reader")
        )
    assert response.status_code == 200
    assert response.json()["data"]["listMeta"]["totalItems"] == 0
    view.core.list_pod_for_all_namespaces.assert_not_called()
    view.core.list_namespaced_pod.assert_not_called()


@pytest.mark.asyncio
async def test_secret_values_and_annotations_require_separate_grant(
    secured, view, monkeypatch
):
    import copy

    from web.api import resource_yaml

    user = add_user()
    raw = resource("Secret", name="secret", namespace="team-a")
    raw["metadata"]["resourceVersion"] = "1"
    raw["data"] = {"password": "PRIVATE_SECRET"}
    raw["metadata"]["annotations"] = {"last-applied": "PRIVATE_ANNOTATION"}
    monkeypatch.setattr(resource_yaml, "get_kubernetes_client", lambda: view)
    monkeypatch.setattr(
        ResourceYaml, "call", lambda *args, **kwargs: copy.deepcopy(raw)
    )
    view.core.list_namespaced_secret.return_value = {"items": [raw]}
    readable(view.core, "read_namespaced_secret", raw)
    monkeypatch.setattr(k8s, "get_kubernetes_client", lambda: view)
    async with client() as http:
        h = headers("reader")
        for path in (
            "resource/secret",
            "resourcedetail/tls/team-a/secret",
            "manifest/secret/team-a/secret",
            "manifest/secret/team-a/secret/edit",
        ):
            response = await http.get("/api/v1/k8s/" + path, headers=h)
            assert response.status_code == 200, response.text
            assert (
                "PRIVATE_SECRET" not in response.text
                and "PRIVATE_ANNOTATION" not in response.text
            )
        for suffix in ("", "/edit", "/versions/1"):
            assert (
                await http.get(
                    "/api/v1/k8s/manifest/secret/"
                    "team-a/secret" + suffix + "?reveal_secret=true",
                    headers=h,
                )
            ).status_code == 403
        update(
            load_auth_user("admin")["user_id"],
            user,
            sensitive_permissions=["secret.read"],
        )
        assert (
            "PRIVATE_SECRET"
            in (
                await http.get(
                    (
                        "/api/v1/k8s/manifest/secret/"
                        "team-a/secret"
                        "?reveal_secret=true"
                    ),
                    headers=h,
                )
            ).text
        )


@pytest.mark.parametrize(
    "role,action,grants,allowed",
    [
        ("readonly", "resource.read", [], True),
        ("readonly", "resource.update", [], False),
        ("namespace_operator", "resource.update", [], True),
        ("namespace_operator", "secret.read", [], False),
        ("readonly", "pod.terminal", [], False),
        ("readonly", "pod.terminal", ["pod.terminal"], True),
        ("namespace_operator", "pod.files.write", ["pod.files.read"], False),
        ("readonly", "pod.files.read", ["pod.files.read"], True),
        ("readonly", "pod.files.write", ["pod.files.write"], True),
    ],
)
def test_action_matrix_and_grants_cannot_expand_namespaces(
    role, action, grants, allowed
):
    user = SimpleNamespace(
        role=role, namespaces=["team-a"], sensitive_permissions=grants
    )
    if allowed:
        policy.authorize(user, action, "pod", "team-a")
    else:
        with pytest.raises(HTTPException):
            policy.authorize(user, action, "pod", "team-a")
    with pytest.raises(HTTPException):
        policy.authorize(user, action, "pod", "team-b")


@pytest.mark.parametrize(
    "invalid",
    [
        manifest(namespace="team-b"),
        manifest(kind="Namespace"),
        manifest(
            kind="RoleBinding",
            version="rbac.authorization.k8s.io/v1",
            namespace="team-a",
        ),
        manifest(
            kind="ConfigMap",
            version="untrusted.example/v1",
            namespace="team-a",
        ),
    ],
)
def test_batch_authorization_happens_before_any_dry_run_or_write(invalid):
    cluster = Cluster()
    # 为伪造 API 组提供 discovery，也不得凭 namespaced=true 绕过类型限制。
    original = cluster.call

    def call(method, path, body=None, dry_run=False):
        if method == "GET" and path == "/apis/rbac.authorization.k8s.io/v1":
            return {
                "resources": [
                    {
                        "kind": "RoleBinding",
                        "name": "rolebindings",
                        "namespaced": True,
                        "verbs": ["create"],
                    }
                ]
            }
        if method == "GET" and path == "/apis/untrusted.example/v1":
            return {
                "resources": [
                    {
                        "kind": "ConfigMap",
                        "name": "configmaps",
                        "namespaced": True,
                        "verbs": ["create"],
                    }
                ]
            }
        return original(method, path, body, dry_run)

    cluster.call = call
    user = SimpleNamespace(
        role="namespace_operator",
        namespaces=["team-a"],
        sensitive_permissions=[],
    )
    ops = ResourceCreator(
        cluster, "operator", authorizer=policy.authorizer(user)
    )
    ops.sdk = cluster
    with pytest.raises(HTTPException):
        ops.preview(
            payload(manifest(namespace="team-a"), invalid, namespace="team-a")
        )
    assert not any(method == "POST" for method, *_ in cluster.calls)


def test_authorized_batch_and_safe_workload_rules():
    cluster = Cluster()
    cluster.namespaces.add("team-a")
    user = SimpleNamespace(
        role="namespace_operator",
        namespaces=["team-a"],
        sensitive_permissions=[],
    )
    ops = ResourceCreator(
        cluster, "operator", authorizer=policy.authorizer(user)
    )
    ops.sdk = cluster
    assert ops.preview(
        payload(manifest(namespace="team-a"), namespace="team-a")
    )["valid"]
    for spec in (
        {"hostNetwork": True},
        {"volumes": [{"hostPath": {"path": "/"}}]},
        {"serviceAccountName": "admin"},
        {"automountServiceAccountToken": True},
    ):
        with pytest.raises(HTTPException):
            policy.validate_workload(
                user, "team-a", {"kind": "Pod", "spec": spec}
            )


@pytest.mark.asyncio
async def test_denied_operation_is_audited_and_filters_work(secured):
    add_user()
    async with client() as http:
        assert (
            await http.get("/api/v1/users", headers=headers("reader"))
        ).status_code == 403
        result = await http.get(
            "/api/v1/audit-events?username=reader&result=denied",
            headers=headers(),
        )
        assert result.status_code == 200
        rows = result.json()["data"]["items"]
        assert rows and all(
            row["username"] == "reader" and row["result"] == "denied"
            for row in rows
        )
        assert (
            await http.get("/api/v1/audit-events", headers=headers("reader"))
        ).status_code == 403


@pytest.mark.asyncio
async def test_audit_storage_failure_is_closed_or_reports_uncertain_result(
    secured, monkeypatch
):

    original = audit.begin_event

    def failed(**kwargs):
        raise HTTPException(503, "无法建立操作审计，操作尚未执行")

    monkeypatch.setattr(audit, "begin_event", failed)
    async with client() as http:
        body = {"username": "no-write", "password": "sample-password-123"}
        assert (
            await http.post("/api/v1/users", headers=headers(), json=body)
        ).status_code == 503
        assert load_auth_user("no-write") is None
        monkeypatch.setattr(audit, "begin_event", original)

        def lost(*args, **kwargs):
            raise HTTPException(
                503,
                {
                    "message": "操作结果待核实，勿直接重试",
                    "error_code": "AUDIT_FINALIZE_UNCERTAIN",
                },
            )

        monkeypatch.setattr(audit, "finish_event", lost)
        response = await http.post(
            "/api/v1/users", headers=headers(), json=body
        )
        assert (
            response.status_code == 503
            and response.json()["error_code"] == "AUDIT_FINALIZE_UNCERTAIN"
        )
        assert load_auth_user("no-write") is not None


def test_task_revocation_checks_stable_actor_and_auth_revision(secured):
    user = add_user("platform_admin", "worker-owner")
    record = load_auth_user("worker-owner")
    with policy.bind_actor(record):
        context = policy.snapshot()
    with identity.get_db() as db:
        policy.check_task(context, db)
    update(load_auth_user("admin")["user_id"], user, role="readonly")
    with identity.get_db() as db:
        with pytest.raises(HTTPException):
            policy.check_task(context, db)
    with pytest.raises(HTTPException):
        policy.check_task(None)


def test_archive_is_verified_before_pruning_and_restorable(
    secured, monkeypatch, tmp_path
):
    from core.audit_archive import archive_audit
    from core.config import Application

    monkeypatch.setattr(Application, "ROOT_DIR", str(tmp_path))
    event = audit.begin_event(username="admin", action="test.archive")
    audit.finish_event(event, 200)
    with engine_module.get_db() as db:
        db.execute(
            text("UPDATE audit_event SET created_at=1 WHERE event_id=:id"),
            {"id": event},
        )
        db.commit()
    result = archive_audit(prune=True, days=1)
    from pathlib import Path

    archived = [
        json.loads(line)
        for line in Path(result["path"]).read_text().splitlines()
    ]
    assert len(archived) == 1 and archived[0]["event_id"] == event
    assert Path(result["path"]).stat().st_mode & 0o777 == 0o600
    assert audit.query_events()["total"] == 0


def test_application_task_cannot_begin_after_initiator_demotion(
    secured, monkeypatch
):
    from fastapi import BackgroundTasks, Request

    from core.orm.cluster import ClusterSchema
    from core.orm.task import Task, TaskStatus, execute_task_function
    from core.services import app_deployment
    from web.api import app as api

    user = add_user("platform_admin", "task-owner")
    record = load_auth_user("task-owner")
    monkeypatch.setattr(api, "_notify", lambda *_: None)
    monkeypatch.setattr(
        app_deployment,
        "execute_command",
        lambda *a, **k: pytest.fail("撤权后不得执行外部步骤"),
    )
    with policy.bind_actor(record, {"request_id": "task-request"}):
        cluster = api.deploy.__wrapped__(
            Request({"type": "http", "headers": []}),
            ClusterSchema(
                name="test",
                helm_chart="chart",
                helm_chart_version="1",
                config={},
            ),
            BackgroundTasks(),
            SimpleNamespace(username="task-owner"),
        )
    with engine_module.get_db() as db:
        task = db.query(Task).filter_by(resource_id=cluster.cluster_id).one()
        task_id = task.task_id
        assert task.auth_context["actor_id"] == user["user_id"]
    update(load_auth_user("admin")["user_id"], user, role="readonly")
    with pytest.raises(HTTPException):
        execute_task_function(task_id, "", {})
    with engine_module.get_db() as db:
        assert db.get(Task, task_id).status == TaskStatus.failed


def test_image_task_cannot_begin_after_initiator_is_disabled(
    secured, monkeypatch
):
    from core.image_import_worker import TaskWorker
    from core.orm.image_import import (
        ImageImportLease,
        claim_next_image_import_task,
        create_image_import_task,
        find_image_import_task,
        update_image_import_task,
    )
    from web.api import artifacts

    user = add_user("platform_admin", "image-owner")
    with policy.bind_actor(load_auth_user("image-owner")):
        task = create_image_import_task(
            "test.tar", "application/x-tar", "/not-used.tar"
        )
    update_image_import_task(task["task_id"], status="pending")
    update(load_auth_user("admin")["user_id"], user, enabled=False)
    claimed = claim_next_image_import_task("test-worker", 90)
    monkeypatch.setattr(
        artifacts,
        "execute_command",
        lambda *a, **k: pytest.fail("停用后不得导入镜像"),
    )
    worker = TaskWorker()
    try:
        worker._run_task(
            task["task_id"],
            False,
            ImageImportLease("test-worker", claimed["attempt_count"]),
        )
    finally:
        worker._executor.shutdown()
    assert find_image_import_task(task["task_id"])["status"] == "failed"


@pytest.mark.asyncio
async def test_live_platform_websocket_disconnects_on_role_revoke(
    secured, monkeypatch
):
    import asyncio

    from core.misc.websocket import ConnectionManager
    from test_websocket_auth_security import connected_socket
    from web.api import websocket
    from web.utils.auth import create_access_token

    user = add_user("platform_admin", "socket-owner")
    token, _ = create_access_token({"sub": "socket-owner"})
    manager = ConnectionManager(send_timeout=0.2)
    monkeypatch.setattr(websocket, "connection_manager", manager)
    monkeypatch.setattr(websocket, "SESSION_CHECK_INTERVAL", 0.02)
    socket, task = await connected_socket(token, manager)
    try:
        update(load_auth_user("admin")["user_id"], user, role="readonly")
        await asyncio.wait_for(task, 2)
        assert socket.closed == 1008
        assert not manager.active_connections
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_backup_restore_preserves_identity_grants_and_disabled_state(
    secured, store, tmp_path
):
    import os
    import sqlite3

    engine, _, _ = store
    user = add_user("namespace_operator", "backup-owner", ["pod.files.read"])
    update(load_auth_user("admin")["user_id"], user, enabled=False)
    before = load_auth_user("backup-owner")
    path = str(engine.url.database)
    backup = tmp_path / "identity-backup.db"
    with sqlite3.connect(path) as source, sqlite3.connect(backup) as target:
        source.backup(target)
        assert target.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    os.chmod(backup, 0o600)
    with identity.get_db() as db:
        db.execute(
            text(
                "UPDATE console_user SET enabled=1 "
                "WHERE username='backup-owner'"
            )
        )
        db.commit()
    engine.dispose()
    with sqlite3.connect(backup) as source, sqlite3.connect(path) as target:
        source.backup(target)
    identity.ensure_identity_schema()
    assert load_auth_user("backup-owner") == before
    assert before["namespaces"] == ["team-a"] and not before["enabled"]


@pytest.mark.parametrize("status,expected", [(409, 409), (401, 502), (0, 400)])
def test_audit_envelope_result_matches_http_contract(
    secured, status, expected
):
    from web.utils.access import finish

    event = audit.begin_event(action="test.envelope")
    finish(event, {"code": status, "message": "safe", "data": None})
    row = next(
        row
        for row in audit.query_events()["items"]
        if row["event_id"] == event
    )
    assert row["status_code"] == expected and row["result"] == "failed"
