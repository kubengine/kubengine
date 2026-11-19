"""HTTP 权限与审计元信息映射，绝不把请求正文送入审计。"""

from core import authorization as policy
from core.logger import get_log_context
from core.orm import audit


def authorize(user, permission, arguments):
    kind = arguments.get("kind", arguments.get("type", "pod"))
    namespace = arguments.get("namespace")
    if permission == "resource.list":
        policy.resource_scope(user, kind, namespace)
    elif permission == "resource.create.batch":
        if (
            not policy.is_admin(user)
            and policy.value(user, "role") != "namespace_operator"
        ):
            from fastapi import HTTPException

            raise HTTPException(403, "没有创建资源的权限")
        if not policy.is_admin(user):
            from core.services.resource_create import parse_resources

            checker = policy.authorizer(user)
            body = arguments["body"]
            for item in parse_resources(body.yaml_text):
                namespace = item["metadata"].get("namespace", body.namespace)
                checker(
                    "resource.create",
                    item["kind"],
                    namespace,
                    api_version=item["apiVersion"],
                    manifest=item,
                )
    elif permission:
        policy.authorize(user, permission, kind, namespace)
    body = arguments.get("body")
    reveal = (
        arguments.get("reveal_secret") is True
        or getattr(body, "reveal_secret", False) is True
    )
    if policy.kind_name(kind) == "secret" and reveal:
        policy.authorize(user, "secret.read", kind, namespace)


def begin(request, user, action, arguments):
    body = arguments.get("body")
    target = arguments.get("command_req") or arguments.get("file_req")
    resource_name = arguments.get(
        "name", arguments.get("user_id", arguments.get("cluster_id", ""))
    )
    resource_type = arguments.get("kind", arguments.get("type", ""))
    if resource_type:
        resource_type = policy.kind_name(resource_type)
    if target is not None:
        resource_name, resource_type = getattr(target, "host", ""), "ssh-host"
    batch = (
        getattr(body, "request_id", "")
        if action == "resource.create.batch"
        else ""
    )
    event_id = audit.begin_event(
        actor_id=policy.value(user, "user_id", ""),
        username=policy.value(user, "username", ""),
        action=action,
        namespace=arguments.get("namespace", ""),
        resource_type=resource_type,
        resource_name=resource_name,
        source_ip=request.client.host if request.client else "",
        request_id=getattr(request.state, "request_id", None)
        or get_log_context().get("request_id", ""),
        batch_id=batch,
    )
    return {
        "audit_id": event_id,
        "request_id": getattr(request.state, "request_id", None)
        or get_log_context().get("request_id", event_id),
    }


def finish(event_id, response):
    if (
        isinstance(response, dict)
        and {"code", "message", "data"} <= response.keys()
    ):
        status, data = response["code"], response["data"]
    elif all(
        hasattr(response, field) for field in ("code", "message", "data")
    ):
        status, data = response.code, response.data
    elif isinstance(response, tuple) and len(response) >= 3:
        status, _, data = response[:3]
    elif hasattr(response, "status_code"):
        status, data = response.status_code, None
    else:
        status, data = 200, response
    # 已认证业务返回的 401 是上游认证失败，与 HTTP 响应包装保持一致。
    if status == 401:
        status = 502
    elif status in {204, 205}:
        status = 200
    elif not (200 <= status < 300 or 400 <= status <= 599):
        status = 400
    if hasattr(data, "model_dump"):
        data = data.model_dump()
    data = data if isinstance(data, dict) else {}
    revision = data.get("revision") or {}
    audit.finish_event(
        event_id,
        status,
        result="partial" if data.get("complete") is False else None,
        task_id=data.get("task_id", ""),
        revision_id=(
            revision.get("id", "") if isinstance(revision, dict) else ""
        ),
    )


async def preauthorize(request, permission=None):
    """在 multipart/YAML 读取前拒绝未授权请求，并对拒绝留痕。"""
    from fastapi import HTTPException

    from web.utils import auth

    user = None
    try:
        user, _ = await auth.get_current_user(
            authorization=request.headers.get("authorization")
        )
        if permission:
            policy.authorize(
                user, permission, "pod", request.path_params.get("namespace")
            )
        elif not policy.is_admin(user):
            raise HTTPException(403, "需要平台管理员权限")
        return user
    except HTTPException as exc:
        operation = begin(
            request, user, permission or "platform.upload", request.path_params
        )
        audit.finish_event(operation["audit_id"], exc.status_code)
        raise
