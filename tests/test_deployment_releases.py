"""发布预检/受理/执行的隔离 SDK 回归，覆盖历史、安全与故障恢复。"""

import asyncio
import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from sqlalchemy import text

import test_deployment_operations as operations_tests
from core.http_api_client.kubernetes_view_client import KubernetesViewClient
from core.orm.audit import AuditEvent
from core.orm.deployment_operation import (
    DeploymentOperation,
    ensure_deployment_operation_schema,
)
from core.orm.engine import get_db
from core.orm.identity import NamespaceBinding
from core.orm.resource_revision import ResourceRevision
from core.services.deployment_releases import (
    DeploymentReleases,
    patch_template,
)
from core.services.resource_yaml import history_cipher
from test_deployment_operations import (
    Cluster,
    Response,
    command,
    ops,
    step,
    user,
)
from web.api.deployment_operations import (
    ImagePreviewRequest,
    ImageRequest,
    RollbackPreviewRequest,
    RollbackRequest,
)

store = operations_tests.store


class ReleaseCluster(Cluster):
    def __init__(self):
        super().__init__()
        self.dry_runs = []
        self.admission = lambda value: value
        self.dry_failure = None
        old = copy.deepcopy(self.value["spec"]["template"])
        old["spec"]["containers"][0]["image"] = "local/app:v0"
        old["metadata"]["labels"]["pod-template-hash"] = "old"
        self.rs = [
            {
                "metadata": {
                    "name": "example-old",
                    "uid": "rs-old",
                    "annotations": {"deployment.kubernetes.io/revision": "1"},
                    "ownerReferences": [
                        {
                            "apiVersion": "apps/v1",
                            "kind": "Deployment",
                            "name": "example",
                            "uid": "uid-1",
                            "controller": True,
                        }
                    ],
                },
                "spec": {"template": old, "replicas": 0},
                "status": {"replicas": 0},
            }
        ]

    def restart(self, body, **kwargs):
        if not isinstance(body, list):
            return super().restart(body, **kwargs)
        assert [item["op"] for item in body] == ["test", "test", "replace"]
        assert [item["path"] for item in body] == [
            "/metadata/uid",
            "/metadata/resourceVersion",
            "/spec/template",
        ]
        for item in body[:2]:
            assert (
                self.value["metadata"][item["path"].rsplit("/", 1)[-1]]
                == item["value"]
            )
        assert kwargs["field_validation"] == "Strict"
        result = copy.deepcopy(self.value)
        result["spec"]["template"] = copy.deepcopy(body[2]["value"])
        result = self.admission(result)
        if kwargs.get("dry_run") == "All":
            if self.dry_failure:
                raise self.dry_failure
            self.dry_runs.append(copy.deepcopy(body))
            return Response(result)
        self.update({"metadata": self.value["metadata"], "spec": {}})
        result["metadata"] = copy.deepcopy(self.value["metadata"])
        self.value = result
        if self.crash_after:
            raise OSError("SENSITIVE-VALUE lost response")
        return Response(result)


def preview_body(cluster, action="image", **changes):
    value = {
        "uid": cluster.value["metadata"]["uid"],
        "resource_version": cluster.value["metadata"]["resourceVersion"],
    }
    if action == "image":
        value["containers"] = [{"name": "app", "image": "local/app:v2"}]
        cls = ImagePreviewRequest
    else:
        value.update(revision="1", replica_set_uid="rs-old")
        cls = RollbackPreviewRequest
    return cls(**dict(value, **changes))


def release_body(
    cluster, action="image", role="namespace_operator", **changes
):
    body = preview_body(cluster, action, **changes)
    preview = DeploymentReleases(ops(cluster, role)).preview(action, body)
    cls = ImageRequest if action == "image" else RollbackRequest
    return cls(
        **body.model_dump(),
        preview_token=preview["previewToken"],
        request_id="release-1",
    )


