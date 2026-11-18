"""工作台 SDK 契约、数量换算、关联关系和监控降级回归。"""

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from kubernetes import client, config
from kubernetes.client.rest import ApiException

from core.http_api_client.kubernetes_view_client import (
    RESOURCES,
    KubernetesViewClient,
    MetricHistory,
    pod_resources,
    quantity,
    selected,
)


def resource(
    kind, name="example", namespace="apps", spec=None, status=None, **meta
):
    return {
        "apiVersion": "v1",
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": namespace,
            "uid": f"uid-{name}",
            "creationTimestamp": "2026-01-01T00:00:00Z",
            **meta,
        },
        "spec": spec or {},
        "status": status or {},
    }


def pod(
    name="example", namespace="apps", phase="Running", node="n1", **kwargs
):
    spec = {
        "nodeName": node,
        "containers": [
            {
                "name": "main",
                "image": "example:v1",
                "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}},
            }
        ],
        **kwargs.pop("spec", {}),
    }
    return resource("Pod", name, namespace, spec, {"phase": phase}, **kwargs)


def node(name="n1", ready="True"):
    return resource(
        "Node",
        name,
        "",
        status={
            "capacity": {"cpu": "2", "memory": "4Gi", "pods": "10"},
            "conditions": [{"type": "Ready", "status": ready}],
        },
    )


@pytest.fixture
def view():
    api = client.ApiClient(
        client.Configuration(host="https://cluster.invalid")
    )
    view = KubernetesViewClient(api_client=api)
    for attr, cls in [
        ("core", client.CoreV1Api),
        ("apps", client.AppsV1Api),
        ("networking", client.NetworkingV1Api),
        ("storage", client.StorageV1Api),
        ("custom", client.CustomObjectsApi),
        ("discovery", client.DiscoveryV1Api),
        ("batch", client.BatchV1Api),
        ("autoscaling", client.AutoscalingV2Api),
    ]:
        setattr(view, attr, MagicMock(spec=cls))
    for api_name, stem, _, namespaced in RESOURCES.values():
        api = getattr(view, api_name)
        names = (
            [f"list_namespaced_{stem}", f"list_{stem}_for_all_namespaces"]
            if namespaced
            else [f"list_{stem}"]
        )
        for name in names:
            getattr(api, name).return_value = {"items": []}
        getattr(
            api, f"read_{'namespaced_' if namespaced else ''}{stem}"
        ).side_effect = ApiException(status=404, reason="Not Found")
    view.custom.list_cluster_custom_object.return_value = {"items": []}
    view.custom.list_namespaced_custom_object.return_value = {"items": []}
    view.core.list_namespaced_event.return_value = {"items": []}
    view.core.list_event_for_all_namespaces.return_value = {"items": []}
    view.discovery.list_namespaced_endpoint_slice.return_value = {"items": []}
    yield view
    view.api_client.close()


def readable(api, method, value):
    getattr(api, method).side_effect = None
    getattr(api, method).return_value = value


@pytest.mark.parametrize(
    "value,cpu,expected",
    [
        ("1", True, 1000),
        ("250m", True, 250),
        ("120000n", True, 0.12),
        ("2u", True, 0.002),
        ("1e-3", True, 1),
        ("1Gi", False, 1024**3),
        ("1G", False, 10**9),
        ("64Mi", False, 64 * 1024**2),
        (None, False, 0),
    ],
)
def test_quantity_units(value, cpu, expected):
    assert quantity(value, cpu=cpu) == pytest.approx(expected)


@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity", "invalid"])
def test_invalid_quantities_are_rejected(value):
    with pytest.raises(ValueError):
        quantity(value)


def test_init_sidecar_and_overhead_are_included():
    raw = pod(
        spec={
            "containers": [
                {"resources": {"requests": {"cpu": "500m", "memory": "128Mi"}}}
            ],
            "initContainers": [
                {
                    "restartPolicy": "Always",
                    "resources": {"requests": {"cpu": "200m"}},
                },
                {"resources": {"requests": {"cpu": "1"}}},
                {
                    "restartPolicy": "Always",
                    "resources": {"requests": {"cpu": "100m"}},
                },
            ],
            "overhead": {"cpu": "50m", "memory": "16Mi"},
        }
    )
    totals = pod_resources(raw)
    assert totals["cpuRequests"] == 1250
    assert totals["memoryRequests"] == 144 * 1024**2
    assert totals["cpuLimits"] == 0


def test_pod_level_resources_override_container_sum():
    raw = pod(
        spec={
            "resources": {"requests": {"cpu": "2", "memory": "1Gi"}},
            "overhead": {"cpu": "10m"},
        }
    )
    assert pod_resources(raw)["cpuRequests"] == 2010
    assert pod_resources(raw)["memoryRequests"] == 1024**3


