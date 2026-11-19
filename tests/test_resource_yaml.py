"""资源编辑的并发保护、版本持久化、字段差异和 Secret 边界。"""

import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi import FastAPI, HTTPException
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from core.config import Application
from core.http_api_client.kubernetes_view_client import (
    RESOURCES,
    KubernetesViewClient,
)
from core.orm import engine as engine_module
from core.orm.engine import Base, get_db
from core.orm.resource_revision import ResourceRevision
from core.services.resource_yaml import (
    ResourceYaml,
    changes,
    editable,
    parse_manifest,
)
from web.api.resource_yaml import YamlApply, YamlChange


class Response:
    def __init__(self, value, status=200):
        self.data = json.dumps(value).encode()
        self.status = status
        self.closed = False

    def close(self):
        self.closed = True

    def release_conn(self):
        pass


class Cluster:
    def __init__(self, kind="ConfigMap"):
        self.value = {
            "apiVersion": "v1",
            "kind": kind,
            "metadata": {
                "name": "example",
                "namespace": "apps",
                "uid": "uid-1",
                "resourceVersion": "1",
                "managedFields": [],
                "generation": 1,
            },
            "data": {"setting": "original"},
        }
        self.writes = []
        self.dry_runs = 0
        self.refuse = None
        self.api_client = SimpleNamespace(
            configuration=SimpleNamespace(
                host="https://cluster.test", ssl_ca_cert=None
            )
        )
        stem = "secret" if kind == "Secret" else "config_map"
        self.core = SimpleNamespace(
            **{
                "read_namespaced_" + stem: self.read,
                "replace_namespaced_" + stem: self.replace,
            }
        )

    def read(self, **kwargs):
        return Response(self.value)

    def replace(self, body, **kwargs):
        assert kwargs["field_validation"] == "Strict"
        assert kwargs["field_manager"] == "kubengine-yaml"
        assert kwargs["_preload_content"] is False
        if kwargs.get("dry_run") == "All":
            self.dry_runs += 1
            return Response(body)
        if self.refuse:
            raise self.refuse
        if (
            body["metadata"]["resourceVersion"]
            != self.value["metadata"]["resourceVersion"]
        ):
            raise ApiException(status=409, reason="Conflict")
        self.value = copy.deepcopy(body)
        self.value["metadata"]["resourceVersion"] = str(
            int(body["metadata"]["resourceVersion"]) + 1
        )
        self.writes.append(copy.deepcopy(body))
        return Response(self.value)


@pytest.fixture
def store(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'history.db'}")
    sessions = scoped_session(sessionmaker(bind=engine))
    monkeypatch.setattr(engine_module, "engine", engine)
    monkeypatch.setattr(engine_module, "SessionLocal", sessions)
    monkeypatch.setattr(Application, "ROOT_DIR", str(tmp_path))
    Base.metadata.create_all(engine)
    yield tmp_path
    sessions.remove()
    engine.dispose()


def change(cluster, value="modified"):
    raw = editable(cluster.value)
    raw["data"]["setting"] = value
    return YamlChange(
        yaml_text=yaml.safe_dump(raw),
        uid=cluster.value["metadata"]["uid"],
        resource_version=cluster.value["metadata"]["resourceVersion"],
        comment="修改测试",
        reveal_secret=cluster.value["kind"] == "Secret",
    )


def request(ops, data, request_id="request-1"):
    result = ops.preview(data)
    return YamlApply(
        **data.model_dump(),
        request_id=request_id,
        preview_hash=result["previewHash"],
    )


@pytest.mark.parametrize(
    "source",
    [
        "apiVersion: v1\napiVersion: v2",
        "a: &a [*a]",
        "[1, 2]",
        "a: 1\n---\nb: 2",
        "a: .nan",
        "a: !!python/object:object {}",
        "1: value",
        "a: [" * 65 + "0" + "]" * 65,
    ],
)
def test_rejects_ambiguous_unsafe_or_unbounded_yaml(source):
    with pytest.raises(HTTPException) as error:
        parse_manifest(source)
    assert error.value.status_code == 400


def test_yaml_size_and_dates():
    assert parse_manifest("date: 2026-11-19")["date"] == "2026-11-19"
    with pytest.raises(HTTPException) as error:
        parse_manifest("x: " + "中" * 1024**2)
    assert error.value.status_code == 413