@pytest.mark.parametrize("action", ["image", "rollback"])
def test_release_preserves_non_template_and_tracks_health(store, action):
    cluster = ReleaseCluster()
    cluster.hpa = [
        {
            "metadata": {"name": "autoscale"},
            "spec": {
                "scaleTargetRef": {
                    "kind": "Deployment",
                    "apiVersion": "apps/v1",
                    "name": "example",
                }
            },
        }
    ]
    original = copy.deepcopy(cluster.value["spec"])
    body = release_body(cluster, action)
    record = ops(cluster).submit(action, body)["operation"]
    assert record["action"] == action
    assert record["release"].get("sourceRevision", "") == (
        "1" if action == "rollback" else ""
    )
    assert not cluster.writes
    step(cluster)
    assert len(cluster.writes) == 1
    assert cluster.value["spec"]["template"]["spec"]["containers"][0][
        "image"
    ] == ("local/app:v2" if action == "image" else "local/app:v0")
    for field in original.keys() - {"template"}:
        assert cluster.value["spec"][field] == original[field]
    assert (
        "pod-template-hash"
        not in cluster.value["spec"]["template"]["metadata"]["labels"]
    )
    assert "SENSITIVE-VALUE" not in json.dumps(record)
    cluster.healthy()
    step(cluster)
    result = ops(cluster).get(record["id"])
    assert (
        result["status"] == "succeeded" and result["writeStatus"] == "accepted"
    )
    with get_db() as db:
        revision = db.get(ResourceRevision, record["revisionId"])
        assert (
            revision.status == "applied"
            and "SENSITIVE-VALUE" not in revision.snapshots
        )
        snapshot = json.loads(
            history_cipher().decrypt(revision.snapshots.encode())
        )
        assert snapshot["before"]["spec"] == original
        assert snapshot["after"]["spec"] == cluster.value["spec"]
        assert db.get(AuditEvent, record["auditId"]).task_id == record["id"]


def test_multi_container_by_name_preserves_unselected_fields(store):
    cluster = ReleaseCluster()
    cluster.value["spec"]["template"]["spec"]["containers"].append(
        {
            "name": "sidecar",
            "image": "local/side:v1",
            "securityContext": {
                "runAsNonRoot": True,
                "allowPrivilegeEscalation": False,
            },
        }
    )
    body = release_body(
        cluster,
        containers=[
            {"name": "sidecar", "image": "local/side:v3"},
            {"name": "app", "image": "host:5000/a/app@sha256:" + "a" * 64},
        ],
    )
    ops(cluster).submit("image", body)
    step(cluster)
    containers = cluster.value["spec"]["template"]["spec"]["containers"]
    assert [item["name"] for item in containers] == ["app", "sidecar"]
    assert containers[0]["env"][0]["value"] == "SENSITIVE-VALUE"
    assert containers[1]["image"] == "local/side:v3"


@pytest.mark.parametrize(
    "containers,code",
    [
        ([{"name": "unknown", "image": "app:v2"}], 422),
        ([{"name": "app", "image": "a:v2"}] * 2, 422),
        ([{"name": "app", "image": "local/app:v1"}], 409),
        *[
            ([{"name": "app", "image": image}], 422)
            for image in [
                "app",
                "https://repo/app:v2",
                "app:v2 space",
                "app@sha256:abc",
                "app:",
                "UPPER:v2",
                "app:v2\n",
            ]
        ],
    ],
)
def test_invalid_images_never_write(store, containers, code):
    cluster = ReleaseCluster()
    with pytest.raises(HTTPException) as error:
        DeploymentReleases(ops(cluster)).preview(
            "image", preview_body(cluster, containers=containers)
        )
    assert error.value.status_code == code
    assert not cluster.writes