def test_node_allocation_excludes_finished_and_unassigned_pods(view):
    view.core.list_node.return_value = {"items": [node(), node("n2", "False")]}
    view.core.list_pod_for_all_namespaces.return_value = {
        "items": [
            pod(),
            pod("pending", phase="Pending"),
            pod("done", phase="Succeeded"),
            pod("failed", phase="Failed"),
            pod("unassigned", node=""),
            pod("other", node="n2"),
        ]
    }
    code, _, data = view.node()
    assert code == 200
    first, second = data["nodes"]
    assert first["ready"] is True and second["ready"] is False
    allocation = first["allocatedResources"]
    assert allocation["allocatedPods"] == 2
    assert allocation["cpuRequests"] == 200
    assert allocation["cpuRequestsFraction"] == 10
    assert allocation["memoryRequests"] == 128 * 1024**2
    assert allocation["podFraction"] == 20
    json.dumps(data)


def test_metrics_are_converted_and_cluster_capacity_is_summed(
    view, monkeypatch
):
    from web.api import k8s

    view.core.list_node.return_value = {"items": [node(), node("n2")]}
    view.custom.list_cluster_custom_object.return_value = {
        "items": [
            {
                "metadata": {"name": name},
                "timestamp": "2026-01-01T00:00:00Z",
                "usage": {"cpu": "150000000n", "memory": "1Gi"},
            }
            for name in ("n1", "n2")
        ]
    }
    monkeypatch.setattr(k8s, "get_kubernetes_client", lambda: view)
    monkeypatch.setattr(
        k8s,
        "LonghornClient",
        lambda: SimpleNamespace(
            capacity=lambda: (200, "success", {"total": 10, "available": 8})
        ),
    )
    code, _, data = asyncio.run(k8s.get_overview.__wrapped__(None))
    assert code == 200
    assert data["totalPodCapacity"] == 20
    assert data["totalCpuCapacity"] == 4000
    assert data["cpuUsageRate"][0]["y"] == 0.3
    assert data["memoryUsageRate"][0]["y"] == 2
    assert data["storage_capacity"]["total"] == 10


@pytest.mark.parametrize("status", [403, 404, 503])
def test_missing_metrics_do_not_break_nodes(view, status):
    view.core.list_node.return_value = {"items": [node()]}
    view.custom.list_cluster_custom_object.side_effect = ApiException(
        status=status
    )
    code, _, data = view.node()
    assert code == 200 and len(data["nodes"]) == 1
    assert data["cumulativeMetrics"][0]["dataPoints"] == []
    assert data["errors"][0]["status"] == status


def test_missing_pod_metrics_are_unknown_not_fake_zero(view):
    view.core.list_namespaced_pod.return_value = {"items": [pod()]}
    code, _, data = view.resource_list("pod", "apps")
    assert code == 200
    assert data["pods"][0]["metrics"]["cpuUsage"] is None
    assert data["pods"][0]["metrics"]["cpuUsageHistory"] == []


def test_sdk_models_are_serialized_with_api_field_names(view):
    view.core.list_namespaced_pod.return_value = client.V1PodList(
        items=[
            client.V1Pod(
                metadata=client.V1ObjectMeta(
                    name="actual-model",
                    namespace="apps",
                    uid="uid-1",
                    creation_timestamp=datetime(
                        2026, 1, 1, tzinfo=timezone.utc
                    ),
                ),
                spec=client.V1PodSpec(
                    containers=[
                        client.V1Container(name="app", image="test:v1")
                    ]
                ),
            )
        ]
    )
    _, _, data = view.resource_list("pod", "apps")
    assert data["pods"][0]["objectMeta"]["creationTimestamp"].startswith(
        "2026-01-01"
    )
    json.dumps(data)


def test_filter_sort_and_pagination_have_filtered_total(view):
    view.core.list_namespaced_config_map.return_value = {
        "items": [
            resource("ConfigMap", name)
            for name in ["unrelated", "cache-c", "cache-a", "cache-b"]
        ]
    }
    code, _, data = view.resource_list(
        "configmap", "apps", 1, 2, "a,name", "name,cache-"
    )
    assert code == 200 and data["listMeta"]["totalItems"] == 3
    assert data["items"][0]["objectMeta"]["name"] == "cache-b"


@pytest.mark.parametrize(
    "kind,api,method,key",
    [
        ("pv", "core", "list_persistent_volume", "items"),
        ("pvc", "core", "list_namespaced_persistent_volume_claim", "items"),
        ("ingress", "networking", "list_namespaced_ingress", "items"),
        ("sc", "storage", "list_storage_class", "items"),
    ],
)
def test_resource_dispatch_honors_namespace_scope(
    view, kind, api, method, key
):
    code, _, data = view.resource_list(kind, "apps")
    assert code == 200 and data[key] == []
    kwargs = getattr(getattr(view, api), method).call_args.kwargs
    assert ("namespace" in kwargs) is (kind in {"pvc", "ingress"})
    assert kwargs["_request_timeout"] == (5, 30)


