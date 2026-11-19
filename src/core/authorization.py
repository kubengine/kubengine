"""控制台统一动作、资源范围及持久化任务授权。未匹配规则默认拒绝。"""

import re
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

from fastapi import HTTPException

actor_context = ContextVar("console_actor", default=None)
operation_context = ContextVar("console_operation", default=None)
ORDINARY = frozenset(
    {
        "pod",
        "deployment",
        "statefulset",
        "daemonset",
        "replicaset",
        "job",
        "cronjob",
        "service",
        "endpoints",
        "ingress",
        "networkpolicy",
        "configmap",
        "secret",
        "persistentvolumeclaim",
        "horizontalpodautoscaler",
        "event",
    }
)
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
KNOWN = ORDINARY | {
    "node",
    "namespace",
    "persistentvolume",
    "storageclass",
    "ingressclass",
    "role",
    "rolebinding",
    "clusterrole",
    "clusterrolebinding",
    "customresourcedefinition",
    "serviceaccount",
}
for _kind in KNOWN:
    ALIASES[_kind + ("es" if _kind.endswith("s") else "s")] = _kind
ALIASES["endpoints"] = "endpoints"
DNS = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")


def value(user, key, default=None):
    return (
        user.get(key, default)
        if isinstance(user, dict)
        else getattr(user, key, default)
    )


def kind_name(kind):
    name = str(kind).lower()
    return ALIASES.get(name, name)


def is_admin(user):
    return value(user, "role") == "platform_admin"


def namespace_name(namespace):
    if namespace in (None, "", "_all"):
        return None
    if namespace == "_cluster":
        return "_cluster"
    if not isinstance(namespace, str) or not DNS.fullmatch(namespace):
        raise HTTPException(403, "命名空间未获授权")
    return namespace


def authorize(user, action, kind=None, namespace=None, *, namespaced=None):
    if is_admin(user):
        return
    role = value(user, "role")
    if role not in {"namespace_operator", "readonly"}:
        raise HTTPException(403, "权限不足")
    if action in {"profile.read", "password.change", "session.logout"}:
        return
    canonical = kind_name(kind)
    ns = namespace_name(namespace)
    if (
        canonical not in ORDINARY
        or namespaced is False
        or ns not in value(user, "namespaces", [])
    ):
        raise HTTPException(403, "资源或命名空间未获授权")
    if action == "resource.read":
        return
    if (
        action in {"resource.create", "resource.update"}
        and role == "namespace_operator"
        and canonical != "event"
    ):
        if canonical == "secret":
            authorize(user, "secret.read", canonical, ns)
        return
    if action in {
        "secret.read",
        "pod.terminal",
        "pod.files.read",
        "pod.files.write",
    }:
        if action in value(user, "sensitive_permissions", []):
            return
    raise HTTPException(403, "此操作未获授权")


def resource_scope(user, kind, namespace=None):
    """None 表示管理员全集；普通用户返回显式 namespace 集合。"""
    ns = namespace_name(namespace)
    if is_admin(user):
        return None
    canonical = kind_name(kind)
    if canonical == "namespace":
        if ns not in (None, "_cluster"):
            authorize(user, "resource.read", "pod", ns)
        elif ns == "_cluster":
            raise HTTPException(403, "不允许集群级访问")
    elif canonical not in ORDINARY or ns == "_cluster":
        raise HTTPException(403, "不允许访问此资源类型")
    if ns:
        authorize(
            user,
            "resource.read",
            "pod" if canonical == "namespace" else canonical,
            ns,
        )
        return [ns]
    return sorted(set(value(user, "namespaces", [])))


def authorizer(user):
    def check(
        action,
        kind,
        namespace,
        namespaced=True,
        api_version=None,
        manifest=None,
    ):
        authorize(user, action, kind, namespace, namespaced=namespaced)
        if not is_admin(user) and api_version:
            expected = {
                "deployment": "apps/v1",
                "statefulset": "apps/v1",
                "daemonset": "apps/v1",
                "replicaset": "apps/v1",
                "job": "batch/v1",
                "cronjob": "batch/v1",
                "ingress": "networking.k8s.io/v1",
                "networkpolicy": "networking.k8s.io/v1",
                "horizontalpodautoscaler": "autoscaling/v2",
            }.get(kind_name(kind), "v1")
            if api_version != expected:
                raise HTTPException(403, "此 API 组或版本未获授权")
        if not is_admin(user) and manifest is not None:
            validate_workload(user, namespace, manifest)

    return check


