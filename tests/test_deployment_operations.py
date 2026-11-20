"""模拟 SDK 验证运维写入、故障恢复、授权及不泄密边界。"""

import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from core.config import Application
from core.http_api_client.kubernetes_view_client import KubernetesViewClient
from core.orm import engine as engine_module
from core.orm.audit import AuditEvent
from core.orm.deployment_operation import DeploymentOperation
from core.orm.engine import Base, get_db
from core.orm.identity import ConsoleUser, NamespaceBinding
from core.orm.resource_revision import ResourceRevision
from core.services.deployment_operations import (
    RESTART_MARKER,
    DeploymentOperations,
    claim_operation,
    process_operation,
)
from core.services.deployment_sdk import sdk_json
from web.api.deployment_operations import OperationRequest, ScaleRequest


class Response:
    status = 200

    def __init__(self, value):
        self.data = json.dumps(value).encode()

    def close(self):
        pass

    def release_conn(self):
        pass


class Cluster:
    def __init__(self):
        self.value = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": "example",
                "namespace": "apps",
                "uid": "uid-1",
                "resourceVersion": "1",
                "generation": 1,
            },
            "spec": {
                "replicas": 1,
                "strategy": {"type": "RollingUpdate"},
                "selector": {"matchLabels": {"app": "example"}},
                "template": {
                    "metadata": {
                        "labels": {"app": "example"},
                        "annotations": {"keep": "value"},
                    },
                    "spec": {
                        "automountServiceAccountToken": False,
                        "containers": [
                            {
                                "name": "app",
                                "image": "local/app:v1",
                                "securityContext": {
                                    "runAsNonRoot": True,
                                    "allowPrivilegeEscalation": False,
                                },
                                "env": [
                                    {
                                        "name": "PASSWORD",
                                        "value": "SENSITIVE-VALUE",
                                    }
                                ],
                            }
                        ],
                        "future": {"preserve": True},
                    },
                },
            },
            "status": {
                "observedGeneration": 1,
                "replicas": 1,
                "updatedReplicas": 1,
                "readyReplicas": 1,
                "availableReplicas": 1,
            },
        }
        self.hpa, self.hpa_error = [], None
        self.rs, self.pods, self.events, self.writes = [], [], [], []
        self.failure, self.crash_after = None, False
        self.api_client = SimpleNamespace(
            configuration=SimpleNamespace(
                host="https://cluster.test", ssl_ca_cert=None
            )
        )
        self.apps = SimpleNamespace(
            read_namespaced_deployment=self.read,
            replace_namespaced_deployment_scale=self.scale,
            patch_namespaced_deployment=self.restart,
            list_namespaced_replica_set=lambda **kw: Response(
                {"items": self.rs}
            ),
        )
        self.autoscaling = SimpleNamespace(
            list_namespaced_horizontal_pod_autoscaler=self.hpas
        )
        self.core = SimpleNamespace(
            list_namespaced_pod=lambda **kw: Response({"items": self.pods}),
            list_namespaced_event=lambda **kw: Response(
                {"items": self.events}
            ),
        )

    def read(self, **kwargs):
        if self.value is None:
            raise ApiException(status=404)
        return Response(self.value)

    def hpas(self, **kwargs):
        if self.hpa_error:
            raise self.hpa_error
        return Response({"items": self.hpa})

    def update(self, body):
        if self.failure:
            raise self.failure
        assert body["metadata"]["uid"] == self.value["metadata"]["uid"]
        assert (
            body["metadata"]["resourceVersion"]
            == self.value["metadata"]["resourceVersion"]
        )
        with get_db() as db:
            assert db.query(DeploymentOperation).one().phase == "writing"
            assert db.query(ResourceRevision).one().status == "pending"
            assert db.query(AuditEvent).one().result == "pending"
        self.writes.append(copy.deepcopy(body))
        self.value["metadata"]["resourceVersion"] = "2"
        self.value["metadata"]["generation"] = 2

    def scale(self, body, **kwargs):
        assert body["kind"] == "Scale" and set(body["spec"]) == {"replicas"}
        self.update(body)
        self.value["spec"]["replicas"] = body["spec"]["replicas"]
        if self.crash_after:
            raise OSError("lost SENSITIVE-VALUE response")
        return Response(body)

    def restart(self, body, **kwargs):
        assert set(body["spec"]["template"]) == {"metadata"}
        self.update(body)
        self.value["spec"]["template"]["metadata"]["annotations"].update(
            body["spec"]["template"]["metadata"]["annotations"]
        )
        if self.crash_after:
            raise OSError("lost SENSITIVE-VALUE response")
        return Response(self.value)

    def healthy(self, replicas=None):
        replicas = (
            self.value["spec"]["replicas"] if replicas is None else replicas
        )
        self.value["status"] = {
            "observedGeneration": self.value["metadata"]["generation"],
            "replicas": replicas,
            "updatedReplicas": replicas,
            "readyReplicas": replicas,
            "availableReplicas": replicas,
        }
        template = copy.deepcopy(self.value["spec"]["template"])
        template["metadata"]["labels"]["pod-template-hash"] = "abc"
        self.rs = [
            {
                "metadata": {
                    "name": "new-rs",
                    "uid": "rs-new",
                    "ownerReferences": [
                        {
                            "uid": self.value["metadata"]["uid"],
                            "controller": True,
                            "apiVersion": "apps/v1",
                            "kind": "Deployment",
                            "name": self.value["metadata"]["name"],
                        }
                    ],
                },
                "spec": {"replicas": replicas, "template": template},
                "status": {"replicas": replicas},
            }
        ]
        self.pods = [
            {
                "metadata": {
                    "name": "pod-" + str(index),
                    "uid": "pod-" + str(index),
                    "ownerReferences": [{"uid": "rs-new", "controller": True}],
                },
                "status": {},
            }
            for index in range(replicas)
        ]