def test_pvc_detail_is_namespaced_and_pv_is_cluster_scoped(view):
    readable(
        view.core,
        "read_namespaced_persistent_volume_claim",
        resource(
            "PersistentVolumeClaim",
            "data",
            spec={"volumeName": "pv-data", "accessModes": ["ReadWriteOnce"]},
            status={"phase": "Bound", "capacity": {"storage": "10Gi"}},
        ),
    )
    readable(
        view.core,
        "read_persistent_volume",
        resource(
            "PersistentVolume",
            "pv-data",
            "",
            spec={
                "capacity": {"storage": "10Gi"},
                "csi": {"driver": "driver", "volumeHandle": "handle"},
            },
            status={"phase": "Bound"},
        ),
    )
    assert (
        view.resource_detail("pvc", "apps", "data")[2]["volume"] == "pv-data"
    )
    assert (
        view.core.read_namespaced_persistent_volume_claim.call_args.kwargs[
            "namespace"
        ]
        == "apps"
    )
    assert (
        view.resource_detail("pv", "ignored", "pv-data")[2][
            "persistentVolumeSource"
        ]["csi"]["driver"]
        == "driver"
    )
    assert "namespace" not in view.core.read_persistent_volume.call_args.kwargs


def test_full_label_selector_and_related_pod_pagination(view):
    selector = {
        "matchLabels": {"component": "cache"},
        "matchExpressions": [
            {"key": "tier", "operator": "In", "values": ["backend", "worker"]},
            {"key": "disabled", "operator": "DoesNotExist"},
            {"key": "zone", "operator": "NotIn", "values": ["blocked"]},
            {"key": "managed", "operator": "Exists"},
        ],
    }
    readable(
        view.apps,
        "read_namespaced_stateful_set",
        resource("StatefulSet", "release", spec={"selector": selector}),
    )
    view.core.list_namespaced_pod.return_value = {
        "items": [pod("a"), pod("b"), pod("c")]
    }
    code, _, data = view.resource_pod(
        "statefulset", "apps", "release", 1, 2, "a,name"
    )
    assert code == 200 and data["listMeta"]["totalItems"] == 3
    assert data["pods"][0]["objectMeta"]["name"] == "b"
    query = view.core.list_namespaced_pod.call_args.kwargs["label_selector"]
    assert "component=cache" in query and "tier in (backend,worker)" in query
    assert (
        "!disabled" in query
        and "zone notin (blocked)" in query
        and "managed" in query
    )
    assert "app=release" not in query
    assert selected(
        {"component": "cache", "tier": "backend", "managed": "yes"}, selector
    )
    assert not selected(
        {"component": "cache", "tier": "frontend", "managed": "yes"}, selector
    )


def test_selectorless_service_never_returns_all_namespace_pods(view):
    readable(
        view.core, "read_namespaced_service", resource("Service", "manual")
    )
    code, _, data = view.resource_pod("service", "apps", "manual")
    assert code == 200 and data["pods"] == []
    view.core.list_namespaced_pod.assert_not_called()


@pytest.mark.parametrize(
    "kind,method",
    [
        ("deployment", "list_namespaced_deployment"),
        ("statefulset", "list_namespaced_stateful_set"),
        ("daemonset", "list_namespaced_daemon_set"),
        ("replicaset", "list_namespaced_replica_set"),
    ],
)
def test_workload_views_include_images_and_real_pod_counts(view, kind, method):
    raw = resource(
        kind,
        "workload",
        spec={
            "replicas": 3,
            "selector": {"matchLabels": {"part": "worker"}},
            "template": {"spec": {"containers": [{"image": "worker:v1"}]}},
        },
        status={"desiredNumberScheduled": 3},
    )
    getattr(view.apps, method).return_value = {"items": [raw]}
    view.core.list_namespaced_pod.return_value = {
        "items": [
            pod("a", labels={"part": "worker"}),
            pod("b", phase="Pending", labels={"part": "worker"}),
            pod("foreign", labels={"part": "other"}),
        ]
    }
    code, _, data = view.resource_list(kind, "apps")
    assert code == 200
    row = data[RESOURCES[kind][2]][0]
    assert row["containerImages"] == ["worker:v1"]
    assert row["podInfo"]["current"] == 2
    assert row["podInfo"]["desired"] == 3
    assert row["podInfo"]["running"] == 1 and row["podInfo"]["pending"] == 1
    view.core.list_namespaced_pod.assert_called_once()


def test_pod_details_have_safe_nested_defaults_and_events(view):
    readable(view.core, "read_namespaced_pod", pod())
    code, _, data = view.resource_detail("pod", "apps", "example")
    assert code == 200
    assert data["controller"]["pods"]["running"] == 0
    assert data["objectMeta"]["ownerReferences"] == []
    assert data["metrics"][0]["metricName"] == "cpu/usage_rate"
    c = data["containers"][0]
    assert c["env"] == [] and c["args"] == [] and c["commands"] == []
    assert c["securityContext"]["capabilities"]["drop"] == []
    assert c["readinessProbe"]["exec"]["command"] == []
    assert data["eventList"]["events"] == []
    json.dumps(data)


