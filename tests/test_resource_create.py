"""YAML 创建的预检、作用域、逐项结果、幂等及初始版本记录。"""

import copy
import json
from types import SimpleNamespace

import pytest
import yaml
from fastapi import HTTPException
from kubernetes.client.exceptions import ApiException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import scoped_session, sessionmaker

from core.config import Application
from core.orm import engine as engine_module
from core.orm.engine import Base, get_db
from core.orm.resource_revision import ResourceRevision
from core.services.resource_create import (
    ResourceCreator,
    SdkJson,
    parse_resources,
)
from core.services.resource_yaml import ResourceYaml
from web.api.resource_yaml import YamlCreate, YamlCreatePreview


def rejected(code, field=None):
    exc = ApiException(status=code, reason="private submitted value")
    exc.body = json.dumps(
        {
            "message": "private submitted value",
            "details": {
                "causes": (
                    [{"field": field, "message": "private submitted value"}]
                    if field
                    else []
                )
            },
        }
    )
    return exc


class Response:
    def __init__(self, body, status=200):
        self.data = json.dumps(body).encode()
        self.status = status
        self.closed = False

    def close(self):
        self.closed = True

    def release_conn(self):
        pass


class Cluster:
    def __init__(self):
        self.api_client = SimpleNamespace(
            configuration=SimpleNamespace(
                host="https://test-cluster", ssl_ca_cert=None
            )
        )
        self.namespaces = {"default", "apps"}
        self.objects = {}
        self.writes = []
        self.reject = {}
        self.unknown = set()
        self.calls = []
        self.core = SimpleNamespace(
            read_namespaced_config_map=self.read,
            read_namespaced_secret=self.read,
        )

    def read(self, name, namespace, **kwargs):
        return Response(
            next(
                v
                for v in self.objects.values()
                if v["metadata"]["name"] == name
                and v["metadata"].get("namespace") == namespace
            )
        )

    def call(self, method, path, body=None, dry_run=False):
        self.calls.append((method, path, copy.deepcopy(body), dry_run))
        if method == "GET":
            resources = {
                "/api/v1": [
                    ("ConfigMap", "configmaps", True),
                    ("Secret", "secrets", True),
                    ("Namespace", "namespaces", False),
                    ("Service", "services", True),
                ],
                "/apis/apps/v1": [("Deployment", "deployments", True)],
                "/apis/example.test/v1": [("Widget", "widgets", True)],
                "/apis/rbac.authorization.k8s.io/v1": [
                    ("ClusterRole", "clusterroles", False)
                ],
            }
            if path not in resources:
                raise rejected(404)
            return {
                "resources": [
                    {
                        "kind": k,
                        "name": p,
                        "namespaced": n,
                        "verbs": ["get", "create"],
                    }
                    for k, p, n in resources[path]
                ]
            }
        identity = path + "/" + body["metadata"]["name"]
        if (
            "/namespaces/" in path
            and body["metadata"]["namespace"] not in self.namespaces
        ):
            raise rejected(404)
        if identity in self.objects:
            raise rejected(409)
        if identity in self.reject and not dry_run:
            raise self.reject[identity]
        value = copy.deepcopy(body)
        value["metadata"].update(
            uid="uid-" + str(len(self.objects) + 1), resourceVersion="1"
        )
        if dry_run:
            return value
        self.objects[identity] = value
        self.writes.append(identity)
        if body["kind"] == "Namespace":
            self.namespaces.add(body["metadata"]["name"])
        if identity in self.unknown:
            raise OSError("private network information")
        return value


@pytest.fixture
def store(monkeypatch, tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path / "create.db"}')
    sessions = scoped_session(sessionmaker(bind=engine))
    monkeypatch.setattr(engine_module, "engine", engine)
    monkeypatch.setattr(engine_module, "SessionLocal", sessions)
    monkeypatch.setattr(Application, "ROOT_DIR", str(tmp_path))
    Base.metadata.create_all(engine)
    yield tmp_path
    sessions.remove()
    engine.dispose()