@pytest.mark.parametrize(
    "field,value",
    [
        ("uid", "old-uid"),
        ("kind", "StatefulSet"),
        ("apiVersion", "apps/v2"),
        ("name", "other"),
        ("controller", False),
    ],
)
def test_history_owner_reference_is_strict(store, field, value):
    cluster = ReleaseCluster()
    cluster.rs[0]["metadata"]["ownerReferences"][0][field] = value
    assert DeploymentReleases(ops(cluster)).history()["items"] == []


def test_history_summaries_invalid_revision_pagination_and_redaction(store):
    cluster = ReleaseCluster()
    duplicate = copy.deepcopy(cluster.rs[0])
    duplicate["metadata"].update(name="duplicate", uid="duplicate")
    missing = copy.deepcopy(duplicate)
    missing["metadata"].update(name="missing", uid="missing", annotations={})
    invalid = copy.deepcopy(missing)
    invalid["metadata"].update(
        name="invalid",
        uid="invalid",
        annotations={
            "deployment.kubernetes.io/revision": "SENSITIVE-REVISION"
        },
    )
    cluster.rs.extend([duplicate, missing, invalid])
    pages = []

    def list_page(**kwargs):
        pages.append(kwargs.get("_continue"))
        return Response(
            {
                "items": (
                    cluster.rs[:1]
                    if not kwargs.get("_continue")
                    else cluster.rs[1:]
                ),
                "metadata": {
                    "continue": "next" if not kwargs.get("_continue") else ""
                },
            }
        )

    cluster.apps.list_namespaced_replica_set = list_page
    value = DeploymentReleases(ops(cluster)).history()
    assert pages == [None, "next"]
    assert all(not item["canRollback"] for item in value["items"])
    assert "SENSITIVE-VALUE" not in json.dumps(value)
    assert "SENSITIVE-REVISION" not in json.dumps(value)
    assert "env" not in json.dumps(value)


@pytest.mark.parametrize(
    "mutation", ["deleted", "deleting", "changed", "duplicate", "owner"]
)
def test_rollback_source_changed_after_preview_or_acceptance(store, mutation):
    cluster = ReleaseCluster()
    body = release_body(cluster, "rollback")

    def mutate():
        if mutation == "deleted":
            cluster.rs = []
        elif mutation == "deleting":
            cluster.rs[0]["metadata"]["deletionTimestamp"] = "now"
        elif mutation == "changed":
            cluster.rs[0]["spec"]["template"]["spec"]["containers"][0][
                "image"
            ] = "app:evil"
        elif mutation == "duplicate":
            cluster.rs.append(copy.deepcopy(cluster.rs[0]))
        else:
            cluster.rs[0]["metadata"]["ownerReferences"][0]["uid"] = "other"

    original = copy.deepcopy(cluster.rs)
    mutate()
    with pytest.raises(HTTPException) as error:
        ops(cluster).submit("rollback", body)
    assert error.value.status_code == 409
    cluster.rs = original
    record = ops(cluster).submit("rollback", body)["operation"]
    mutate()
    step(cluster)
    assert ops(cluster).get(record["id"])["status"] == "failed"
    assert not cluster.writes


@pytest.mark.parametrize(
    "unsafe", ["init", "serviceaccount", "hostpath", "secret"]
)
def test_rollback_rechecks_complete_historical_template_security(
    store, unsafe
):
    cluster = ReleaseCluster()
    spec = cluster.rs[0]["spec"]["template"]["spec"]
    if unsafe == "init":
        spec["initContainers"] = [
            {
                "name": "init",
                "image": "busybox:v1",
                "securityContext": {"privileged": True},
            }
        ]
    elif unsafe == "serviceaccount":
        spec["serviceAccountName"] = "admin"
    elif unsafe == "hostpath":
        spec["volumes"] = [{"name": "host", "hostPath": {"path": "/"}}]
    else:
        spec["volumes"] = [
            {"name": "secret", "secret": {"secretName": "credential"}}
        ]
    with pytest.raises(HTTPException) as error:
        DeploymentReleases(ops(cluster)).preview(
            "rollback", preview_body(cluster, "rollback")
        )
    assert error.value.status_code == 403
    # 管理员可查看差异，但仍不能看到模板里的凭据值。
    result = DeploymentReleases(ops(cluster, "platform_admin")).preview(
        "rollback", preview_body(cluster, "rollback")
    )
    assert "SENSITIVE-VALUE" not in json.dumps(result)
    assert all(
        item["redacted"]
        for item in result["changes"]
        if not item["path"].endswith("/image")
    )