def test_service_endpoints_use_endpoint_slices(view):
    readable(
        view.core,
        "read_namespaced_service",
        resource(
            "Service",
            "cache",
            spec={
                "clusterIP": "10.0.0.1",
                "ports": [{"port": 6379}],
                "selector": {"component": "cache"},
            },
        ),
    )
    view.discovery.list_namespaced_endpoint_slice.return_value = {
        "items": [
            {
                "ports": [{"port": 6379}],
                "endpoints": [
                    {
                        "addresses": ["10.1.0.1"],
                        "nodeName": "n1",
                        "conditions": {"ready": True},
                    },
                    {
                        "addresses": ["10.1.0.2"],
                        "conditions": {"ready": False},
                    },
                ],
            }
        ]
    }
    code, _, data = view.resource_detail("service", "apps", "cache")
    assert code == 200 and data["endpointList"]["listMeta"]["totalItems"] == 2
    assert data["endpointList"]["endpoints"][1]["ready"] is False
    assert (
        view.discovery.list_namespaced_endpoint_slice.call_args.kwargs[
            "label_selector"
        ]
        == "kubernetes.io/service-name=cache"
    )


def test_legacy_encoded_relations_and_new_replicaset_use_real_ownership(view):
    deployment = resource(
        "Deployment",
        "release",
        spec={"selector": {"matchLabels": {"app": "release"}}},
        annotations={"deployment.kubernetes.io/revision": "2"},
    )
    readable(view.apps, "read_namespaced_deployment", deployment)
    view.apps.list_namespaced_replica_set.return_value = {
        "items": [
            resource(
                "ReplicaSet",
                "foreign",
                ownerReferences=[{"uid": "other", "controller": True}],
            ),
            resource(
                "ReplicaSet",
                "current",
                annotations={"deployment.kubernetes.io/revision": "2"},
                ownerReferences=[{"uid": "uid-release", "controller": True}],
            ),
        ]
    }
    assert (
        view.resource_detail(
            "deployment", "apps", "release%252Fnewreplicaset"
        )[2]["objectMeta"]["name"]
        == "current"
    )
    readable(
        view.core,
        "read_namespaced_pod",
        pod(
            labels={"component": "cache"},
            spec={
                "volumes": [{"persistentVolumeClaim": {"claimName": "data"}}]
            },
        ),
    )
    view.core.list_namespaced_service.return_value = {
        "items": [
            resource(
                "Service",
                "matching",
                spec={"selector": {"component": "cache"}},
            ),
            resource(
                "Service", "other", spec={"selector": {"component": "other"}}
            ),
            resource("Service", "manual"),
        ]
    }
    assert (
        view.resource_detail("pod", "apps", "example%2Fservice")[2][
            "listMeta"
        ]["totalItems"]
        == 1
    )
    view.core.list_namespaced_persistent_volume_claim.return_value = {
        "items": [
            resource("PersistentVolumeClaim", "data"),
            resource("PersistentVolumeClaim", "other"),
        ]
    }
    assert (
        view.resource_detail("pod", "apps", "example/persistentvolumeclaim")[
            2
        ]["items"][0]["objectMeta"]["name"]
        == "data"
    )


@pytest.mark.parametrize("status", [403, 404, 500])
def test_essential_api_errors_are_not_successful_empty_lists(view, status):
    view.core.list_namespaced_pod.side_effect = ApiException(
        status=status, reason="Failed", body=json.dumps({"message": "denied"})
    )
    code, message, data = view.resource_list("pod", "apps")
    assert code == status and message == "denied" and data == {}


def test_unknown_resources_bad_relations_and_pagination_are_rejected(view):
    assert view.resource_list("arbitrary")[0] == 400
    assert view.resource_list("pod", page=0)[0] == 400
    assert (
        view.resource_detail("pod", "apps", "example/../../secret")[0] == 400
    )
    assert view.resource_pod("secret", "apps", "example")[0] == 400


def test_history_is_bounded_deduplicated_and_does_not_fabricate_samples():
    history = MetricHistory(max_resources=1, max_samples=2)
    assert history.record("a", "2026-01-01T00:00:00Z", 1, 2)[0][
        "dataPoints"
    ] == [{"x": 1767225600.0, "y": 1}]
    history.record("a", "2026-01-01T00:00:00Z", 3, 4)
    history.record("a", "2026-01-01T00:01:00Z", 5, 6)
    series = history.record("a", "2026-01-01T00:02:00Z", 7, 8)
    assert [p["y"] for p in series[0]["dataPoints"]] == [5, 7]
    history.record("b", "2026-01-01T00:02:00Z", 9, 10)
    assert list(history.entries) == ["b"]


@pytest.mark.parametrize(
    "explicit,environment", [("/explicit", "/env"), (None, "/env")]
)
def test_kubeconfig_precedes_incluster_without_changing_global_config(
    monkeypatch, explicit, environment
):
    monkeypatch.setenv("KUBECONFIG", environment)
    load = MagicMock()
    incluster = MagicMock()
    monkeypatch.setattr(config, "load_kube_config", load)
    monkeypatch.setattr(config, "load_incluster_config", incluster)
    before = client.Configuration.get_default_copy().host
    view = KubernetesViewClient(kubeconfig_path=explicit)
    assert load.call_args.kwargs["config_file"] == (explicit or environment)
    assert load.call_args.kwargs["client_configuration"] is not None
    incluster.assert_not_called()
    assert client.Configuration.get_default_copy().host == before
    view.api_client.close()


def test_limit_only_resources_use_kubernetes_request_defaults():
    raw = pod(
        spec={
            "containers": [
                {"resources": {"limits": {"cpu": "500m", "memory": "1Gi"}}}
            ]
        }
    )
    totals = pod_resources(raw)
    assert totals["cpuRequests"] == totals["cpuLimits"] == 500
    assert totals["memoryRequests"] == totals["memoryLimits"] == 1024**3