def resource(name="example", kind="ConfigMap", version="v1", namespace=None):
    meta = {"name": name}
    if namespace is not None:
        meta["namespace"] = namespace
    value = {"apiVersion": version, "kind": kind, "metadata": meta}
    if kind == "ConfigMap":
        value["data"] = {"greeting": "hello"}
    return value


def payload(*objects, namespace="apps", comment="创建测试"):
    return YamlCreatePreview(
        yaml_text=yaml.safe_dump_all(objects),
        namespace=namespace,
        comment=comment,
    )


def creator(cluster, actor="operator"):
    ops = ResourceCreator(cluster, actor)
    ops.sdk = cluster
    return ops


def command(ops, body, key="test-request"):
    preview = ops.preview(body)
    assert preview["valid"]
    return YamlCreate(
        **body.model_dump(),
        preview_token=preview["previewToken"],
        request_id=key,
    )


@pytest.mark.parametrize(
    "source",
    [
        "",
        "null",
        "[1,2]",
        "apiVersion: v1\napiVersion: v2",
        "a: &a [*a]",
        "kind: ConfigMap\nmetadata: {name: example}",
        "apiVersion: ../../other\nkind: ConfigMap\nmetadata: {name: example}",
        "apiVersion: v1\nkind: ConfigMap\nmetadata: {generateName: example-}",
        "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: ../bad}",
        (
            "apiVersion: v1\nkind: ConfigMap\n"
            "metadata: {name: example, uid: original}"
        ),
        (
            "apiVersion: v1\nkind: ConfigMap\n"
            "metadata: {name: example}\nstatus: {}"
        ),
        (
            "apiVersion: v1\nkind: ConfigMap\n"
            "metadata: {name: example}\ndata: {secret: .nan}"
        ),
    ],
)
def test_invalid_yaml_and_exported_server_fields_are_rejected(source):
    with pytest.raises(HTTPException) as error:
        parse_resources(source)
    assert error.value.status_code == 400


def test_multiple_documents_and_list_are_bounded():
    assert (
        len(
            parse_resources(
                yaml.safe_dump_all(
                    [
                        resource("one"),
                        {
                            "kind": "List",
                            "apiVersion": "v1",
                            "items": [resource("two")],
                        },
                    ]
                )
            )
        )
        == 2
    )
    with pytest.raises(HTTPException) as error:
        parse_resources(
            yaml.safe_dump_all([resource(str(i)) for i in range(21)])
        )
    assert error.value.status_code == 413
    with pytest.raises(HTTPException) as error:
        parse_resources("x: " + "中" * 1024**2)
    assert error.value.status_code == 413


def test_preview_is_read_only_and_create_saves_encrypted_initial_version(
    store,
):
    cluster = Cluster()
    ops = creator(cluster)
    body = payload(resource())
    request = command(ops, body)
    assert cluster.writes == []
    result = ops.create(request)
    assert result["complete"] and result["created"] == 1
    assert result["items"][0]["version"] == 1
    history = ResourceYaml(cluster, "configmap", "apps", "example")
    assert history.versions()["total"] == 1
    version = history.version(result["items"][0]["revisionId"])
    assert (
        version["actor"] == "operator"
        and version["beforeYaml"].strip() == "{}"
    )
    assert "greeting: hello" in version["afterYaml"]
    with get_db() as db:
        row = db.scalar(select(ResourceRevision))
        assert "hello" not in row.snapshots


def test_retry_from_another_worker_never_creates_again(store):
    cluster = Cluster()
    ops = creator(cluster)
    request = command(ops, payload(resource()))
    first = ops.create(request)
    replay = creator(cluster).create(request)
    assert first["items"][0]["revisionId"] == replay["items"][0]["revisionId"]
    assert replay["items"][0]["replayed"] and len(cluster.writes) == 1