@pytest.fixture
def store(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'operations.db'}")
    sessions = scoped_session(sessionmaker(bind=engine))
    monkeypatch.setattr(engine_module, "engine", engine)
    monkeypatch.setattr(engine_module, "SessionLocal", sessions)
    monkeypatch.setattr(Application, "ROOT_DIR", str(tmp_path))
    Base.metadata.create_all(engine)
    with get_db() as db:
        for role in ("platform_admin", "namespace_operator", "readonly"):
            db.add(
                ConsoleUser(
                    user_id=role,
                    username=role,
                    password_hash="disabled",
                    role=role,
                    enabled=True,
                    auth_revision=1,
                    created_at=0,
                    updated_at=0,
                )
            )
            db.add(NamespaceBinding(user_id=role, namespace="apps"))
        db.commit()
    yield tmp_path
    sessions.remove()
    engine.dispose()


def user(role="namespace_operator"):
    return {
        "user_id": role,
        "username": role,
        "role": role,
        "auth_revision": 1,
        "enabled": True,
        "namespaces": ["apps"],
        "sensitive_permissions": [],
    }


def ops(cluster, role="namespace_operator", namespace="apps", name="example"):
    return DeploymentOperations(cluster, namespace, name, user(role))


def command(
    cluster,
    replicas=2,
    request_id="request-1",
    restart=False,
    confirm_zero=False,
):
    fields = dict(
        uid=cluster.value["metadata"]["uid"],
        resource_version=cluster.value["metadata"]["resourceVersion"],
        request_id=request_id,
    )
    return (
        OperationRequest(**fields)
        if restart
        else ScaleRequest(
            **fields, replicas=replicas, confirm_zero=confirm_zero
        )
    )


def step(cluster):
    with get_db() as db:
        for row in db.query(DeploymentOperation):
            row.next_poll = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
    claim = claim_operation()
    assert claim is not None
    process_operation(*claim, cluster)


@pytest.mark.parametrize(
    "replicas", [-1, True, False, 1.5, "2", None, 2147483648]
)
def test_strict_replica_validation(replicas):
    with pytest.raises(ValidationError):
        ScaleRequest(
            uid="x", resource_version="1", request_id="one", replicas=replicas
        )