def test_controller_lookup_uses_owner_uid(view):
    raw = pod(
        ownerReferences=[
            {
                "name": "old",
                "kind": "ReplicaSet",
                "uid": "deleted-uid",
                "controller": True,
            }
        ]
    )
    readable(view.core, "read_namespaced_pod", raw)
    readable(
        view.apps, "read_namespaced_replica_set", resource("ReplicaSet", "old")
    )
    assert (
        view.resource_detail("pod", "apps", "example")[2]["controller"][
            "objectMeta"
        ]["name"]
        == ""
    )


def test_service_related_ingress_uses_backends_not_name_prefix(view):
    readable(
        view.core, "read_namespaced_service", resource("Service", "cache")
    )
    view.networking.list_namespaced_ingress.return_value = {
        "items": [
            resource(
                "Ingress",
                "gateway",
                spec={
                    "rules": [
                        {
                            "host": "cache.test",
                            "http": {
                                "paths": [
                                    {
                                        "path": "/",
                                        "backend": {
                                            "service": {
                                                "name": "cache",
                                                "port": {"number": 80},
                                            }
                                        },
                                    }
                                ]
                            },
                        }
                    ]
                },
            ),
            resource(
                "Ingress",
                "cache-unrelated",
                spec={"defaultBackend": {"service": {"name": "other"}}},
            ),
        ]
    }
    code, _, data = view.resource_detail(
        "service", "apps", "cache%252Fingress"
    )
    assert code == 200 and data["listMeta"]["totalItems"] == 1
    assert data["items"][0]["objectMeta"]["name"] == "gateway"
    assert data["items"][0]["spec"]["rules"][0]["host"] == "cache.test"


def test_secret_values_remain_base64_and_configmaps_remain_strings(view):
    secret = resource("Secret", "tls")
    secret["data"] = {"tls.crt": "dGVzdA=="}
    readable(view.core, "read_namespaced_secret", secret)
    assert (
        view.resource_detail("secret", "apps", "tls")[2]["data"]["tls.crt"]
        == "dGVzdA=="
    )
    cm = resource("ConfigMap", "config")
    cm["data"] = {"settings": "unchanged\n"}
    readable(view.core, "read_namespaced_config_map", cm)
    assert (
        view.resource_detail("configmap", "apps", "config")[2]["data"][
            "settings"
        ]
        == "unchanged\n"
    )


def test_node_details_have_metrics_and_events_even_when_unavailable(view):
    readable(view.core, "read_node", node())
    view.custom.list_cluster_custom_object.side_effect = ApiException(
        status=404
    )
    view.core.list_event_for_all_namespaces.side_effect = ApiException(
        status=403
    )
    code, _, data = view.node("n1")
    assert code == 200
    assert [metric["metricName"] for metric in data["metrics"]] == [
        "cpu/usage_rate",
        "memory/usage",
    ]
    assert data["eventList"]["events"] == []
    assert {error["source"] for error in data["errors"]} == {
        "metrics.k8s.io",
        "events",
    }


def test_routes_support_canonical_and_legacy_contracts(monkeypatch, view):
    from fastapi import FastAPI

    from web.api import k8s

    app = FastAPI()
    app.include_router(k8s.router, prefix="/api/v1/k8s")
    canonical = "/api/v1/k8s/resource/{type}"
    legacy = "/api/v1/k8s/dashboard/resource/{type}"
    assert canonical in app.openapi()["paths"]
    assert legacy not in app.openapi()["paths"]
    assert any(
        getattr(route, "path", None) == legacy.removeprefix("/api/v1/k8s")
        for route in k8s.router.routes
    )
    monkeypatch.setattr(k8s, "get_kubernetes_client", lambda: view)
    code, _, data = k8s.get_resource_list.__wrapped__(
        request=None,
        type="configmap",
        pagination=SimpleNamespace(page_size=10, page=1),
        name=None,
        namespace="apps",
        label_selector=None,
        field_selector=None,
        sort_by="d,creationTimestamp",
    )
    assert code == 200 and data["items"] == []


def test_missing_connection_config_is_explicit_service_unavailable(
    monkeypatch,
):
    from fastapi import HTTPException

    from web.api import k8s

    k8s.get_kubernetes_client.cache_clear()
    constructor = MagicMock(side_effect=config.ConfigException("missing"))
    monkeypatch.setattr(k8s, "KubernetesViewClient", constructor)
    with pytest.raises(HTTPException) as error:
        k8s.get_kubernetes_client()
    assert error.value.status_code == 503
    assert k8s.get_kubernetes_client.cache_info().currsize == 0


def test_default_deployment_has_no_dashboard_dependency():
    from pathlib import Path

    from core.deployment_inputs import INPUTS

    root = Path(__file__).resolve().parents[1]
    assert "install_dashboard.py" not in INPUTS
    assert not (root / "src/infra/install_dashboard.py").exists()
    assert not (root / "src/core/http_api_client/dashboard_client.py").exists()
    assert (
        '"install_dashboard.py"' not in (root / "src/cli/k8s.py").read_text()
    )


