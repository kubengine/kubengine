"""通过官方 SDK 构建工作台数据，不依赖 Kubernetes Dashboard。

SDK 原始资源和界面 DTO 在此集中转换。Metrics API 只提供当前采样，
趋势仅保存本进程实际读取过的采样，不插值或伪造历史数据。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone
from decimal import Decimal
from functools import wraps
from typing import Any, Callable, Hashable, ParamSpec, cast
from urllib.parse import unquote

import yaml
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.utils.quantity import parse_quantity
from urllib3.exceptions import HTTPError

Raw = dict[str, Any]
Result = tuple[int, str, Any]
P = ParamSpec("P")

TIMEOUT = (5, 30)
# API 属性、SDK 方法名、返回列表字段、是否按命名空间隔离。
RESOURCES = {
    "pod": ("core", "pod", "pods", True),
    "node": ("core", "node", "nodes", False),
    "namespace": ("core", "namespace", "namespaces", False),
    "service": ("core", "service", "services", True),
    "configmap": ("core", "config_map", "items", True),
    "secret": ("core", "secret", "secrets", True),
    "persistentvolume": ("core", "persistent_volume", "items", False),
    "persistentvolumeclaim": (
        "core",
        "persistent_volume_claim",
        "items",
        True,
    ),
    "deployment": ("apps", "deployment", "deployments", True),
    "statefulset": ("apps", "stateful_set", "statefulSets", True),
    "daemonset": ("apps", "daemon_set", "daemonSets", True),
    "replicaset": ("apps", "replica_set", "replicaSets", True),
    "ingress": ("networking", "ingress", "items", True),
    "storageclass": ("storage", "storage_class", "items", False),
    "job": ("batch", "job", "jobs", True),
    "cronjob": ("batch", "cron_job", "cronJobs", True),
    "horizontalpodautoscaler": (
        "autoscaling",
        "horizontal_pod_autoscaler",
        "items",
        True,
    ),
    "ingressclass": ("networking", "ingress_class", "items", False),
    "networkpolicy": ("networking", "network_policy", "items", True),
    "event": ("core", "event", "events", True),
}
ALIASES = {
    "pv": "persistentvolume",
    "pvc": "persistentvolumeclaim",
    "svc": "service",
    "cm": "configmap",
    "deploy": "deployment",
    "sts": "statefulset",
    "ds": "daemonset",
    "rs": "replicaset",
    "sc": "storageclass",
    "tls": "secret",
    "hpa": "horizontalpodautoscaler",
}
WORKLOADS = {"deployment", "statefulset", "daemonset", "replicaset"}


def resource_kind(value: str) -> str:
    from core.authorization import kind_name

    value = kind_name(value)
    if value not in RESOURCES:
        raise ValueError(f"不支持的资源类型：{value}")
    return value


def hide_secret(value):
    import copy

    value = copy.deepcopy(value)
    for field in ("data", "stringData", "binaryData"):
        if field in value:
            value[field] = {key: "<已隐藏>" for key in value[field] or {}}
    for field in ("metadata", "objectMeta"):
        if field in value:
            meta = value[field]
            # 注解和标签可能携带 last-applied 或凭据，只保留安全的身份字段。
            value[field] = {
                key: meta[key]
                for key in (
                    "name",
                    "namespace",
                    "uid",
                    "resourceVersion",
                    "creationTimestamp",
                )
                if key in meta
            }
    return value


def quantity(value: Any, *, cpu: bool = False) -> float:
    amount = cast(Callable[[str], Decimal], parse_quantity)(str(value or 0))
    if not amount.is_finite() or amount < 0:
        raise ValueError("资源数量必须为有限非负值")
    return float(amount * (1000 if cpu else 1))


def pod_resources(pod: Raw) -> Raw:
    """计算普通容器、init/sidecar 和 overhead 的有效资源需求。"""
    spec = pod.get("spec") or {}
    totals = {}
    for category, suffix in (("requests", "Requests"), ("limits", "Limits")):
        for name, prefix in (("cpu", "cpu"), ("memory", "memory")):

            def amount(container: Raw) -> float:
                resources = container.get("resources") or {}
                return quantity(
                    (resources.get(category) or {}).get(
                        name,
                        (
                            (resources.get("limits") or {}).get(name)
                            if category == "requests"
                            else None
                        ),
                    ),
                    cpu=name == "cpu",
                )

            steady = sum(amount(c) for c in spec.get("containers") or [])
            sidecars = 0.0
            init_peak = 0.0
            for container in spec.get("initContainers") or []:
                current = amount(container)
                if container.get("restartPolicy") == "Always":
                    sidecars += current
                    steady += current
                    current = sidecars
                else:
                    current += sidecars
                init_peak = max(init_peak, current)
            pod_level = (spec.get("resources") or {}).get(category) or {}
            effective = (
                quantity(pod_level[name], cpu=name == "cpu")
                if name in pod_level
                else max(steady, init_peak)
            )
            overhead = quantity(
                (spec.get("overhead") or {}).get(name), cpu=name == "cpu"
            )
            # 未设置 limit 时，不能把 overhead 单独当成资源上限。
            totals[prefix + suffix] = effective + (
                overhead if category == "requests" or effective else 0
            )
    return totals


def selector_text(selector: Raw | None, *, service: bool = False) -> str:
    if not selector:
        return ""
    labels = selector if service else selector.get("matchLabels") or {}
    terms = [f"{key}={value}" for key, value in sorted(labels.items())]
    if not service:
        for expr in selector.get("matchExpressions") or []:
            key, op = expr["key"], expr["operator"]
            values = ",".join(expr.get("values") or [])
            if op in {"In", "NotIn"}:
                terms.append(
                    f"{key} {'in' if op == 'In' else 'notin'} ({values})"
                )
            elif op == "Exists":
                terms.append(key)
            elif op == "DoesNotExist":
                terms.append(f"!{key}")
            else:
                raise ValueError(f"不支持的标签选择器：{op}")
    return ",".join(terms)


def selected(labels: Raw, selector: Raw | None) -> bool:
    if not selector:
        return False
    if any(
        labels.get(k) != v
        for k, v in (selector.get("matchLabels") or {}).items()
    ):
        return False
    for expr in selector.get("matchExpressions") or []:
        key, op, values = (
            expr["key"],
            expr["operator"],
            expr.get("values") or [],
        )
        if op == "In" and (key not in labels or labels[key] not in values):
            return False
        if op == "NotIn" and key in labels and labels[key] in values:
            return False
        if op == "Exists" and key not in labels:
            return False
        if op == "DoesNotExist" and key in labels:
            return False
        if op not in {"In", "NotIn", "Exists", "DoesNotExist"}:
            raise ValueError(f"不支持的标签选择器：{op}")
    return True


def metadata(raw: Raw) -> Raw:
    result = dict(raw.get("metadata") or {})
    for key in ("labels", "annotations"):
        result[key] = result.get(key) or {}
    result["ownerReferences"] = result.get("ownerReferences") or []
    for key in ("name", "namespace", "uid", "creationTimestamp"):
        result[key] = result.get(key) or ""
    return result


def empty_metrics() -> list[Raw]:
    return [
        {
            "metricName": name,
            "aggregation": "sum",
            "dataPoints": [],
            "metricPoints": [],
        }
        for name in ("cpu/usage_rate", "memory/usage")
    ]


def api_call(method: Callable[P, Raw]) -> Callable[P, Result]:
    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> Result:
        try:
            return 200, "success", method(*args, **kwargs)
        except ApiException as exc:
            message = exc.reason or "Kubernetes API 请求失败"
            if exc.body:
                try:
                    message = json.loads(exc.body).get("message") or message
                except (ValueError, TypeError, AttributeError):
                    pass
            return exc.status or 502, str(message), {}
        except ValueError as exc:
            return 400, str(exc), {}
        except (HTTPError, OSError):
            return 503, "无法连接 Kubernetes API Server", {}

    return wrapped


class MetricHistory:
    """线程安全且容量有界的进程内采样缓存。"""

    def __init__(
        self, max_resources: int = 2048, max_samples: int = 60
    ) -> None:
        self.entries: OrderedDict[
            Hashable, tuple[float, deque[tuple[float, float, float]]]
        ] = OrderedDict()
        self.lock = threading.Lock()
        self.max_resources = max_resources
        self.max_samples = max_samples

    def record(
        self, key: Hashable, timestamp: str, cpu: float, memory: float
    ) -> list[Raw]:
        stamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        epoch = stamp.timestamp()
        with self.lock:
            now = time.monotonic()
            for stale in [
                k for k, (seen, _) in self.entries.items() if now - seen > 1800
            ]:
                del self.entries[stale]
            _, points = self.entries.pop(
                key, (now, deque(maxlen=self.max_samples))
            )
            point = (epoch, cpu, memory)
            if points and epoch == points[-1][0]:
                points[-1] = point
            elif not points or epoch > points[-1][0]:
                points.append(point)
            while points and epoch - points[0][0] > 1800:
                points.popleft()
            self.entries[key] = (now, points)
            while len(self.entries) > self.max_resources:
                self.entries.popitem(last=False)
            result = empty_metrics()
            for index, series in enumerate(result, 1):
                series["dataPoints"] = [
                    {"x": p[0], "y": p[index]} for p in points
                ]
                series["metricPoints"] = [
                    {
                        "timestamp": datetime.fromtimestamp(
                            p[0], timezone.utc
                        ).isoformat(),
                        "value": p[index],
                    }
                    for p in points
                ]
            return result


class ReadContext:
    """一次请求内批量读取，避免每行重复请求集群。"""

    def __init__(self, view: KubernetesViewClient) -> None:
        self.view = view
        self.pod_cache: dict[str | None, list[Raw]] = {}
        self.metric_cache: dict[
            tuple[str, str | None], dict[tuple[str, str], Raw]
        ] = {}
        self.errors: list[Raw] = []

    def pods(self, namespace: str | None = None) -> list[Raw]:
        if namespace not in self.pod_cache:
            self.pod_cache[namespace] = self.view._list("pod", namespace)
        return self.pod_cache[namespace]

    def metrics(
        self, kind: str, namespace: str | None = None
    ) -> dict[tuple[str, str], Raw]:
        key = (kind, namespace)
        if key not in self.metric_cache:
            kwargs: Raw = dict(
                group="metrics.k8s.io",
                version="v1beta1",
                plural=kind,
                _request_timeout=TIMEOUT,
            )
            try:
                if namespace:
                    raw = self.view.custom.list_namespaced_custom_object(
                        namespace=namespace, **kwargs
                    )
                else:
                    raw = self.view.custom.list_cluster_custom_object(**kwargs)
                self.metric_cache[key] = {
                    (metadata(item)["namespace"], metadata(item)["name"]): item
                    for item in cast(Raw, raw).get("items") or []
                }
            except (ApiException, HTTPError, OSError) as exc:
                self.errors.append(
                    {
                        "source": "metrics.k8s.io",
                        "status": getattr(exc, "status", 503),
                        "message": "监控指标暂不可用",
                    }
                )
                self.metric_cache[key] = {}
        return self.metric_cache[key]


class KubernetesViewClient:
    """保持工作台响应契约的只读 Kubernetes SDK 客户端。"""

    def __init__(
        self, kubeconfig_path: str | None = None, api_client: Any = None
    ) -> None:
        if api_client is None:
            configuration = client.Configuration()
            path = kubeconfig_path or os.environ.get("KUBECONFIG")
            if path:
                config.load_kube_config(  # type: ignore[no-untyped-call]
                    config_file=path, client_configuration=configuration
                )
            else:
                try:
                    cast(Callable[..., None], config.load_incluster_config)(
                        client_configuration=configuration
                    )
                except config.ConfigException:
                    default = "/etc/kubernetes/admin.conf"
                    config.load_kube_config(  # type: ignore[no-untyped-call]
                        config_file=(
                            default if os.path.isfile(default) else None
                        ),
                        client_configuration=configuration,
                    )
            api_client = client.ApiClient(configuration)
        self.api_client = api_client
        self.core = client.CoreV1Api(api_client)
        self.apps = client.AppsV1Api(api_client)
        self.networking = client.NetworkingV1Api(api_client)
        self.storage = client.StorageV1Api(api_client)
        self.custom = client.CustomObjectsApi(api_client)
        self.discovery = client.DiscoveryV1Api(api_client)
        self.batch = client.BatchV1Api(api_client)
        self.autoscaling = client.AutoscalingV2Api(api_client)
        self.history = MetricHistory()

    def _plain(self, value: Any) -> Any:
        return self.api_client.sanitize_for_serialization(value)

    def _list(
        self, kind: str, namespace: str | None = None, **kwargs: Any
    ) -> list[Raw]:
        api, stem, _, namespaced = RESOURCES[kind]
        if namespaced:
            method = (
                f"list_namespaced_{stem}"
                if namespace
                else f"list_{stem}_for_all_namespaces"
            )
        else:
            method = f"list_{stem}"
        if namespaced and namespace:
            kwargs["namespace"] = namespace
        raw = getattr(getattr(self, api), method)(
            _request_timeout=TIMEOUT, **kwargs
        )
        return cast(list[Raw], self._plain(raw).get("items") or [])

    def _read(self, kind: str, namespace: str | None, name: str) -> Raw:
        api, stem, _, namespaced = RESOURCES[kind]
        kwargs = {"name": name, "_request_timeout": TIMEOUT}
        if namespaced:
            if not namespace:
                raise ValueError("资源详情必须指定命名空间")
            kwargs["namespace"] = namespace
        method = f"read_{'namespaced_' if namespaced else ''}{stem}"
        return cast(
            Raw, self._plain(getattr(getattr(self, api), method)(**kwargs))
        )

    @staticmethod
    def _page(
        kind: str, items: list[Raw], page: int = 1, size: int = 10
    ) -> Raw:
        if page < 1 or not 1 <= size <= 1000:
            raise ValueError("分页参数无效")
        start = (page - 1) * size
        result = {
            "listMeta": {"totalItems": len(items)},
            RESOURCES[kind][2]: items[start : start + size],
            "errors": [],
        }
        return result

    @staticmethod
    def _sort(items: list[Raw], sort_by: str) -> list[Raw]:
        direction, _, field = sort_by.partition(",")
        if direction not in {"a", "d"} or field not in {
            "creationTimestamp",
            "name",
            "namespace",
        }:
            raise ValueError("不支持的排序参数")
        return sorted(
            items,
            key=lambda r: (
                metadata(r).get(field) or "",
                metadata(r)["namespace"],
                metadata(r)["name"],
            ),
            reverse=direction == "d",
        )

    def _series(self, kind: str, raw: Raw, context: ReadContext) -> list[Raw]:
        meta = metadata(raw)
        metric = context.metrics(kind, meta["namespace"] or None).get(
            (meta["namespace"], meta["name"])
        )
        if not metric:
            return empty_metrics()
        try:
            usage = (
                [metric["usage"]]
                if kind == "nodes"
                else [c["usage"] for c in metric.get("containers") or []]
            )
            if not usage or any(
                "cpu" not in u or "memory" not in u for u in usage
            ):
                return empty_metrics()
            return self.history.record(
                (kind, meta["namespace"], meta["uid"] or meta["name"]),
                metric["timestamp"],
                sum(quantity(u["cpu"], cpu=True) for u in usage),
                sum(quantity(u["memory"]) for u in usage),
            )
        except (KeyError, ValueError, TypeError):
            context.errors.append(
                {"source": "metrics.k8s.io", "message": "监控采样格式无效"}
            )
            return empty_metrics()

    @staticmethod
    def _pod_info(pods: list[Raw], desired: int | None = None) -> Raw:
        counts = {
            phase.lower(): sum(
                (p.get("status") or {}).get("phase") == phase for p in pods
            )
            for phase in ("Running", "Pending", "Failed", "Succeeded")
        }
        return {
            "current": len(pods),
            "desired": desired if desired is not None else len(pods),
            **counts,
            "terminating": sum(
                bool(metadata(p).get("deletionTimestamp")) for p in pods
            ),
            "warnings": [],
        }

    def _related_pods(
        self, kind: str, raw: Raw, context: ReadContext
    ) -> list[Raw]:
        meta, spec = metadata(raw), raw.get("spec") or {}
        selector = spec.get("selector") or {}
        if kind == "service":
            selector = {"matchLabels": selector} if selector else {}
        if kind not in WORKLOADS | {"service", "job"} or not selector:
            return []
        return [
            p
            for p in context.pods(meta["namespace"])
            if selected(metadata(p)["labels"], selector)
        ]

    def _base(self, kind: str, raw: Raw) -> Raw:
        return {
            "objectMeta": metadata(raw),
            "typeMeta": {
                "kind": kind,
                "scalable": kind
                in {"deployment", "statefulset", "replicaset"},
                "restartable": kind in WORKLOADS,
            },
            "errors": [],
        }

    def _events(self, raw: Raw, context: ReadContext) -> Raw:
        meta = metadata(raw)
        field = (
            f"involvedObject.uid={meta['uid']}"
            if meta["uid"]
            else f"involvedObject.name={meta['name']}"
        )
        kwargs: Raw = {"field_selector": field, "_request_timeout": TIMEOUT}
        try:
            if meta["namespace"]:
                events = self.core.list_namespaced_event(
                    namespace=meta["namespace"], **kwargs
                )
            else:
                events = self.core.list_event_for_all_namespaces(**kwargs)
            rows = []
            for item in self._plain(events).get("items") or []:
                rows.append(
                    {
                        **self._base("event", item),
                        "type": item.get("type"),
                        "reason": item.get("reason"),
                        "message": item.get("message"),
                        "count": item.get("count", 1),
                        "involvedObject": item.get("involvedObject") or {},
                        "firstTimestamp": item.get("firstTimestamp"),
                        "lastTimestamp": (
                            (item.get("series") or {}).get("lastObservedTime")
                            or item.get("lastTimestamp")
                            or item.get("eventTime")
                            or metadata(item)["creationTimestamp"]
                        ),
                        "firstSeen": item.get("firstTimestamp"),
                        "lastSeen": item.get("lastTimestamp"),
                        "sourceComponent": (item.get("source") or {}).get(
                            "component", ""
                        ),
                    }
                )
            return {
                "listMeta": {"totalItems": len(rows)},
                "events": rows,
                "errors": [],
            }
        except (ApiException, HTTPError, OSError) as exc:
            error = {
                "source": "events",
                "status": getattr(exc, "status", 503),
                "message": "事件暂不可用",
            }
            context.errors.append(error)
            return {
                "listMeta": {"totalItems": 0},
                "events": [],
                "errors": [error],
            }

    def _node(
        self,
        raw: Raw,
        pods: list[Raw],
        context: ReadContext,
        *,
        detail: bool = False,
    ) -> Raw:
        result = self._base("node", raw)
        meta, status, spec = (
            metadata(raw),
            raw.get("status") or {},
            raw.get("spec") or {},
        )
        capacity = status.get("capacity") or {}
        active = [
            p
            for p in pods
            if (p.get("spec") or {}).get("nodeName") == meta["name"]
            and (p.get("status") or {}).get("phase")
            not in {"Succeeded", "Failed"}
        ]
        allocation = {
            key: 0.0
            for key in (
                "cpuRequests",
                "cpuLimits",
                "memoryRequests",
                "memoryLimits",
            )
        }
        for pod in active:
            for key, value in pod_resources(pod).items():
                allocation[key] += value
        allocation.update(
            cpuCapacity=quantity(capacity.get("cpu"), cpu=True),
            memoryCapacity=quantity(capacity.get("memory")),
            allocatedPods=len(active),
            podCapacity=quantity(capacity.get("pods")),
        )
        for prefix in ("cpu", "memory"):
            for category in ("Requests", "Limits"):
                total = allocation[prefix + "Capacity"]
                allocation[prefix + category + "Fraction"] = (
                    allocation[prefix + category] / total * 100 if total else 0
                )
        allocation["podFraction"] = (
            len(active) / allocation["podCapacity"] * 100
            if allocation["podCapacity"]
            else 0
        )
        conditions = status.get("conditions") or []
        result.update(
            ready=any(
                c.get("type") == "Ready" and c.get("status") == "True"
                for c in conditions
            ),
            allocatedResources=allocation,
        )
        if detail:
            result.update(
                nodeInfo=status.get("nodeInfo") or {},
                phase=status.get("phase") or "Unknown",
                podCIDR=spec.get("podCIDR") or "",
                providerID=spec.get("providerID") or "",
                unschedulable=spec.get("unschedulable") or False,
                conditions=[
                    {**c, "lastProbeTime": c.get("lastHeartbeatTime")}
                    for c in conditions
                ],
                containerImages=sorted(
                    {
                        image
                        for item in status.get("images") or []
                        for image in item.get("names") or []
                    }
                ),
                metrics=self._series("nodes", raw, context),
                taints=spec.get("taints") or [],
                addresses=status.get("addresses") or [],
                podList=self._pod_list(
                    active, context, meta["namespace"] or None
                ),
                eventList=self._events(raw, context),
            )
        return result

    @api_call
    def node(self, name: str | None = None) -> Raw:
        context = ReadContext(self)
        pods = context.pods()
        if name is not None:
            result = self._node(
                self._read("node", None, name), pods, context, detail=True
            )
        else:
            nodes = self._list("node")
            node_metrics = [self._series("nodes", n, context) for n in nodes]
            cumulative = empty_metrics()
            if nodes and all(m[0]["dataPoints"] for m in node_metrics):
                stamp = min(m[0]["dataPoints"][-1]["x"] for m in node_metrics)
                cumulative = self.history.record(
                    ("cluster",),
                    datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
                    sum(m[0]["dataPoints"][-1]["y"] for m in node_metrics),
                    sum(m[1]["dataPoints"][-1]["y"] for m in node_metrics),
                )
            result = {
                "nodes": [self._node(n, pods, context) for n in nodes],
                "cumulativeMetrics": cumulative,
                "listMeta": {"totalItems": len(nodes)},
            }
        result["errors"] = context.errors
        return result

    def _pod(
        self, raw: Raw, context: ReadContext, *, detail: bool = False
    ) -> Raw:
        result = self._base("pod", raw)
        spec, status = raw.get("spec") or {}, raw.get("status") or {}
        statuses = status.get("containerStatuses") or []
        phase = status.get("phase") or "Unknown"
        state = (
            "Terminating" if metadata(raw).get("deletionTimestamp") else phase
        )
        if state != "Terminating":
            for container in statuses:
                waiting = (container.get("state") or {}).get("waiting") or {}
                if waiting.get("reason"):
                    state = waiting["reason"]
                    break
        series = self._series("pods", raw, context)
        containers = spec.get("containers") or []
        result.update(
            status=state,
            nodeName=spec.get("nodeName") or "",
            podIP=status.get("podIP") or "",
            containerImages=[c.get("image", "") for c in containers],
            initContainerImages=[
                c.get("image", "") for c in spec.get("initContainers") or []
            ],
            restartCount=sum(s.get("restartCount") or 0 for s in statuses),
            containerStatuses=[
                {
                    "name": s.get("name"),
                    "ready": s.get("ready") or False,
                    "state": next(
                        (
                            k.capitalize()
                            for k, v in (s.get("state") or {}).items()
                            if v is not None
                        ),
                        "Unknown",
                    ),
                }
                for s in statuses
            ],
            allocatedResources=pod_resources(raw),
            warnings=[],
            metrics={
                "cpuUsage": (
                    series[0]["dataPoints"][-1]["y"]
                    if series[0]["dataPoints"]
                    else None
                ),
                "memoryUsage": (
                    series[1]["dataPoints"][-1]["y"]
                    if series[1]["dataPoints"]
                    else None
                ),
                "cpuUsageHistory": series[0]["metricPoints"],
                "memoryUsageHistory": series[1]["metricPoints"],
            },
        )
        if detail:
            volumes = {
                volume["name"]: volume
                for volume in spec.get("volumes") or []
                if volume.get("name")
            }
            details = []
            for container in containers:
                current: Raw = next(
                    (
                        s
                        for s in statuses
                        if s.get("name") == container.get("name")
                    ),
                    {},
                )
                security = {
                    "capabilities": {"drop": []},
                    **(container.get("securityContext") or {}),
                }
                security["capabilities"] = {
                    "drop": [],
                    **(security.get("capabilities") or {}),
                }
                details.append(
                    {
                        **container,
                        "commands": container.get("command") or [],
                        "args": container.get("args") or [],
                        "env": container.get("env") or [],
                        "volumeMounts": [
                            {
                                **mount,
                                "volume": volumes.get(mount.get("name"), {}),
                            }
                            for mount in container.get("volumeMounts") or []
                        ],
                        "resources": {
                            "requests": {},
                            "limits": {},
                            **(container.get("resources") or {}),
                        },
                        "securityContext": security,
                        "status": current,
                        "state": next(
                            (
                                k.capitalize()
                                for k, v in (
                                    current.get("state") or {}
                                ).items()
                                if v is not None
                            ),
                            "Unknown",
                        ),
                        "livenessProbe": {
                            "exec": {"command": []},
                            **(container.get("livenessProbe") or {}),
                        },
                        "readinessProbe": {
                            "exec": {"command": []},
                            **(container.get("readinessProbe") or {}),
                        },
                    }
                )
            controller = {
                "objectMeta": metadata({}),
                "typeMeta": {"kind": ""},
                "pods": self._pod_info([]),
                "containerImages": [],
            }
            owner = next(
                (
                    r
                    for r in metadata(raw)["ownerReferences"]
                    if r.get("controller")
                ),
                None,
            )
            if owner and owner.get("kind", "").lower() in WORKLOADS:
                kind = owner["kind"].lower()
                try:
                    parent = self._read(
                        kind, metadata(raw)["namespace"], owner["name"]
                    )
                    if (
                        not owner.get("uid")
                        or metadata(parent)["uid"] == owner["uid"]
                    ):
                        controller = self._workload(kind, parent, context)
                except ApiException as exc:
                    context.errors.append(
                        {
                            "source": "controller",
                            "status": exc.status,
                            "message": "所属工作负载暂不可用",
                        }
                    )
            events = self._events(raw, context)
            result.update(
                metrics=series,
                containers=details,
                controller=controller,
                podPhase=phase,
                podIP=status.get("podIP") or "",
                qosClass=status.get("qosClass") or "",
                serviceAccountName=spec.get("serviceAccountName") or "",
                securityContext=spec.get("securityContext") or {},
                conditions=status.get("conditions") or [],
                eventList=events,
                warnings=[
                    e for e in events["events"] if e.get("type") == "Warning"
                ],
            )
        return result

    def _pod_list(
        self,
        pods: list[Raw],
        context: ReadContext,
        namespace: str | None = None,
        page: int = 1,
        size: int = 100,
    ) -> Raw:
        # 排序和分页先于指标序列化，避免在列表页生成所有 Pod 的历史。
        selected_pods = pods[(page - 1) * size : page * size]
        rows = [self._pod(p, context) for p in selected_pods]
        return {
            "listMeta": {"totalItems": len(pods)},
            "pods": rows,
            "status": self._pod_info(pods),
            "cumulativeMetrics": empty_metrics(),
            "errors": context.errors,
        }

    def _workload(self, kind: str, raw: Raw, context: ReadContext) -> Raw:
        spec, status = raw.get("spec") or {}, raw.get("status") or {}
        related = self._related_pods(kind, raw, context)
        desired = (
            status.get("desiredNumberScheduled")
            if kind == "daemonset"
            else spec.get("replicas", 1)
        )
        info = self._pod_info(related, desired or 0)
        template = (spec.get("template") or {}).get("spec") or {}
        result = self._base(kind, raw)
        result.update(
            podInfo=info,
            pods=info,
            selector=spec.get("selector") or {},
            containerImages=[
                c.get("image", "") for c in template.get("containers") or []
            ],
            initContainerImages=[
                c.get("image", "")
                for c in template.get("initContainers") or []
            ],
            conditions=status.get("conditions") or [],
            readyReplicas=(
                status.get("numberReady", 0)
                if kind == "daemonset"
                else status.get("readyReplicas", 0)
            ),
            strategy=(
                spec.get("strategy") or spec.get("updateStrategy") or {}
            ).get("type", ""),
            rollingUpdateStrategy=(spec.get("strategy") or {}).get(
                "rollingUpdate"
            )
            or {},
            minReadySeconds=spec.get("minReadySeconds") or 0,
            revisionHistoryLimit=spec.get("revisionHistoryLimit"),
        )
        if kind == "deployment":
            result["selector"] = (spec.get("selector") or {}).get(
                "matchLabels"
            ) or {}
        return result

    def _service(
        self, raw: Raw, context: ReadContext, *, detail: bool = False
    ) -> Raw:
        result = self._base("service", raw)
        spec, status = raw.get("spec") or {}, raw.get("status") or {}
        ports = spec.get("ports") or []
        result.update(
            type=spec.get("type") or "ClusterIP",
            clusterIP=spec.get("clusterIP") or "",
            selector=spec.get("selector") or {},
            sessionAffinity=spec.get("sessionAffinity") or "None",
            internalEndpoint={
                "host": spec.get("clusterIP") or "",
                "ports": ports,
            },
            externalEndpoints=[
                {"host": e.get("hostname") or e.get("ip", ""), "ports": ports}
                for e in (status.get("loadBalancer") or {}).get("ingress")
                or []
            ],
            endpointList={
                "listMeta": {"totalItems": 0},
                "endpoints": [],
                "errors": [],
            },
        )
        result["externalEndpoints"].extend(
            {"host": ip, "ports": ports}
            for ip in spec.get("externalIPs") or []
        )
        if spec.get("externalName"):
            result["externalEndpoints"].append(
                {"host": spec["externalName"], "ports": ports}
            )
        if detail:
            meta = metadata(raw)
            try:
                slices = self.discovery.list_namespaced_endpoint_slice(
                    namespace=meta["namespace"],
                    label_selector=(
                        f"kubernetes.io/service-name={meta['name']}"
                    ),
                    _request_timeout=TIMEOUT,
                )
                rows = []
                for part in self._plain(slices).get("items") or []:
                    for endpoint in part.get("endpoints") or []:
                        for address in endpoint.get("addresses") or []:
                            rows.append(
                                {
                                    "host": address,
                                    "ports": part.get("ports") or [],
                                    "nodeName": endpoint.get("nodeName") or "",
                                    "ready": (
                                        endpoint.get("conditions") or {}
                                    ).get("ready")
                                    is not False,
                                }
                            )
                result["endpointList"] = {
                    "listMeta": {"totalItems": len(rows)},
                    "endpoints": rows,
                    "errors": [],
                }
            except (ApiException, HTTPError, OSError) as exc:
                context.errors.append(
                    {
                        "source": "endpointslices",
                        "status": getattr(exc, "status", 503),
                        "message": "服务端点暂不可用",
                    }
                )
        return result

    def _format(
        self,
        kind: str,
        raw: Raw,
        context: ReadContext,
        *,
        detail: bool = False,
    ) -> Raw:
        if kind == "pod":
            return self._pod(raw, context, detail=detail)
        if kind in WORKLOADS:
            return self._workload(kind, raw, context)
        if kind == "service":
            return self._service(raw, context, detail=detail)
        if kind == "node":
            return self._node(raw, context.pods(), context, detail=detail)
        result = self._base(kind, raw)
        spec, status = raw.get("spec") or {}, raw.get("status") or {}
        if kind == "event":
            result.update(
                type=raw.get("type") or "Normal",
                reason=raw.get("reason") or "",
                message=raw.get("message") or "",
                involvedObject=raw.get("involvedObject") or {},
                count=raw.get("count")
                or (raw.get("series") or {}).get("count", 1),
                firstTimestamp=raw.get("firstTimestamp"),
                lastTimestamp=(raw.get("series") or {}).get("lastObservedTime")
                or raw.get("lastTimestamp")
                or raw.get("eventTime")
                or metadata(raw)["creationTimestamp"],
                sourceComponent=(raw.get("source") or {}).get("component")
                or raw.get("reportingComponent")
                or "",
            )
        elif kind == "ingress":
            result.update(
                spec=spec,
                endpoints=[
                    {"host": e.get("hostname") or e.get("ip", ""), "ports": []}
                    for e in (status.get("loadBalancer") or {}).get("ingress")
                    or []
                ],
                hosts=[r.get("host", "*") for r in spec.get("rules") or []],
            )
        elif kind == "storageclass":
            result.update(
                provisioner=raw.get("provisioner") or "",
                parameters=raw.get("parameters") or {},
                reclaimPolicy=raw.get("reclaimPolicy") or "Delete",
                volumeBindingMode=raw.get("volumeBindingMode") or "Immediate",
            )
        elif kind in {"configmap", "secret"}:
            result.update(
                data=raw.get("data") or {},
                binaryData=raw.get("binaryData") or {},
            )
            if kind == "secret":
                result["type"] = raw.get("type") or "Opaque"
        elif kind in {"persistentvolume", "persistentvolumeclaim"}:
            result.update(
                status=status.get("phase") or "Unknown",
                capacity=(
                    status if kind == "persistentvolumeclaim" else spec
                ).get("capacity")
                or {},
                accessModes=spec.get("accessModes") or [],
                storageClass=spec.get("storageClassName") or "",
                volume=spec.get("volumeName") or "",
                reclaimPolicy=spec.get("persistentVolumeReclaimPolicy") or "",
                claim="/".join(
                    (spec.get("claimRef") or {}).get(k, "")
                    for k in ("namespace", "name")
                ),
                persistentVolumeSource={
                    k: spec[k]
                    for k in (
                        "csi",
                        "nfs",
                        "hostPath",
                        "local",
                        "iscsi",
                        "cephfs",
                    )
                    if spec.get(k) is not None
                },
            )
        else:
            result.update(spec=spec, status=status)
        return result

    @api_call
    def resource_list(
        self,
        resource_type: str,
        namespace: str | None = None,
        items_per_page: int = 10,
        page: int = 1,
        sort_by: str = "d,creationTimestamp",
        filter_by: str | None = None,
        label_selector: str | None = None,
        field_selector: str | None = None,
        allowed_namespaces: list[str] | None = None,
        reveal_secret: bool = True,
    ) -> Raw:
        kind = resource_kind(resource_type)
        selectors = {
            key: value
            for key, value in {
                "label_selector": label_selector,
                "field_selector": field_selector,
            }.items()
            if value
        }
        if allowed_namespaces is None:
            items = self._list(kind, namespace, **selectors)
        elif kind == "namespace":
            items = [{"metadata": {"name": ns}} for ns in allowed_namespaces]
        else:
            items = [
                row
                for ns in allowed_namespaces
                for row in self._list(kind, ns, **selectors)
            ]
        if filter_by:
            field, _, needle = filter_by.partition(",")
            if field not in {"name", "namespace"}:
                raise ValueError("不支持的资源过滤条件")
            items = [r for r in items if needle in metadata(r)[field]]
        items = self._sort(items, sort_by)
        # 校验分页，并在格式化前截取结果。
        self._page(kind, [], page, items_per_page)
        context = ReadContext(self)
        rows = [
            self._format(kind, r, context)
            for r in items[(page - 1) * items_per_page : page * items_per_page]
        ]
        if kind == "secret" and not reveal_secret:
            rows = [hide_secret(row) for row in rows]
        return {
            "listMeta": {"totalItems": len(items)},
            RESOURCES[kind][2]: rows,
            "errors": context.errors,
        }

    @api_call
    def resource_detail(
        self,
        resource_type: str,
        namespace: str | None,
        name: str,
        reveal_secret: bool = True,
    ) -> Raw:
        kind = resource_kind(resource_type)
        # 保留现有客户端编码的关联资源路径，并严格白名单分派。
        for _ in range(2):
            name = unquote(name)
        name, separator, relation = name.partition("/")
        if not name or (
            separator
            and relation
            not in {
                "newreplicaset",
                "service",
                "ingress",
                "persistentvolumeclaim",
            }
        ):
            raise ValueError("无效的资源名称或关联资源")
        raw = self._read(kind, namespace, name)
        context = ReadContext(self)
        if separator:
            result = self._relation(kind, raw, relation, context)
        else:
            result = self._format(kind, raw, context, detail=True)
        result["errors"] = context.errors
        return (
            hide_secret(result)
            if kind == "secret" and not reveal_secret
            else result
        )

    def _relation(
        self, kind: str, raw: Raw, relation: str, context: ReadContext
    ) -> Raw:
        meta = metadata(raw)
        if relation == "newreplicaset" and kind == "deployment":
            replicas = [
                r
                for r in self._list("replicaset", meta["namespace"])
                if any(
                    o.get("uid") == meta["uid"] and o.get("controller")
                    for o in metadata(r)["ownerReferences"]
                )
            ]
            revision = meta["annotations"].get(
                "deployment.kubernetes.io/revision"
            )
            matching = (
                [
                    r
                    for r in replicas
                    if metadata(r)["annotations"].get(
                        "deployment.kubernetes.io/revision"
                    )
                    == revision
                ]
                if revision
                else []
            )
            if not (matching or replicas):
                return {
                    **self._base("replicaset", {}),
                    "podInfo": self._pod_info([]),
                }
            newest = self._sort(matching or replicas, "d,creationTimestamp")[0]
            return self._workload("replicaset", newest, context)
        if relation == "service" and kind == "pod":
            rows = [
                r
                for r in self._list("service", meta["namespace"])
                if (r.get("spec") or {}).get("selector")
                and selected(
                    meta["labels"], {"matchLabels": r["spec"]["selector"]}
                )
            ]
            return {
                "listMeta": {"totalItems": len(rows)},
                "services": [self._service(r, context) for r in rows],
            }
        if relation == "ingress" and kind == "service":

            def references(ingress: Raw) -> bool:
                spec = ingress.get("spec") or {}
                backends = [spec.get("defaultBackend") or {}]
                for rule in spec.get("rules") or []:
                    backends.extend(
                        p.get("backend") or {}
                        for p in (rule.get("http") or {}).get("paths") or []
                    )
                return any(
                    (b.get("service") or {}).get("name") == meta["name"]
                    for b in backends
                )

            rows = [
                r
                for r in self._list("ingress", meta["namespace"])
                if references(r)
            ]
            return {
                "listMeta": {"totalItems": len(rows)},
                "items": [self._format("ingress", r, context) for r in rows],
            }
        if relation == "persistentvolumeclaim" and kind == "pod":
            names = {
                (v.get("persistentVolumeClaim") or {}).get("claimName")
                for v in (raw.get("spec") or {}).get("volumes") or []
            }
            rows = [
                r
                for r in self._list("persistentvolumeclaim", meta["namespace"])
                if metadata(r)["name"] in names
            ]
            return {
                "listMeta": {"totalItems": len(rows)},
                "items": [
                    self._format("persistentvolumeclaim", r, context)
                    for r in rows
                ],
            }
        raise ValueError("不支持的关联资源查询")

    @api_call
    def resource_pod(
        self,
        resource_type: str,
        namespace: str | None,
        resource_name: str,
        items_per_page: int = 10,
        page: int = 1,
        sort_by: str = "d,creationTimestamp",
    ) -> Raw:
        kind = resource_kind(resource_type)
        if kind not in WORKLOADS | {"service", "node", "job", "cronjob"}:
            raise ValueError("该资源不支持关联 Pod 查询")
        self._page("pod", [], page, items_per_page)
        parent = self._read(kind, namespace, resource_name)
        selector = (parent.get("spec") or {}).get("selector") or {}
        label_selector = selector_text(selector, service=kind == "service")
        # 无 selector 的 Service 不能退化为查询整个命名空间。
        if kind == "node":
            pods = self._list(
                "pod", field_selector=f"spec.nodeName={resource_name}"
            )
        elif kind == "cronjob":
            job_uids = {
                metadata(job)["uid"]
                for job in self._list("job", namespace)
                if any(
                    ref.get("uid") == metadata(parent)["uid"]
                    for ref in metadata(job)["ownerReferences"]
                )
            }
            pods = (
                [
                    pod
                    for pod in self._list("pod", namespace)
                    if any(
                        ref.get("uid") in job_uids
                        for ref in metadata(pod)["ownerReferences"]
                    )
                ]
                if job_uids
                else []
            )
        else:
            pods = (
                self._list("pod", namespace, label_selector=label_selector)
                if label_selector
                else []
            )
        context = ReadContext(self)
        return self._pod_list(
            self._sort(pods, sort_by), context, namespace, page, items_per_page
        )

    @api_call
    def resource_manifest(
        self,
        resource_type: str,
        namespace: str | None,
        name: str,
        reveal_secret: bool = False,
    ) -> Raw:
        """返回只读原始对象；Secret 默认隐藏值及可能携带值的注解。"""
        kind = resource_kind(resource_type)
        raw = self._read(kind, namespace, name)
        raw.get("metadata", {}).pop("managedFields", None)
        redacted = kind == "secret" and not reveal_secret
        if redacted:
            for field in ("data", "stringData"):
                if field in raw:
                    raw[field] = {key: "<已隐藏>" for key in raw[field] or {}}
            if raw.get("metadata", {}).get("annotations"):
                raw["metadata"]["annotations"] = {
                    key: "<已隐藏>" for key in raw["metadata"]["annotations"]
                }
        return {
            "manifest": raw,
            "yaml": yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
            "redacted": redacted,
        }

    @api_call
    def resource_events(
        self,
        resource_type: str,
        namespace: str | None,
        name: str,
        page: int = 1,
        items_per_page: int = 10,
    ) -> Raw:
        kind = resource_kind(resource_type)
        raw = self._read(kind, namespace, name)
        context = ReadContext(self)
        result = self._events(raw, context)
        rows = sorted(
            result["events"],
            key=lambda row: row.get("lastTimestamp") or "",
            reverse=True,
        )
        result.update(self._page("event", rows, page, items_per_page))
        result["errors"] = context.errors
        return result

    @api_call
    def pod_logs(
        self,
        namespace: str,
        name: str,
        container: str | None = None,
        tail_lines: int = 200,
        since_seconds: int | None = None,
        previous: bool = False,
        timestamps: bool = True,
    ) -> Raw:
        if not 1 <= tail_lines <= 2000:
            raise ValueError("日志行数应在 1–2000 之间")
        if since_seconds is not None and not 1 <= since_seconds <= 604800:
            raise ValueError("日志时间范围应在 1 秒至 7 天之间")
        raw = self._read("pod", namespace, name)
        spec = raw.get("spec") or {}
        regular = spec.get("containers") or []
        containers = (
            regular
            + (spec.get("initContainers") or [])
            + (spec.get("ephemeralContainers") or [])
        )
        container = container or (regular[0]["name"] if regular else None)
        if not container or container not in {c["name"] for c in containers}:
            raise ValueError("指定容器不属于该 Pod")
        limit = 1024 * 1024
        kwargs: Raw = {
            "name": name,
            "namespace": namespace,
            "container": container,
            "tail_lines": tail_lines,
            "previous": previous,
            "timestamps": timestamps,
            "follow": False,
            "limit_bytes": limit,
            "_request_timeout": TIMEOUT,
        }
        if since_seconds is not None:
            kwargs["since_seconds"] = since_seconds
        text = self.core.read_namespaced_pod_log(**kwargs)
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        return {
            "pod": name,
            "namespace": namespace,
            "container": container,
            "logs": text or "",
            "previous": previous,
            "limitBytes": limit,
            "truncated": len((text or "").encode("utf-8")) >= limit,
        }