def validate_workload(user, namespace, manifest):
    """应用侧纵深校验；仍需管理员配置集群准入，不能替代隔离。"""
    spec = manifest.get("spec") or {}
    kind = kind_name(manifest.get("kind"))
    if kind == "cronjob":
        spec = (spec.get("jobTemplate") or {}).get("spec") or {}
        kind = "job"
    if kind in {"deployment", "statefulset", "daemonset", "replicaset", "job"}:
        spec = (spec.get("template") or {}).get("spec") or {}
    elif kind != "pod":
        return
    if any(spec.get(field) for field in ("hostNetwork", "hostPID", "hostIPC")):
        raise HTTPException(403, "普通用户不能启用宿主机命名空间")
    if spec.get("automountServiceAccountToken") is not False:
        raise HTTPException(
            403, "工作负载须显式关闭 ServiceAccount Token 自动挂载"
        )
    from core.config import ConfigDict

    allowed = (
        ConfigDict.get_instance()
        .get("authorization", {})
        .get("service_accounts", {})
        .get(namespace, ["default"])
    )
    if spec.get("serviceAccountName", "default") not in allowed:
        raise HTTPException(403, "此 ServiceAccount 未获管理员许可")
    security = spec.get("securityContext") or {}
    containers = [
        *spec.get("containers", []),
        *spec.get("initContainers", []),
        *spec.get("ephemeralContainers", []),
    ]
    secret_ref = False
    for volume in spec.get("volumes") or []:
        if "hostPath" in volume:
            raise HTTPException(403, "普通用户不能挂载 hostPath")
        secret_ref |= "secret" in volume or any(
            "secret" in source
            for source in (volume.get("projected") or {}).get("sources", [])
        )
    for container in containers:
        context = container.get("securityContext") or {}
        if (
            context.get("privileged")
            or context.get("allowPrivilegeEscalation") is not False
            or context.get("runAsUser") == 0
            or (
                context.get("runAsNonRoot", security.get("runAsNonRoot"))
                is not True
            )
            or (context.get("capabilities") or {}).get("add")
        ):
            raise HTTPException(
                403,
                "普通用户工作负载须禁用提权、以非 root 运行且不得增加 capabilities",
            )
        if any(port.get("hostPort") for port in container.get("ports", [])):
            raise HTTPException(403, "普通用户不能使用 hostPort")
        secret_ref |= any(
            "secretKeyRef" in (env.get("valueFrom") or {})
            for env in container.get("env", [])
        ) or any("secretRef" in env for env in container.get("envFrom", []))
    if secret_ref:
        authorize(user, "secret.read", "secret", namespace)


@contextmanager
def bind_actor(user, operation=None):
    actor_token = actor_context.set(user)
    operation_token = operation_context.set(operation)
    try:
        yield
    finally:
        operation_context.reset(operation_token)
        actor_context.reset(actor_token)


def snapshot():
    user = actor_context.get()
    operation = operation_context.get() or {}
    if user is None:
        # 仅本机 Python/CLI 调用可走此入口；HTTP 必须在鉴权上下文中调用。
        return {
            "service_actor": "local-cli",
            "request_id": operation.get("request_id", uuid4().hex),
        }
    return {
        "actor_id": value(user, "user_id"),
        "username": value(user, "username"),
        "auth_revision": value(user, "auth_revision"),
        "request_id": operation.get("request_id"),
        "audit_id": operation.get("audit_id"),
    }


def check_task(context, db=None):
    """任务开始和每个外部步骤前复核，旧任务缺身份不得自动接管。"""
    if not isinstance(context, dict):
        raise HTTPException(403, "旧任务缺少发起者，请由管理员重新提交或接管")
    if context.get("service_actor") == "local-cli":
        return
    from core.orm.engine import get_db
    from core.orm.identity import ConsoleUser

    if db is None:
        with get_db() as session:
            return check_task(context, session)
    user = db.get(ConsoleUser, context.get("actor_id"))
    if (
        not user
        or not user.enabled
        or user.auth_revision != context.get("auth_revision")
        or user.role != "platform_admin"
    ):
        raise HTTPException(
            403, "任务发起者已停用、凭据变更或权限已撤销；已完成步骤保留"
        )