def test_http_routes_keep_auth_and_return_identical_sdk_payloads(
    monkeypatch, view
):
    import httpx
    from fastapi import FastAPI

    from web.api import k8s
    from web.utils import auth

    app = FastAPI()
    app.include_router(k8s.router, prefix="/api/v1/k8s")
    paths = [
        "/api/v1/k8s/resource/configmap",
        "/api/v1/k8s/dashboard/resource/configmap",
    ]

    async def request_all():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            return [
                await http.get(path, params={"namespace": "apps"})
                for path in paths
            ]

    assert all(
        response.status_code == 401 for response in asyncio.run(request_all())
    )

    async def identity(**kwargs):
        return SimpleNamespace(username="admin"), "test-identity"

    monkeypatch.setattr(auth, "get_current_user", identity)
    monkeypatch.setattr(k8s, "get_kubernetes_client", lambda: view)
    responses = asyncio.run(request_all())
    assert all(response.status_code == 200 for response in responses)
    assert responses[0].json() == responses[1].json()
    assert responses[0].json()["data"]["listMeta"]["totalItems"] == 0


def test_pod_usage_and_histories_preserve_millicores_and_bytes(view):
    view.core.list_namespaced_pod.return_value = {"items": [pod()]}
    view.custom.list_namespaced_custom_object.return_value = {
        "items": [
            {
                "metadata": {"name": "example", "namespace": "apps"},
                "timestamp": "2026-01-01T00:00:00Z",
                "containers": [
                    {"usage": {"cpu": "120000000n", "memory": "64Mi"}},
                    {"usage": {"cpu": "30m", "memory": "16Mi"}},
                ],
            }
        ]
    }
    code, _, data = view.resource_list("pod", "apps")
    assert code == 200
    usage = data["pods"][0]["metrics"]
    assert usage["cpuUsage"] == 150
    assert usage["memoryUsage"] == 80 * 1024**2
    assert usage["cpuUsageHistory"][0]["value"] == 150
    assert len(usage["memoryUsageHistory"]) == 1


@pytest.mark.parametrize(
    "kind,api,stem,namespaced",
    [
        ("job", "batch", "job", True),
        ("cronjob", "batch", "cron_job", True),
        (
            "horizontalpodautoscaler",
            "autoscaling",
            "horizontal_pod_autoscaler",
            True,
        ),
        ("ingressclass", "networking", "ingress_class", False),
        ("networkpolicy", "networking", "network_policy", True),
        ("event", "core", "event", True),
    ],
)
def test_new_resource_types_use_correct_sdk_scope(
    view, kind, api, stem, namespaced
):
    method = f"list_namespaced_{stem}" if namespaced else f"list_{stem}"
    getattr(getattr(view, api), method).return_value = {
        "items": [resource(kind)]
    }
    code, _, data = view.resource_list(kind, "apps")
    assert code == 200 and data["listMeta"]["totalItems"] == 1
    kwargs = getattr(getattr(view, api), method).call_args.kwargs
    assert (kwargs.get("namespace") == "apps") == namespaced


def test_label_and_field_selectors_are_applied_before_pagination(view):
    view.core.list_namespaced_pod.return_value = {"items": [pod()]}
    code, _, data = view.resource_list(
        "pod",
        "apps",
        label_selector="app in (api,web)",
        field_selector="spec.nodeName=n1",
    )
    assert code == 200 and data["listMeta"]["totalItems"] == 1
    assert (
        view.core.list_namespaced_pod.call_args.kwargs["label_selector"]
        == "app in (api,web)"
    )
    assert (
        view.core.list_namespaced_pod.call_args.kwargs["field_selector"]
        == "spec.nodeName=n1"
    )


def test_secret_manifest_masks_data_and_annotations_until_revealed(
    view,
):
    secret = resource("Secret")
    secret["data"] = {"password": "cHJpdmF0ZQ=="}
    secret["stringData"] = {"extra": "private-literal"}
    secret["metadata"]["annotations"] = {
        "kubectl.kubernetes.io/last-applied-configuration": "private-literal"
    }
    secret["metadata"]["managedFields"] = [{"manager": "kubectl"}]
    readable(view.core, "read_namespaced_secret", secret)
    code, _, data = view.resource_manifest("secret", "apps", "example")
    assert code == 200 and data["redacted"]
    assert (
        "private-literal" not in data["yaml"]
        and "cHJpdmF0ZQ==" not in data["yaml"]
    )
    assert "managedFields" not in data["manifest"]["metadata"]
    assert data["manifest"]["data"]["password"] == "<已隐藏>"
    code, _, revealed = view.resource_manifest(
        "secret", "apps", "example", reveal_secret=True
    )
    assert code == 200 and not revealed["redacted"]
    assert revealed["manifest"]["data"]["password"] == "cHJpdmF0ZQ=="