@pytest.mark.parametrize(
    "action,replicas", [("scale", 3), ("scale", 0), ("restart", 1)]
)
def test_accept_write_observe_history_audit_and_replay(
    store, action, replicas
):
    cluster = Cluster()
    before = copy.deepcopy(cluster.value["spec"]["template"])
    body = command(
        cluster,
        replicas,
        restart=action == "restart",
        confirm_zero=replicas == 0,
    )
    operation = ops(cluster).submit(action, body)["operation"]
    assert operation["status"] == "pending" and not cluster.writes
    step(cluster)
    assert ops(cluster).get(operation["id"])["writeStatus"] == "accepted"
    assert ops(cluster).get(operation["id"])["status"] == "running"
    if action == "restart":
        assert (
            RESTART_MARKER
            in cluster.value["spec"]["template"]["metadata"]["annotations"]
        )
        del cluster.value["spec"]["template"]["metadata"]["annotations"][
            RESTART_MARKER
        ]
        assert cluster.value["spec"]["template"] == before
        cluster.value["spec"]["template"]["metadata"]["annotations"][
            RESTART_MARKER
        ] = cluster.writes[0]["spec"]["template"]["metadata"]["annotations"][
            RESTART_MARKER
        ]
    cluster.healthy()
    step(cluster)
    result = ops(cluster).get(operation["id"])
    assert result["status"] == "succeeded"
    assert result["progress"]["old"] == 0
    assert "SENSITIVE-VALUE" not in json.dumps(result)
    assert ops(cluster).submit(action, body)["replayed"]
    assert len(cluster.writes) == 1
    with get_db() as db:
        assert (
            db.query(DeploymentOperation).count()
            == db.query(ResourceRevision).count()
            == 1
        )
        revision = db.query(ResourceRevision).one()
        assert revision.status == "applied" and revision.after_rv == "2"
        assert "SENSITIVE-VALUE" not in revision.snapshots
        audit = db.query(AuditEvent).one()
        assert (
            audit.action == "deployment." + action
            and audit.revision_id == str(revision.id)
        )
        assert audit.task_id == operation["id"] and audit.result == "accepted"


@pytest.mark.parametrize(
    "field,value",
    [
        ("paused", True),
        ("deleting", True),
        ("uid", "recreated"),
        ("rv", "2"),
        ("hpa", True),
        ("hpa_error", True),
    ],
)
def test_reject_unsafe_targets(store, field, value):
    cluster = Cluster()
    body = command(cluster)
    if field == "paused":
        cluster.value["spec"]["paused"] = value
    if field == "deleting":
        cluster.value["metadata"]["deletionTimestamp"] = "now"
    if field == "uid":
        cluster.value["metadata"]["uid"] = value
    if field == "rv":
        cluster.value["metadata"]["resourceVersion"] = value
    if field == "hpa":
        cluster.hpa = [
            {
                "metadata": {"name": "autoscaler"},
                "spec": {
                    "scaleTargetRef": {
                        "kind": "Deployment",
                        "name": "example",
                        "apiVersion": "apps/v1",
                    }
                },
            }
        ]
    if field == "hpa_error":
        cluster.hpa_error = ApiException(status=403)
    with pytest.raises((HTTPException, ApiException)):
        ops(cluster).submit("scale", body)
    assert not cluster.writes
    with get_db() as db:
        assert db.query(DeploymentOperation).count() == 0


def test_zero_confirmation_noop_and_workload_validation(store):
    cluster = Cluster()
    for body in [command(cluster, 0), command(cluster, 1)]:
        with pytest.raises(HTTPException) as error:
            ops(cluster).submit("scale", body)
        assert error.value.status_code == 409
    cluster.value["spec"]["template"]["spec"]["hostNetwork"] = True
    with pytest.raises(HTTPException) as error:
        ops(cluster).submit("scale", command(cluster))
    assert error.value.status_code == 403 and not cluster.writes


def test_idempotency_conflict_and_concurrent_target_lock(store):
    cluster = Cluster()
    body = command(cluster)

    def submit(_):
        try:
            return ops(cluster).submit("scale", body)["operation"]["id"]
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    assert results[0] == results[1]
    for body in [
        command(cluster, 3),
        command(cluster, 2, request_id="different"),
    ]:
        with pytest.raises(HTTPException) as error:
            ops(cluster).submit("scale", body)
        assert error.value.status_code == 409