@pytest.mark.parametrize(
    "mutation", ["input", "actor", "expired", "rv", "uid", "admission"]
)
def test_preview_binding_conflicts(store, mutation, monkeypatch):
    cluster = ReleaseCluster()
    body = release_body(cluster)
    role = "namespace_operator"
    if mutation == "input":
        body.containers[0].image = "app:v3"
    elif mutation == "actor":
        role = "platform_admin"
    elif mutation == "expired":
        cipher = history_cipher()
        raw = cipher.decrypt(body.preview_token.encode())
        body.preview_token = cipher.encrypt_at_time(
            raw, int(time.time()) - 301
        ).decode()
    elif mutation in {"rv", "uid"}:
        cluster.value["metadata"][
            "resourceVersion" if mutation == "rv" else "uid"
        ] = "other"
    else:

        def admission(value):
            value["spec"]["template"]["metadata"]["annotations"][
                "injected"
            ] = "new"
            return value

        cluster.admission = admission
    with pytest.raises(HTTPException) as error:
        ops(cluster, role).submit("image", body)
    assert error.value.status_code == 409 and not cluster.writes


def test_idempotency_cross_action_lock_and_expired_retry(store):
    cluster = ReleaseCluster()
    body = release_body(cluster)
    first = ops(cluster).submit("image", body)
    assert (
        ops(cluster).submit("image", body)["operation"]["id"]
        == first["operation"]["id"]
    )
    with pytest.raises(HTTPException):
        ops(cluster).submit("restart", command(cluster, request_id="another"))
    body.containers[0].image = "app:v3"
    with pytest.raises(HTTPException):
        ops(cluster).submit("image", body)
    assert not cluster.writes


@pytest.mark.parametrize("action", ["image", "rollback"])
def test_release_response_loss_and_worker_restart_do_not_replay(store, action):
    cluster = ReleaseCluster()
    body = release_body(cluster, action)
    record = ops(cluster).submit(action, body)["operation"]
    cluster.crash_after = True
    step(cluster)
    assert ops(cluster).get(record["id"])["status"] == "timed_out"
    assert ops(cluster).submit(action, body)["replayed"]
    step(cluster)
    assert len(cluster.writes) == 1
    cluster.healthy()
    step(cluster)
    assert ops(cluster).get(record["id"])["status"] == "succeeded"


def test_release_revoked_and_server_rejection(store):
    cluster = ReleaseCluster()
    record = ops(cluster).submit("image", release_body(cluster))["operation"]
    with get_db() as db:
        db.query(NamespaceBinding).filter_by(
            user_id="namespace_operator"
        ).delete()
        db.commit()
    step(cluster)
    assert (
        ops(cluster, "platform_admin").get(record["id"])["status"]
        == "permission_revoked"
    )
    assert not cluster.writes


def test_worker_rechecks_admission_and_source_before_writing(store):
    cluster = ReleaseCluster()
    record = ops(cluster).submit("image", release_body(cluster))["operation"]
    cluster.dry_failure = ApiException(status=422, reason="SENSITIVE-VALUE")
    step(cluster)
    assert not cluster.writes
    assert "SENSITIVE-VALUE" not in json.dumps(ops(cluster).get(record["id"]))