def test_binding_prevents_changed_yaml_namespace_actor_or_expired_preview(
    store, monkeypatch
):
    cluster = Cluster()
    ops = creator(cluster)
    request = command(ops, payload(resource()))
    for values in [
        {"yaml_text": payload(resource("other")).yaml_text},
        {"namespace": "default"},
        {"comment": "changed"},
    ]:
        with pytest.raises(HTTPException) as error:
            ops.create(request.model_copy(update=values))
        assert error.value.status_code == 409
    with pytest.raises(HTTPException):
        creator(cluster, "other-user").create(request)
    monkeypatch.setattr(
        "core.services.resource_create.time.time", lambda: 9999999999
    )
    with pytest.raises(HTTPException):
        ops.create(request)
    assert cluster.writes == []


def test_request_key_cannot_be_reused_for_different_content(store):
    cluster = Cluster()
    ops = creator(cluster)
    ops.create(command(ops, payload(resource())))
    another = command(ops, payload(resource("another")))
    with pytest.raises(HTTPException) as error:
        ops.create(another)
    assert error.value.status_code == 409 and len(cluster.writes) == 1


def test_scope_discovery_core_grouped_cluster_and_custom_resources(store):
    cluster = Cluster()
    ops = creator(cluster)
    body = payload(
        resource("deployment", "Deployment", "apps/v1"),
        resource("widget", "Widget", "example.test/v1"),
        resource("role", "ClusterRole", "rbac.authorization.k8s.io/v1"),
    )
    result = ops.create(command(ops, body))
    assert result["complete"]
    assert (
        "/apis/apps/v1/namespaces/apps/deployments/deployment"
        in cluster.writes
    )
    assert (
        "/apis/example.test/v1/namespaces/apps/widgets/widget"
        in cluster.writes
    )
    assert (
        "/apis/rbac.authorization.k8s.io/v1/clusterroles/role"
        in cluster.writes
    )
    assert result["items"][2]["namespace"] == ""


def test_explicit_namespace_wins_and_cluster_namespace_is_rejected(store):
    cluster = Cluster()
    ops = creator(cluster)
    result = ops.create(command(ops, payload(resource(namespace="default"))))
    assert result["items"][0]["namespace"] == "default"
    with pytest.raises(HTTPException):
        ops.preview(
            payload(
                resource(
                    "role",
                    "ClusterRole",
                    "rbac.authorization.k8s.io/v1",
                    "apps",
                )
            )
        )
    with pytest.raises(HTTPException):
        ops.preview(payload(resource(), namespace="../bad"))


def test_duplicate_identity_and_unsupported_kind_are_rejected(store):
    ops = creator(Cluster())
    for body in [
        payload(resource(), resource()),
        payload(resource(kind="NotAKind")),
    ]:
        with pytest.raises(HTTPException) as error:
            ops.preview(body)
        assert error.value.status_code == 400


def test_existing_resource_is_not_overwritten(store):
    cluster = Cluster()
    ops = creator(cluster)
    body = payload(resource())
    ops.create(command(ops, body))
    preview = ops.preview(body)
    assert not preview["valid"] and preview["items"][0]["code"] == 409
    assert preview["previewToken"] == "" and len(cluster.writes) == 1


def test_new_namespace_is_created_first_even_when_last_in_file(store):
    cluster = Cluster()
    ops = creator(cluster)
    body = payload(
        resource(namespace="new-space"), resource("new-space", "Namespace")
    )
    preview = ops.preview(body)
    assert preview["valid"] and preview["items"][0]["status"] == "deferred"
    assert cluster.writes == []
    result = ops.create(
        YamlCreate(
            **body.model_dump(),
            preview_token=preview["previewToken"],
            request_id="new-namespace",
        )
    )
    assert result["complete"] and result["items"][0]["index"] == 1
    assert cluster.writes[0] == "/api/v1/namespaces/new-space"
    assert (
        cluster.writes[1] == "/api/v1/namespaces/new-space/configmaps/example"
    )