@pytest.mark.parametrize(
    "failure,status",
    [
        (ApiException(status=409), "failed"),
        (OSError("SENSITIVE-VALUE"), "timed_out"),
    ],
)
def test_write_failure_is_recorded_and_never_replayed(store, failure, status):
    cluster = Cluster()
    body = command(cluster, restart=True)
    operation = ops(cluster).submit("restart", body)["operation"]
    cluster.failure = failure
    step(cluster)
    assert ops(cluster).get(operation["id"])["status"] == status
    assert "SENSITIVE-VALUE" not in json.dumps(
        ops(cluster).get(operation["id"])
    )
    cluster.failure = None
    assert ops(cluster).submit("restart", body)["replayed"]
    if status == "timed_out":
        step(cluster)
    assert not cluster.writes


def test_lost_response_can_verify_success_without_second_restart(store):
    cluster = Cluster()
    cluster.crash_after = True
    body = command(cluster, restart=True)
    operation = ops(cluster).submit("restart", body)["operation"]
    step(cluster)
    assert ops(cluster).get(operation["id"])["status"] == "timed_out"
    cluster.healthy()
    step(cluster)
    assert ops(cluster).get(operation["id"])["status"] == "succeeded"
    assert ops(cluster).get(operation["id"])["writeStatus"] == "accepted"
    assert (
        ops(cluster).submit("restart", body)["replayed"]
        and len(cluster.writes) == 1
    )


def test_process_crash_after_intent_and_stale_lease_never_write_again(
    store, monkeypatch
):
    cluster = Cluster()
    operation = ops(cluster).submit("restart", command(cluster, restart=True))[
        "operation"
    ]
    original = cluster.apps.patch_namespaced_deployment

    def crash(**kwargs):
        raise KeyboardInterrupt()

    cluster.apps.patch_namespaced_deployment = crash
    first = claim_operation()
    with pytest.raises(KeyboardInterrupt):
        process_operation(*first, cluster)
    with get_db() as db:
        row = db.get(DeploymentOperation, operation["id"])
        assert row.phase == "writing"
        row.lease_until = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
    cluster.apps.patch_namespaced_deployment = original
    step(cluster)
    assert not cluster.writes
    with pytest.raises(RuntimeError):
        process_operation(*first, cluster)


@pytest.mark.parametrize(
    "change", ["enabled", "role", "namespace", "credentials"]
)
def test_revocation_before_execution_prevents_write(store, change):
    cluster = Cluster()
    operation = ops(cluster).submit("scale", command(cluster))["operation"]
    with get_db() as db:
        actor = db.get(ConsoleUser, "namespace_operator")
        if change == "enabled":
            actor.enabled = False
        if change == "role":
            actor.role = "readonly"
        if change == "namespace":
            db.query(NamespaceBinding).filter_by(
                user_id=actor.user_id
            ).delete()
        if change == "credentials":
            actor.auth_revision += 1
        db.commit()
    step(cluster)
    assert not cluster.writes
    assert (
        ops(cluster, "platform_admin").get(operation["id"])["status"]
        == "permission_revoked"
    )


@pytest.mark.parametrize("change", ["uid", "generation", "spec", "deleted"])
def test_external_changes_cannot_be_mistaken_for_success(store, change):
    cluster = Cluster()
    operation = ops(cluster).submit("restart", command(cluster, restart=True))[
        "operation"
    ]
    step(cluster)
    cluster.healthy()
    if change == "uid":
        cluster.value["metadata"]["uid"] = "other-uid"
    if change == "generation":
        cluster.value["metadata"]["generation"] = 3
    if change == "spec":
        cluster.value["spec"]["template"]["spec"]["containers"][0][
            "image"
        ] = "other"
    if change == "deleted":
        cluster.value = None
    step(cluster)
    assert ops(cluster).get(operation["id"])["status"] == "superseded"