def test_migration_is_idempotent_and_keeps_old_rows(store):
    cluster = Cluster()
    record = ops(cluster).submit("scale", command(cluster))["operation"]
    with get_db() as db:
        columns = ", ".join(
            '"' + column.name + '"'
            for column in DeploymentOperation.__table__.columns
            if column.name != "release_context"
        )
        db.execute(
            text(
                f"CREATE TABLE old_operations AS SELECT {columns} "
                "FROM deployment_operation"
            )
        )
        db.execute(text("DROP TABLE deployment_operation"))
        db.execute(
            text("ALTER TABLE old_operations RENAME TO deployment_operation")
        )
        db.commit()
    ensure_deployment_operation_schema()
    ensure_deployment_operation_schema()
    assert ops(cluster).get(record["id"])["release"] == {}
    step(cluster)
    cluster.healthy()
    step(cluster)
    assert ops(cluster).get(record["id"])["status"] == "succeeded"


def test_installed_sdk_json_patch_preserves_raw_template_and_strict_options(
    monkeypatch,
):
    api = client.ApiClient(client.Configuration(host="https://cluster.test"))
    view = KubernetesViewClient(api_client=api)
    calls = []

    def transport(*args, **kwargs):
        calls.append((args, kwargs))
        return Response({"spec": {"template": {"future": True}}})

    monkeypatch.setattr(api.rest_client, "request", transport)
    cluster = ReleaseCluster()
    try:
        result = patch_template(
            ops(view, "platform_admin"),
            cluster.value,
            cluster.value["spec"]["template"],
            dry_run=True,
        )
        assert result["spec"]["template"]["future"]
        args, kwargs = calls[0]
        assert (
            kwargs["headers"]["Content-Type"] == "application/json-patch+json"
        )
        query = parse_qs(urlsplit(args[1]).query)
        if not query:
            # SDK 34–36 在 REST 层编码查询参数，37 已写入 URL。
            query = {key: [value] for key, value in kwargs["query_params"]}
        assert query["dryRun"] == ["All"]
        assert query["fieldValidation"] == ["Strict"]
        assert kwargs["body"][2]["value"]["spec"]["future"]["preserve"]
    finally:
        api.close()


def test_release_http_role_namespace_and_sensitive_boundaries(
    store, monkeypatch
):
    from web.api import deployment_operations as routes
    from web.utils import auth

    cluster = ReleaseCluster()
    monkeypatch.setattr(routes, "get_kubernetes_client", lambda: cluster)
    role = "readonly"

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
            assert (await http.get(base + "/history")).status_code == 401
            http.headers["Authorization"] = "Bearer test"
            assert (await http.get(base + "/history")).status_code == 200
            assert (
                await http.get(base.replace("/apps/", "/other/") + "/history")
            ).status_code == 403
            for action in ("image", "rollback"):
                data = preview_body(cluster, action).model_dump()
                assert (
                    await http.post(base + f"/{action}/preview", json=data)
                ).status_code == 403
                role = "namespace_operator"
                response = await http.post(
                    base + f"/{action}/preview", json=data
                )
                assert response.status_code == 200, response.text
                assert "SENSITIVE-VALUE" not in response.text
                data.update(
                    request_id="http-" + action,
                    preview_token=response.json()["data"]["previewToken"],
                )
                role = "readonly"
                assert (
                    await http.post(base + "/" + action, json=data)
                ).status_code == 403
            role = "platform_admin"
            # 预检不能跨操作者转用。
            assert (
                await http.post(base + "/rollback", json=data)
            ).status_code == 409
            response = await http.post(
                base + "/rollback/preview",
                json=preview_body(cluster, "rollback").model_dump(),
            )
            data["preview_token"] = response.json()["data"]["previewToken"]
            response = await http.post(base + "/rollback", json=data)
            assert response.status_code == 202, response.text
            assert response.json()["data"]["operation"]["action"] == "rollback"

    asyncio.run(run())


@pytest.mark.parametrize("action", ["image", "rollback"])
@pytest.mark.parametrize("field", ["paused", "deleting"])
def test_release_rejects_paused_or_deleting(store, action, field):
    cluster = ReleaseCluster()
    if field == "paused":
        cluster.value["spec"]["paused"] = True
    else:
        cluster.value["metadata"]["deletionTimestamp"] = "now"
    with pytest.raises(HTTPException) as error:
        release_body(cluster, action)
    assert error.value.status_code == 409 and not cluster.writes