def test_diff_distinguishes_types_and_escapes_paths():
    result = changes(
        {"a/b~c": [True, "old"], "removed": 0},
        {"a/b~c": [1, "new"], "added": None},
    )
    assert {r["path"] for r in result} == {
        "/a~1b~0c/0",
        "/a~1b~0c/1",
        "/removed",
        "/added",
    }
    assert next(r for r in result if r["path"] == "/added")["action"] == "add"


def test_editor_preview_apply_history_and_idempotent_retry(store):
    cluster = Cluster()
    cluster.value["futureField"] = {"preserved": "new-server-field"}
    ops = ResourceYaml(cluster, "cm", "apps", "example")
    editor = ops.editor()
    assert (
        "managedFields" not in editor["yaml"]
        and editor["resourceVersion"] == "1"
    )
    data = change(cluster)
    command = request(ops, data)
    assert cluster.writes == []
    applied = ops.apply(command, "operator")
    assert applied["revision"]["status"] == "applied"
    assert applied["resourceVersion"] == "2"
    assert (
        len(cluster.writes) == 1
        and cluster.value["futureField"]["preserved"] == "new-server-field"
    )
    replay = ops.apply(command, "operator")
    assert replay["replayed"] and len(cluster.writes) == 1
    history = ops.versions()
    assert history["total"] == 1 and history["items"][0]["actor"] == "operator"
    detail = ops.version(applied["revision"]["id"])
    assert detail["changes"] == [
        {
            "path": "/data/setting",
            "action": "replace",
            "before": "original",
            "after": "modified",
            "redacted": False,
        }
    ]
    assert (
        "original" in detail["beforeYaml"]
        and "modified" in detail["afterYaml"]
    )
    with get_db() as db:
        row = db.get(ResourceRevision, applied["revision"]["id"])
        assert (
            "original" not in row.snapshots and "modified" not in row.snapshots
        )
    assert (
        store / "config/resource-history.key"
    ).stat().st_mode & 0o777 == 0o600
    next_command = request(ops, change(cluster, "third"), "request-2")
    assert ops.apply(next_command, "operator")["revision"]["version"] == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("apiVersion", "apps/v1"),
        ("kind", "Secret"),
        ("metadata.name", "other"),
        ("metadata.namespace", "other"),
        ("status", {}),
        ("metadata.uid", "uid-1"),
    ],
)
def test_resource_identity_and_server_fields_cannot_be_edited(
    store, field, value
):
    cluster = Cluster()
    ops = ResourceYaml(cluster, "configmap", "apps", "example")
    data = change(cluster)
    raw = yaml.safe_load(data.yaml_text)
    if "." in field:
        parent, key = field.split(".")
        raw[parent][key] = value
    else:
        raw[field] = value
    data.yaml_text = yaml.safe_dump(raw)
    with pytest.raises(HTTPException) as error:
        ops.preview(data)
    assert error.value.status_code == 400 and not cluster.writes


def test_conflicts_and_preview_hash_prevent_overwriting(store):
    cluster = Cluster()
    ops = ResourceYaml(cluster, "configmap", "apps", "example")
    command = request(ops, change(cluster))
    cluster.value["metadata"]["resourceVersion"] = "2"
    with pytest.raises(HTTPException) as error:
        ops.apply(command, "operator")
    assert error.value.status_code == 409 and not cluster.writes
    assert ops.versions()["total"] == 0
    command = request(ops, change(cluster))
    command.preview_hash = "0" * 64
    with pytest.raises(HTTPException) as error:
        ops.apply(command, "operator")
    assert error.value.status_code == 409 and not cluster.writes


def test_noop_does_not_create_history_or_write(store):
    cluster = Cluster()
    ops = ResourceYaml(cluster, "configmap", "apps", "example")
    data = change(cluster, "original")
    assert ops.preview(data)["noChanges"]
    assert ops.apply(request(ops, data), "operator")["noChanges"]
    assert ops.versions()["total"] == 0 and not cluster.writes


@pytest.mark.parametrize(
    "failure,status",
    [
        (ApiException(status=409, reason="Conflict"), "failed"),
        (OSError("disconnected"), "unknown"),
    ],
)
def test_failure_retains_history_and_is_never_automatically_replayed(
    store, failure, status
):
    cluster = Cluster()
    ops = ResourceYaml(cluster, "configmap", "apps", "example")
    command = request(ops, change(cluster))
    cluster.refuse = failure
    with pytest.raises(type(failure)):
        ops.apply(command, "operator")
    row = ops.versions()["items"][0]
    assert row["status"] == status
    assert ops.apply(command, "operator")["replayed"]
    assert not cluster.writes