def test_resource_manifest_returns_actual_kubernetes_document(view):
    readable(
        view.apps,
        "read_namespaced_deployment",
        resource("Deployment", spec={"replicas": 3}),
    )
    code, _, data = view.resource_manifest("deployment", "apps", "example")
    assert code == 200 and data["manifest"]["spec"]["replicas"] == 3
    assert "replicas: 3" in data["yaml"] and "kind: Deployment" in data["yaml"]
    view.apps.read_namespaced_deployment.side_effect = ApiException(
        status=403, reason="Forbidden"
    )
    assert view.resource_manifest("deployment", "apps", "example")[0] == 403


def test_resource_events_use_object_uid_and_page_after_time_sort(view):
    readable(view.core, "read_namespaced_pod", pod())
    items = []
    for day in [1, 3, 2]:
        event = resource("Event", name=f"e{day}")
        event.update(
            type="Warning",
            reason="BackOff",
            message="container restarting",
            lastTimestamp=f"2026-01-0{day}T00:00:00Z",
            involvedObject={"kind": "Pod", "name": "example"},
        )
        items.append(event)
    view.core.list_namespaced_event.return_value = {"items": items}
    code, _, data = view.resource_events(
        "pod", "apps", "example", items_per_page=2
    )
    assert code == 200 and data["listMeta"]["totalItems"] == 3
    assert [e["objectMeta"]["name"] for e in data["events"]] == ["e3", "e2"]
    assert data["events"][0]["involvedObject"]["kind"] == "Pod"
    assert (
        view.core.list_namespaced_event.call_args.kwargs["field_selector"]
        == "involvedObject.uid=uid-example"
    )
    view.core.list_namespaced_event.side_effect = ApiException(
        status=403, reason="Forbidden"
    )
    code, _, data = view.resource_events("pod", "apps", "example")
    assert code == 200 and data["errors"][0]["status"] == 403


def test_cluster_events_preserve_new_series_timestamp_and_count(view):
    event = resource("Event")
    event.update(
        type="Warning",
        message="failure",
        reason="FailedScheduling",
        series={"lastObservedTime": "2026-01-01T00:00:00Z", "count": 4},
        involvedObject={"kind": "Pod", "name": "pending"},
    )
    view.core.list_event_for_all_namespaces.return_value = {"items": [event]}
    code, _, data = view.resource_list("event", field_selector="type=Warning")
    assert code == 200 and data["events"][0]["count"] == 4
    assert data["events"][0]["lastTimestamp"] == "2026-01-01T00:00:00Z"


def test_node_pods_are_selected_by_actual_node_name(view):
    readable(view.core, "read_node", node())
    view.core.list_pod_for_all_namespaces.return_value = {"items": [pod()]}
    code, _, data = view.resource_pod("node", "_cluster", "n1")
    assert code == 200 and len(data["pods"]) == 1
    assert (
        view.core.list_pod_for_all_namespaces.call_args.kwargs[
            "field_selector"
        ]
        == "spec.nodeName=n1"
    )


def test_cronjob_pods_follow_job_owner_uids_not_guessed_labels(view):
    cron = resource("CronJob", "nightly")
    job = resource(
        "Job",
        "nightly-123",
        ownerReferences=[
            {
                "uid": cron["metadata"]["uid"],
                "kind": "CronJob",
                "name": "nightly",
            }
        ],
    )
    owned = pod(
        "owned",
        ownerReferences=[{"kind": "Job", "uid": job["metadata"]["uid"]}],
    )
    unrelated = pod(
        "other", ownerReferences=[{"kind": "Job", "uid": "unrelated"}]
    )
    readable(view.batch, "read_namespaced_cron_job", cron)
    view.batch.list_namespaced_job.return_value = {"items": [job]}
    view.core.list_namespaced_pod.return_value = {"items": [owned, unrelated]}
    code, _, data = view.resource_pod("cronjob", "apps", "nightly")
    assert code == 200 and [p["objectMeta"]["name"] for p in data["pods"]] == [
        "owned"
    ]


def test_pod_waiting_reason_is_visible_in_resource_list(view):
    value = pod()
    value["status"]["containerStatuses"] = [
        {"name": "main", "state": {"waiting": {"reason": "CrashLoopBackOff"}}}
    ]
    view.core.list_namespaced_pod.return_value = {"items": [value]}
    assert (
        view.resource_list("pod", "apps")[2]["pods"][0]["status"]
        == "CrashLoopBackOff"
    )


def test_logs_are_bounded_and_support_init_previous_and_container_validation(
    view,
):
    value = pod(
        spec={
            "initContainers": [{"name": "setup"}],
            "ephemeralContainers": [{"name": "debugger"}],
        }
    )
    readable(view.core, "read_namespaced_pod", value)
    view.core.read_namespaced_pod_log.return_value = (
        "2026-01-01T00:00:00Z ready\n"
    )
    code, _, data = view.pod_logs(
        "apps",
        "example",
        container="setup",
        tail_lines=500,
        since_seconds=600,
        previous=True,
    )
    assert code == 200 and data["container"] == "setup" and data["previous"]
    kwargs = view.core.read_namespaced_pod_log.call_args.kwargs
    assert kwargs["limit_bytes"] == 1024 * 1024 and kwargs["follow"] is False
    assert (
        kwargs["tail_lines"] == 500
        and kwargs["since_seconds"] == 600
        and kwargs["previous"] is True
    )
    assert view.pod_logs("apps", "example", container="debugger")[0] == 200
    view.core.read_namespaced_pod_log.reset_mock()
    assert view.pod_logs("apps", "example", container="unrelated")[0] == 400
    view.core.read_namespaced_pod_log.assert_not_called()