def test_rollback_replaces_template_removing_current_only_fields(store):
    cluster = ReleaseCluster()
    template = cluster.value["spec"]["template"]
    template["metadata"]["annotations"]["current-only"] = "remove-me"
    template["spec"]["containers"][0]["env"].append(
        {"name": "NEW", "value": "remove-me"}
    )
    record = ops(cluster).submit(
        "rollback", release_body(cluster, "rollback")
    )["operation"]
    step(cluster)
    template = cluster.value["spec"]["template"]
    assert "current-only" not in template["metadata"]["annotations"]
    assert len(template["spec"]["containers"][0]["env"]) == 1
    assert record["release"]["sourceReplicaSetUid"] == "rs-old"


@pytest.mark.parametrize("action", ["image", "rollback"])
def test_sdk_replicaset_access_is_required(store, action):
    cluster = ReleaseCluster()

    def forbidden(**kwargs):
        raise ApiException(status=403, reason="SENSITIVE-VALUE")

    cluster.apps.list_namespaced_replica_set = forbidden
    with pytest.raises(ApiException):
        release_body(cluster, action)
    assert not cluster.writes


@pytest.mark.parametrize(
    "failure,status",
    [
        (ApiException(status=422, reason="SENSITIVE-VALUE"), "failed"),
        (OSError("SENSITIVE-VALUE"), "timed_out"),
    ],
)
def test_actual_write_failure_and_unknown_do_not_replay(
    store, failure, status
):
    cluster = ReleaseCluster()
    record = ops(cluster).submit("image", release_body(cluster))["operation"]
    cluster.failure = failure
    step(cluster)
    result = ops(cluster).get(record["id"])
    assert result["status"] == status
    assert "SENSITIVE-VALUE" not in json.dumps(result)
    cluster.failure = None
    if status == "timed_out":
        step(cluster)
    assert not cluster.writes


def test_release_audit_finalize_interruption_recovers_without_replay(
    store, monkeypatch
):
    from core.services import deployment_operations as service

    cluster = ReleaseCluster()
    record = ops(cluster).submit("image", release_body(cluster))["operation"]
    original = service.record_write

    def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(service, "record_write", fail)
    with pytest.raises(RuntimeError):
        step(cluster)
    with get_db() as db:
        row = db.get(DeploymentOperation, record["id"])
        assert row.phase == "writing"
        row.lease_until = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
    monkeypatch.setattr(service, "record_write", original)
    cluster.healthy()
    step(cluster)
    assert ops(cluster).get(record["id"])["status"] == "succeeded"
    assert len(cluster.writes) == 1


def test_concurrent_release_requests_share_target_lock(store):
    cluster = ReleaseCluster()
    body = release_body(cluster)

    def submit(index):
        request = body.model_copy(update={"request_id": f"release-{index}"})
        try:
            return ops(cluster).submit("image", request)["operation"]["status"]
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(submit, range(2)))
    assert result.count("pending") == result.count(409) == 1
    assert not cluster.writes


def test_image_pull_failure_has_safe_diagnostic_and_final_failure(store):
    cluster = ReleaseCluster()
    record = ops(cluster).submit("image", release_body(cluster))["operation"]
    step(cluster)
    cluster.healthy()
    cluster.value["status"]["conditions"] = [
        {
            "type": "Progressing",
            "status": "False",
            "reason": "ProgressDeadlineExceeded",
            "message": "SENSITIVE-VALUE",
        }
    ]
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
    step(cluster)
    result = ops(cluster).get(record["id"])
    assert result["status"] == "failed"
    assert result["progress"]["diagnostics"][0]["code"] == "ImagePullBackOff"
    assert "SENSITIVE-VALUE" not in json.dumps(result)