@pytest.mark.parametrize("condition", ["deadline", "quota"])
def test_explicit_deployment_failure_and_safe_diagnostics(store, condition):
    cluster = Cluster()
    operation = ops(cluster).submit("restart", command(cluster, restart=True))[
        "operation"
    ]
    step(cluster)
    cluster.healthy()
    cluster.value["status"]["conditions"] = (
        [
            {
                "type": "Progressing",
                "status": "False",
                "reason": "ProgressDeadlineExceeded",
                "message": "SENSITIVE-VALUE",
            }
        ]
        if condition == "deadline"
        else [
            {
                "type": "ReplicaFailure",
                "status": "True",
                "reason": "FailedCreate",
                "message": "SENSITIVE-VALUE",
            }
        ]
    )
    cluster.pods[0]["status"] = {
        "containerStatuses": [
            {
                "state": {
                    "waiting": {
                        "reason": "ImagePullBackOff",
                        "message": "SENSITIVE-VALUE",
                    }
                }
            }
        ]
    }
    cluster.events = [
        {
            "involvedObject": {"uid": "pod-0"},
            "reason": "Unhealthy",
            "message": "SENSITIVE-VALUE",
        },
        {"involvedObject": {"uid": "unrelated"}, "reason": "FailedScheduling"},
    ]
    step(cluster)
    result = ops(cluster).get(operation["id"])
    assert result["status"] == "failed"
    assert {item["code"] for item in result["progress"]["diagnostics"]} == {
        "ImagePullBackOff",
        "Unhealthy",
    }
    assert "SENSITIVE-VALUE" not in json.dumps(result)


def test_ready_pods_alone_and_old_terminating_replicas_are_insufficient(store):
    cluster = Cluster()
    operation = ops(cluster).submit("restart", command(cluster, restart=True))[
        "operation"
    ]
    step(cluster)
    cluster.healthy()
    cluster.value["status"]["observedGeneration"] = 1
    step(cluster)
    assert ops(cluster).get(operation["id"])["status"] == "running"
    cluster.value["status"]["observedGeneration"] = 2
    old = copy.deepcopy(cluster.rs[0])
    old["metadata"]["uid"] = "old-rs"
    old["spec"]["template"]["metadata"]["annotations"] = {}
    old["spec"]["replicas"] = old["status"]["replicas"] = 0
    old["status"]["terminatingReplicas"] = 1
    cluster.rs.append(old)
    with get_db() as db:
        db.get(DeploymentOperation, operation["id"]).deadline = (
            datetime.utcnow() - timedelta(seconds=1)
        )
        db.commit()
    step(cluster)
    assert ops(cluster).get(operation["id"])["status"] == "timed_out"
    cluster.rs.pop()
    step(cluster)
    assert ops(cluster).get(operation["id"])["status"] == "succeeded"


def test_storage_failure_blocks_cluster_write(store, monkeypatch):
    from core.services import deployment_operations as service

    cluster = Cluster()

    def unavailable():
        raise HTTPException(503, "history unavailable")

    monkeypatch.setattr(service, "history_cipher", unavailable)
    with pytest.raises(HTTPException) as error:
        ops(cluster).submit("scale", command(cluster))
    assert error.value.status_code == 503 and not cluster.writes