@pytest.mark.parametrize(
    "params",
    [
        {"tail_lines": 0},
        {"tail_lines": 2001},
        {"since_seconds": 0},
        {"since_seconds": 604801},
    ],
)
def test_invalid_log_limits_do_not_access_cluster(view, params):
    assert view.pod_logs("apps", "example", **params)[0] == 400
    view.core.read_namespaced_pod.assert_not_called()
    view.core.read_namespaced_pod_log.assert_not_called()


def test_missing_previous_logs_preserve_real_api_error(view):
    readable(view.core, "read_namespaced_pod", pod())
    view.core.read_namespaced_pod_log.side_effect = ApiException(
        status=400,
        reason="BadRequest",
        body=json.dumps(
            {"message": "previous terminated container not found"}
        ),
    )
    code, message, _ = view.pod_logs("apps", "example", previous=True)
    assert code == 400 and "previous terminated container" in message


def test_inspection_http_routes_require_auth_and_validate_log_ranges(
    monkeypatch, view
):
    import httpx
    from fastapi import FastAPI

    from web.api import k8s
    from web.utils import auth

    app = FastAPI()
    app.include_router(k8s.router, prefix="/api/v1/k8s")
    paths = [
        "/manifest/pod/apps/example",
        "/events/pod/apps/example",
        "/pods/apps/example/logs",
    ]

    async def requests():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            return [await http.get("/api/v1/k8s" + path) for path in paths]

    assert all(
        response.status_code == 401 for response in asyncio.run(requests())
    )

    async def identity(**kwargs):
        return SimpleNamespace(username="admin"), "test-identity"

    monkeypatch.setattr(auth, "get_current_user", identity)
    monkeypatch.setattr(k8s, "get_kubernetes_client", lambda: view)
    readable(view.core, "read_namespaced_pod", pod())
    view.core.read_namespaced_pod_log.return_value = "ready"
    responses = asyncio.run(requests())
    assert all(
        response.status_code == 200 and response.json()["code"] == 200
        for response in responses
    )

    async def invalid():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            return await http.get(
                "/api/v1/k8s/pods/apps/example/logs",
                params={"tail_lines": 5000},
            )

    assert asyncio.run(invalid()).status_code == 422


def test_pod_mounts_join_volume_definitions_by_name(view):
    value = pod(
        spec={
            "containers": [
                {
                    "name": "main",
                    "image": "example:v1",
                    "volumeMounts": [
                        {
                            "name": "settings",
                            "mountPath": "/etc/settings",
                            "readOnly": True,
                        },
                        {
                            "name": "data",
                            "mountPath": "/data",
                            "subPath": "app",
                        },
                        {"name": "auth", "mountPath": "/auth"},
                        {"name": "access", "mountPath": "/var/run/access"},
                        {"name": "scratch", "mountPath": "/tmp"},
                        {"name": "missing", "mountPath": "/unknown"},
                    ],
                },
                {"name": "sidecar", "image": "sidecar:v1"},
            ],
            "volumes": [
                {
                    "name": "data",
                    "persistentVolumeClaim": {"claimName": "data-pvc"},
                },
                {"name": "settings", "configMap": {"name": "app-settings"}},
                {"name": "auth", "secret": {"secretName": "app-secret"}},
                {"name": "scratch", "emptyDir": {}},
                {
                    "name": "access",
                    "projected": {
                        "sources": [{"serviceAccountToken": {"path": "token"}}]
                    },
                },
            ],
        }
    )
    readable(view.core, "read_namespaced_pod", value)
    code, _, detail = view.resource_detail("pod", "apps", "example")
    assert code == 200
    mounts = detail["containers"][0]["volumeMounts"]
    assert mounts[0]["volume"]["configMap"]["name"] == "app-settings"
    assert mounts[0]["readOnly"] is True
    assert (
        mounts[1]["volume"]["persistentVolumeClaim"]["claimName"] == "data-pvc"
    )
    assert mounts[1]["subPath"] == "app"
    assert mounts[2]["volume"]["secret"]["secretName"] == "app-secret"
    assert (
        mounts[3]["volume"]["projected"]["sources"][0]["serviceAccountToken"][
            "path"
        ]
        == "token"
    )
    assert mounts[4]["volume"]["emptyDir"] == {}
    assert mounts[5]["volume"] == {} and mounts[5]["mountPath"] == "/unknown"
    assert detail["containers"][1]["volumeMounts"] == []


def test_pod_mount_without_volume_definitions_is_safe(view):
    value = pod(
        spec={
            "containers": [
                {
                    "name": "main",
                    "image": "example:v1",
                    "volumeMounts": [
                        {"name": "missing", "mountPath": "/data"}
                    ],
                }
            ]
        }
    )
    readable(view.core, "read_namespaced_pod", value)
    assert (
        view.resource_detail("pod", "apps", "example")[2]["containers"][0][
            "volumeMounts"
        ][0]["volume"]
        == {}
    )