def test_partial_failure_keeps_successes_without_private_values(
    store,
):
    cluster = Cluster()
    ops = creator(cluster)
    request = command(ops, payload(resource("one"), resource("two")))
    cluster.reject["/api/v1/namespaces/apps/configmaps/two"] = rejected(
        422, "data.setting"
    )
    result = ops.create(request)
    assert not result["complete"] and result["created"] == 1
    assert result["items"][1]["status"] == "failed"
    assert "data.setting" in result["items"][1]["message"]
    assert "private" not in json.dumps(result)
    replay = creator(cluster).create(request)
    assert replay["created"] == 1 and len(cluster.writes) == 1


def test_network_loss_after_write_is_unknown_and_never_replayed(store):
    cluster = Cluster()
    ops = creator(cluster)
    request = command(ops, payload(resource()))
    cluster.unknown.add("/api/v1/namespaces/apps/configmaps/example")
    result = ops.create(request)
    assert result["items"][0]["status"] == "unknown"
    assert "private" not in json.dumps(result)
    replay = creator(cluster).create(request)
    assert (
        replay["items"][0]["status"] == "unknown" and len(cluster.writes) == 1
    )


def test_secret_preview_history_and_errors_are_redacted(store):
    cluster = Cluster()
    ops = creator(cluster)
    value = resource(kind="Secret")
    value["stringData"] = {"password": "private-secret-value"}
    value["metadata"]["annotations"] = {
        "private-annotation": "private-secret-value"
    }
    body = payload(value)
    preview = ops.preview(body)
    assert "private-secret-value" not in json.dumps(preview)
    result = ops.create(
        YamlCreate(
            **body.model_dump(),
            preview_token=preview["previewToken"],
            request_id="secret-create",
        )
    )
    history = ResourceYaml(cluster, "secret", "apps", "example")
    hidden = history.version(result["items"][0]["revisionId"])
    assert "private-secret-value" not in json.dumps(hidden)
    assert "private-secret-value" in json.dumps(
        history.version(result["items"][0]["revisionId"], True)
    )


@pytest.mark.parametrize("modern", [False, True])
def test_sdk_transport_preserves_fields_auth_and_strict_validation(
    modern,
):
    calls = []
    response = Response({"apiVersion": "v1"})

    class Client:
        def call_api(self, *args, **kwargs):
            calls.append((args, kwargs))
            return response

    api = Client()
    if modern:

        def serialize(**kwargs):
            calls.append(((), kwargs))
            return ("POST", "https://cluster.test/api/v1")

        api.param_serialize = serialize
    sdk = SdkJson(api, 30)
    sdk.call(
        "POST",
        "/api/v1/namespaces/apps/configmaps",
        {"futureField": {"must": "survive"}},
        dry_run=True,
    )
    params = calls[0][1]
    assert params["body"]["futureField"]["must"] == "survive"
    assert params["auth_settings"] == ["BearerToken"]
    assert dict(params["query_params"])["fieldValidation"] == "Strict"
    assert dict(params["query_params"])["dryRun"] == "All"
    assert response.closed


def test_real_sdk_serialization_uses_cluster_auth_without_model_field_loss(
    monkeypatch,
):
    from kubernetes import client

    config = client.Configuration(host="https://cluster.test")
    config.api_key["BearerToken"] = "Bearer test-only"
    api = client.ApiClient(configuration=config)
    seen = []
    response = Response({"metadata": {"uid": "created"}})

    def transport(method, url, **kwargs):
        seen.append((method, url, kwargs))
        return response

    monkeypatch.setattr(api.rest_client, "request", transport)
    try:
        result = SdkJson(api, 30).call(
            "POST",
            "/api/v1/namespaces/apps/configmaps",
            {"futureField": "preserved"},
            dry_run=True,
        )
        assert result["metadata"]["uid"] == "created"
        method, url, kwargs = seen[0]
        assert (
            method == "POST"
            and "dryRun=All" in url
            and "fieldValidation=Strict" in url
        )
        assert {k.lower(): v for k, v in kwargs["headers"].items()}[
            "authorization"
        ] == "Bearer test-only"
        assert kwargs["body"] == {"futureField": "preserved"}
    finally:
        api.close()