def test_routes_all_roles_namespaces_and_operation_boundaries(
    store, monkeypatch
):
    from web.api import deployment_operations as routes
    from web.utils import auth

    cluster = Cluster()
    monkeypatch.setattr(routes, "get_kubernetes_client", lambda: cluster)
    role = "platform_admin"

    async def identity(authorization=None, **kwargs):
        if authorization != "Bearer test":
            raise HTTPException(401)
        return SimpleNamespace(**user(role)), "test"

    monkeypatch.setattr(auth, "get_current_user", identity)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1/k8s")

    async def run():
        nonlocal role
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            base = "/api/v1/k8s/deployments/apps/example"
            assert (await http.get(base + "/operations")).status_code == 401
            assert (
                await http.post(base + "/scale", json={})
            ).status_code == 401
            http.headers["Authorization"] = "Bearer test"
            role = "namespace_operator"
            response = await http.post(
                base + "/scale", json=command(cluster).model_dump()
            )
            assert response.status_code == 202, response.text
            record = response.json()["data"]["operation"]
            role = "readonly"
            assert (
                await http.get(base + "/operations/" + record["id"])
            ).status_code == 200
            assert (
                await http.post(
                    base + "/scale", json=command(cluster).model_dump()
                )
            ).status_code == 403
            assert (
                await http.get(
                    base.replace("/apps/", "/other/")
                    + "/operations/"
                    + record["id"]
                )
            ).status_code == 403
            assert (
                await http.get(
                    base.replace("/example", "/another")
                    + "/operations/"
                    + record["id"]
                )
            ).status_code == 404
            role = "platform_admin"
            assert (
                await http.get(
                    base.replace("/apps/", "/other/")
                    + "/operations/"
                    + record["id"]
                )
            ).status_code == 404

    asyncio.run(run())


def test_installed_sdk_raw_scale_restart_and_list_compatibility(monkeypatch):
    api = client.ApiClient(client.Configuration(host="https://cluster.test"))
    view = KubernetesViewClient(api_client=api)
    calls = []

    def transport(*args, **kwargs):
        calls.append((args, kwargs))
        return Response({"items": [], "future": True})

    monkeypatch.setattr(api.rest_client, "request", transport)
    try:
        for sdk, method, kwargs in [
            (
                "apps",
                "replace_namespaced_deployment_scale",
                {
                    "name": "example",
                    "body": {
                        "metadata": {"uid": "one", "resourceVersion": "1"},
                        "spec": {"replicas": 2},
                    },
                },
            ),
            (
                "apps",
                "patch_namespaced_deployment",
                {
                    "name": "example",
                    "body": {
                        "spec": {
                            "template": {
                                "metadata": {
                                    "annotations": {RESTART_MARKER: "one"}
                                }
                            }
                        }
                    },
                    "content_type": "application/strategic-merge-patch+json",
                },
            ),
            (
                "apps",
                "list_namespaced_replica_set",
                {"limit": 500, "_continue": None},
            ),
            (
                "autoscaling",
                "list_namespaced_horizontal_pod_autoscaler",
                {"limit": 500, "_continue": None},
            ),
            ("core", "list_namespaced_pod", {"limit": 500, "_continue": None}),
            (
                "core",
                "list_namespaced_event",
                {"limit": 500, "_continue": None},
            ),
        ]:
            assert sdk_json(view, sdk, method, namespace="apps", **kwargs)[
                "future"
            ]
        assert calls[0][1]["body"]["metadata"]["uid"] == "one"
        assert "/scale" in calls[0][0][1]
        # 老 SDK 在 REST 层将字典补丁转换为 strategic merge；这里拦截
        # 的 request 尚未进入该层。37 的序列化器已显式设置类型。
        expected = (
            "application/strategic-merge-patch+json"
            if hasattr(view.apps, "_patch_namespaced_deployment_serialize")
            else "application/json-patch+json"
        )
        assert calls[1][1]["headers"]["Content-Type"] == expected
    finally:
        api.close()


def test_concurrent_different_requests_only_one_is_admitted(store):
    cluster = Cluster()

    def submit(index):
        try:
            return ops(cluster).submit(
                "scale", command(cluster, request_id=f"request-{index}")
            )["operation"]["status"]
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(submit, range(2)))
    assert result.count("pending") == result.count(409) == 1
    with get_db() as db:
        assert (
            db.query(DeploymentOperation).count()
            == db.query(ResourceRevision).count()
            == 1
        )


def test_audit_insert_failure_rolls_back_all_intent_records(
    store, monkeypatch
):
    from sqlalchemy.exc import SQLAlchemyError

    from core.services import deployment_operations as service

    cluster = Cluster()

    def fail(**kwargs):
        raise SQLAlchemyError("disk failure")

    monkeypatch.setattr(service, "AuditEvent", fail)
    with pytest.raises(HTTPException) as error:
        ops(cluster).submit("scale", command(cluster))
    assert error.value.status_code == 503 and not cluster.writes
    with get_db() as db:
        assert (
            db.query(DeploymentOperation).count()
            == db.query(ResourceRevision).count()
            == 0
        )