def test_secret_history_is_encrypted_and_redaction_keeps_changed_fields(store):
    cluster = Cluster("Secret")
    cluster.value["data"] = {"setting": "b2xkLXNlY3JldA=="}
    ops = ResourceYaml(cluster, "tls", "apps", "example")
    assert not ops.editor()["editable"] and "b2xk" not in ops.editor()["yaml"]
    data = change(cluster, "bmV3LXNlY3JldA==")
    data.reveal_secret = False
    with pytest.raises(HTTPException):
        ops.preview(data)
    data.reveal_secret = True
    result = ops.apply(request(ops, data), "operator")
    hidden = ops.version(result["revision"]["id"])
    assert hidden["changes"][0]["redacted"]
    assert (
        hidden["changes"][0]["before"]
        == hidden["changes"][0]["after"]
        == "<已隐藏>"
    )
    assert "b2xkLXNlY3JldA==" not in json.dumps(hidden)
    assert "bmV3LXNlY3JldA==" not in json.dumps(hidden)
    assert (
        "bmV3LXNlY3JldA=="
        in ops.version(result["revision"]["id"], True)["afterYaml"]
    )


def test_new_uid_has_separate_versions_and_cannot_read_old_history(store):
    cluster = Cluster()
    ops = ResourceYaml(cluster, "configmap", "apps", "example")
    record_id = ops.apply(request(ops, change(cluster)), "operator")[
        "revision"
    ]["id"]
    cluster.value["metadata"]["uid"] = "uid-recreated"
    assert ops.versions()["total"] == 0
    with pytest.raises(HTTPException) as error:
        ops.version(record_id)
    assert error.value.status_code == 404


def test_missing_history_key_blocks_new_cluster_writes(store):
    cluster = Cluster()
    ops = ResourceYaml(cluster, "configmap", "apps", "example")
    ops.apply(request(ops, change(cluster)), "operator")
    (store / "config/resource-history.key").unlink()
    command = request(ops, change(cluster, "next"), "next-request")
    with pytest.raises(HTTPException) as error:
        ops.apply(command, "operator")
    assert error.value.status_code == 503 and len(cluster.writes) == 1


def test_pending_record_exists_before_write_and_survives_interruption(
    store, monkeypatch
):
    cluster = Cluster()
    ops = ResourceYaml(cluster, "configmap", "apps", "example")
    command = request(ops, change(cluster))
    original = cluster.replace

    def interrupt(body, **kwargs):
        if kwargs.get("dry_run") == "All":
            return original(body, **kwargs)
        with get_db() as db:
            row = db.query(ResourceRevision).one()
            assert row.status == "pending" and row.actor == "operator"
        raise KeyboardInterrupt("simulate worker crash")

    monkeypatch.setattr(
        cluster.core, "replace_namespaced_config_map", interrupt
    )
    with pytest.raises(KeyboardInterrupt):
        ops.apply(command, "operator")
    replay = ops.apply(command, "operator")
    assert replay["replayed"] and replay["revision"]["status"] == "pending"
    assert not cluster.writes


def test_history_failure_after_cluster_write_retains_pending_and_no_replay(
    store, monkeypatch
):
    from core.services import resource_yaml

    cipher = resource_yaml.history_cipher()
    calls = []

    def encrypt(value):
        calls.append(True)
        if len(calls) == 2:
            raise OSError("simulate history disk failure after PUT")
        return cipher.encrypt(value)

    monkeypatch.setattr(
        resource_yaml,
        "history_cipher",
        lambda: SimpleNamespace(encrypt=encrypt),
    )
    cluster = Cluster()
    ops = ResourceYaml(cluster, "configmap", "apps", "example")
    command = request(ops, change(cluster))
    result = ops.apply(command, "operator")
    assert result["revision"]["status"] == "pending" and result["warning"]
    assert len(cluster.writes) == 1
    assert (
        ops.apply(command, "operator")["replayed"] and len(cluster.writes) == 1
    )