def test_pending_request_from_another_worker_is_not_submitted_twice(store):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    cluster = Cluster()
    ops = creator(cluster)
    request = command(ops, payload(resource()))
    started, release = threading.Event(), threading.Event()
    original = cluster.call

    def held(method, path, body=None, dry_run=False):
        if method == "POST" and not dry_run:
            started.set()
            assert release.wait(5)
        return original(method, path, body, dry_run)

    cluster.call = held
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(ops.create, request)
        assert started.wait(5)
        try:
            replay = creator(cluster).create(request)
            assert replay["items"][0]["status"] == "unknown"
            assert replay["items"][0]["replayed"]
        finally:
            release.set()
        result = future.result(timeout=5)
    assert result["complete"] and len(cluster.writes) == 1


@pytest.mark.asyncio
async def test_api_auth_and_create_use_standard_response(store, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from fastapi.exceptions import RequestValidationError

    from web import main
    from web.api import resource_yaml as api
    from web.utils import auth

    cluster = Cluster()
    original = api.ResourceCreator

    def factory(view, actor, **kwargs):
        ops = original(view, actor, **kwargs)
        ops.sdk = cluster
        return ops

    async def identity(**kwargs):
        if kwargs.get("authorization") != "Bearer test":
            raise HTTPException(401, "请先登录")
        return (
            SimpleNamespace(username="api-user", role="platform_admin"),
            "preflight",
        )

    monkeypatch.setattr(api, "ResourceCreator", factory)
    monkeypatch.setattr(api, "get_kubernetes_client", lambda: cluster)
    monkeypatch.setattr(auth, "get_current_user", identity)
    app = FastAPI()
    app.middleware("http")(main.request_log_context)
    app.add_exception_handler(HTTPException, main.business_exception_handler)
    app.add_exception_handler(
        RequestValidationError, main.validation_exception_handler
    )
    app.include_router(api.router, prefix="/api/v1/k8s")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        denied = await http.post("/api/v1/k8s/resources/preview", json={})
        assert denied.status_code == 401
        assert denied.json()["error_code"] == "AUTH_REQUIRED"
        body = payload(resource()).model_dump()
        headers = {
            "Authorization": "Bearer test",
            "X-Request-ID": "create-api-test",
        }
        preview = await http.post(
            "/api/v1/k8s/resources/preview", json=body, headers=headers
        )
        assert preview.status_code == 200 and preview.json()["data"]["valid"]
        assert preview.json()["request_id"] == "create-api-test"
        result = await http.post(
            "/api/v1/k8s/resources/create",
            json={
                **body,
                "preview_token": preview.json()["data"]["previewToken"],
                "request_id": "api-create",
            },
            headers=headers,
        )
        assert (
            result.status_code == 200 and result.json()["data"]["created"] == 1
        )
        invalid = await http.post(
            "/api/v1/k8s/resources/create",
            json={**body, "request_id": "without-preview"},
            headers=headers,
        )
        assert invalid.status_code == 422 and cluster.writes == [
            "/api/v1/namespaces/apps/configmaps/example"
        ]


def test_reused_request_rejects_appended_documents_even_after_external_delete(
    store,
):
    cluster = Cluster()
    ops = creator(cluster)
    ops.create(command(ops, payload(resource())))
    cluster.objects.clear()  # 模拟原资源已被外部删除，预检可再次通过。
    appended = command(ops, payload(resource(), resource("additional")))
    with pytest.raises(HTTPException) as error:
        ops.create(appended)
    assert error.value.status_code == 409
    assert len(cluster.writes) == 1