def test_finalize_failure_recovers_without_replaying_write(store, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    from core.services import deployment_operations as service

    cluster = Cluster()
    operation = ops(cluster).submit("restart", command(cluster, restart=True))[
        "operation"
    ]
    original = service.record_write

    def fail(*args, **kwargs):
        raise SQLAlchemyError("disk unavailable after cluster response")

    monkeypatch.setattr(service, "record_write", fail)
    with pytest.raises(SQLAlchemyError):
        step(cluster)
    with get_db() as db:
        row = db.get(DeploymentOperation, operation["id"])
        assert row.phase == "writing"
        row.lease_until = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
    monkeypatch.setattr(service, "record_write", original)
    cluster.healthy()
    step(cluster)
    assert ops(cluster).get(operation["id"])["status"] == "succeeded"
    assert len(cluster.writes) == 1


def test_hpa_takes_control_before_worker_write(store):
    cluster = Cluster()
    operation = ops(cluster).submit("scale", command(cluster))["operation"]
    cluster.hpa = [
        {
            "metadata": {"name": "new-hpa"},
            "spec": {
                "scaleTargetRef": {
                    "kind": "Deployment",
                    "apiVersion": "apps/v1",
                    "name": "example",
                }
            },
        }
    ]
    step(cluster)
    result = ops(cluster).get(operation["id"])
    assert result["status"] == "failed" and "HPA" in result["reason"]
    assert not cluster.writes


def test_queued_timeout_never_sends_a_late_write(store):
    cluster = Cluster()
    operation = ops(cluster).submit("scale", command(cluster))["operation"]
    with get_db() as db:
        db.get(DeploymentOperation, operation["id"]).deadline = (
            datetime.utcnow() - timedelta(seconds=1)
        )
        db.commit()
    step(cluster)
    assert ops(cluster).get(operation["id"])["status"] == "failed"
    assert not cluster.writes


def test_revocation_after_write_preserves_lock_and_later_readonly_verification(
    store,
):
    cluster = Cluster()
    operation = ops(cluster).submit("restart", command(cluster, restart=True))[
        "operation"
    ]
    step(cluster)
    with get_db() as db:
        db.get(ConsoleUser, "namespace_operator").enabled = False
        db.commit()
    step(cluster)
    assert (
        ops(cluster, "platform_admin").get(operation["id"])["status"]
        == "timed_out"
    )
    with pytest.raises(HTTPException) as error:
        ops(cluster, "platform_admin").submit(
            "scale", command(cluster, request_id="new-admin-request")
        )
    assert error.value.status_code == 409
    with get_db() as db:
        actor = db.get(ConsoleUser, "namespace_operator")
        actor.enabled, actor.auth_revision = True, actor.auth_revision + 1
        db.commit()
    cluster.healthy()
    step(cluster)
    assert ops(cluster).get(operation["id"])["status"] == "succeeded"
    assert len(cluster.writes) == 1


def test_schema_migration_is_idempotent(store):
    from core.orm.deployment_operation import (
        ensure_deployment_operation_schema,
    )

    cluster = Cluster()
    operation = ops(cluster).submit("scale", command(cluster))["operation"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(lambda _: ensure_deployment_operation_schema(), range(2))
        )
    assert ops(cluster).get(operation["id"])["status"] == "pending"


def test_observation_lease_renewal_cannot_revive_stale_worker(store):
    from core.services.deployment_operations import renew_operation

    cluster = Cluster()
    operation = ops(cluster).submit("scale", command(cluster))["operation"]
    claim = claim_operation()
    assert renew_operation(*claim)
    with get_db() as db:
        row = db.get(DeploymentOperation, operation["id"])
        assert row.lease_until > datetime.utcnow() + timedelta(seconds=170)
        row.lease_until = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
    new_claim = claim_operation()
    assert new_claim and new_claim != claim
    with pytest.raises(RuntimeError):
        renew_operation(*claim)
    assert renew_operation(*new_claim)