def test_request_identity_cannot_be_reused_with_changed_payload_or_user(store):
    cluster = Cluster()
    ops = ResourceYaml(cluster, "configmap", "apps", "example")
    command = request(ops, change(cluster))
    ops.apply(command, "operator")
    with pytest.raises(HTTPException) as error:
        ops.apply(command, "other-user")
    assert error.value.status_code == 409
    command.comment = "different payload"
    with pytest.raises(HTTPException) as error:
        ops.apply(command, "operator")
    assert error.value.status_code == 409 and len(cluster.writes) == 1


def test_concurrent_updates_have_unique_versions_and_only_one_wins(store):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    cluster = Cluster()
    ops = ResourceYaml(cluster, "configmap", "apps", "example")
    first = request(ops, change(cluster), "first")
    second = first.model_copy(update={"request_id": "second"})
    original = cluster.replace
    barrier, lock = threading.Barrier(2), threading.Lock()

    def replace(body, **kwargs):
        if kwargs.get("dry_run") == "All":
            return original(body, **kwargs)
        barrier.wait(timeout=10)
        with lock:
            return original(body, **kwargs)

    cluster.core.replace_namespaced_config_map = replace

    def apply(command):
        try:
            return ops.apply(command, "operator")["revision"]["status"]
        except ApiException as exc:
            assert exc.status == 409
            return "failed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, [first, second]))
    assert sorted(results) == ["applied", "failed"]
    assert {row["version"] for row in ops.versions()["items"]} == {1, 2}
    assert len(cluster.writes) == 1


def test_secret_api_rejection_does_not_echo_value_even_through_alias():
    from web.api.resource_yaml import execute

    def fail():
        error = ApiException(status=422, reason="invalid TOP-SECRET")
        error.body = json.dumps(
            {
                "message": "invalid TOP-SECRET",
                "details": {
                    "causes": [
                        {"field": "data.password", "message": "TOP-SECRET"}
                    ]
                },
            }
        )
        raise error

    result = execute("tls", fail)
    assert result[0] == 422 and "TOP-SECRET" not in str(result)


def test_sdk_raw_requests_preserve_fields_for_all_known_resource_types(
    monkeypatch,
):
    api = client.ApiClient(client.Configuration(host="https://cluster.test"))
    view = KubernetesViewClient(api_client=api)
    requests = []

    def transport(*args, **kwargs):
        requests.append((args, kwargs))
        return Response(
            {
                "metadata": {"uid": "1", "resourceVersion": "1"},
                "future": {"keep": True},
            }
        )

    monkeypatch.setattr(api.rest_client, "request", transport)
    try:
        for kind in RESOURCES:
            ops = ResourceYaml(view, kind, "apps", "example")
            assert ops.call()["future"]["keep"]
            body = {"future": {"keep": True}}
            assert ops.call(body, dry_run=True)["future"]["keep"]
            assert requests[-1][1]["body"]["future"]["keep"]
    finally:
        api.close()


def test_routes_auth_preview_apply_and_version_access(store, monkeypatch):
    from web.api import resource_yaml as routes
    from web.utils import auth

    cluster = Cluster()
    ops = ResourceYaml(cluster, "configmap", "apps", "example")
    monkeypatch.setattr(routes, "operations", lambda *args: ops)

    async def identity(authorization=None, **kwargs):
        if authorization != "Bearer test":
            raise HTTPException(401, "login required")
        return SimpleNamespace(username="operator"), "test"

    monkeypatch.setattr(auth, "get_current_user", identity)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1/k8s")

    async def execute():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            url = "/api/v1/k8s/manifest/configmap/apps/example"
            assert (await http.get(url + "/edit")).status_code == 401
            assert (
                await http.post(url + "/apply", json={})
            ).status_code == 401
            assert (await http.get(url + "/versions")).status_code == 401
            http.headers["Authorization"] = "Bearer test"
            assert (await http.get(url + "/edit")).json()["data"]["editable"]
            command = request(ops, change(cluster))
            response = await http.post(
                url + "/preview", json=command.model_dump()
            )
            assert (
                response.json()["data"]["changes"][0]["path"]
                == "/data/setting"
            )
            response = await http.post(
                url + "/apply", json=command.model_dump()
            )
            assert response.json()["data"]["revision"]["actor"] == "operator"
            assert (await http.get(url + "/versions")).json()["data"][
                "total"
            ] == 1

    asyncio.run(execute())
