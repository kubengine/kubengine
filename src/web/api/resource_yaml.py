"""资源 YAML 校验、更新及版本查询。"""

import json

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.routing import APIRoute
from kubernetes.client.exceptions import ApiException
from pydantic import BaseModel, Field
from urllib3.exceptions import HTTPError

from core.services.resource_create import ResourceCreator
from core.services.resource_yaml import ResourceYaml
from web.api.k8s import get_kubernetes_client
from web.utils import auth
from web.utils.auth import User, auth_with_renew


class YamlRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request):
            if request.method == "POST":
                await auth.get_current_user(
                    authorization=request.headers.get("authorization")
                )
                chunks, size = [], 0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > 8 * 1024**2:
                        raise HTTPException(413, "YAML 请求过大")
                    chunks.append(chunk)
                request._body = b"".join(chunks)
            return await handler(request)

        return bounded


router = APIRouter(tags=["Kubernetes YAML 与版本"], route_class=YamlRoute)


class YamlChange(BaseModel):
    yaml_text: str = Field(min_length=1, max_length=2 * 1024**2)
    uid: str = Field(min_length=1, max_length=255)
    resource_version: str = Field(min_length=1, max_length=255)
    comment: str = Field(default="", max_length=500)
    reveal_secret: bool = False


class YamlApply(YamlChange):
    request_id: str = Field(
        min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$"
    )
    preview_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class YamlCreatePreview(BaseModel):
    yaml_text: str = Field(min_length=1, max_length=2 * 1024**2)
    namespace: str = Field(default="default", min_length=1, max_length=63)
    comment: str = Field(default="", max_length=500)


class YamlCreate(YamlCreatePreview):
    request_id: str = Field(
        min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$"
    )
    preview_token: str = Field(pattern=r"^\d{10}\.[0-9a-f]{64}$")


@router.post("/resources/preview", summary="预检 YAML 创建资源")
@auth_with_renew()
def preview_create(
    request: Request, body: YamlCreatePreview, current_user: User
):
    return execute(
        "batch",
        lambda: ResourceCreator(
            get_kubernetes_client(), current_user.username
        ).preview(body),
    )


@router.post("/resources/create", summary="通过 YAML 批量创建资源")
@auth_with_renew()
def create_resources(request: Request, body: YamlCreate, current_user: User):
    return execute(
        "batch",
        lambda: ResourceCreator(
            get_kubernetes_client(), current_user.username, seconds=90
        ).create(body),
    )


def execute(kind, action):
    secret = kind.lower() in {"secret", "tls"}
    try:
        return 200, "success", action()
    except ApiException as exc:
        message = exc.reason or "Kubernetes 请求失败"
        try:
            status = json.loads(exc.body or "{}")
            if secret:
                fields = [
                    cause.get("field", "")
                    for cause in (status.get("details") or {}).get("causes")
                    or []
                ]
                message = "Secret 操作被集群拒绝" + (
                    "，字段：" + "、".join(fields) if fields else ""
                )
            else:
                message = status.get("message") or message
        except (ValueError, TypeError, AttributeError):
            if secret:
                message = "Secret 操作被集群拒绝"
        return exc.status or 502, str(message)[:2000], {}
    except (HTTPError, OSError):
        return (
            503,
            "Kubernetes 连接失败；若已提交修改，请先查看资源和版本结果，勿盲目重试",
            {},
        )


def operations(kind, namespace, name):
    return ResourceYaml(get_kubernetes_client(), kind, namespace, name)


@router.get("/manifest/{kind}/{namespace}/{name}/edit")
@auth_with_renew()
def get_editor(
    request: Request,
    kind: str,
    namespace: str,
    name: str,
    reveal_secret: bool = Query(False),
):
    return execute(
        kind, lambda: operations(kind, namespace, name).editor(reveal_secret)
    )


@router.post("/manifest/{kind}/{namespace}/{name}/preview")
@auth_with_renew()
def preview(
    request: Request, kind: str, namespace: str, name: str, body: YamlChange
):
    return execute(
        kind, lambda: operations(kind, namespace, name).preview(body)
    )


@router.post("/manifest/{kind}/{namespace}/{name}/apply")
@auth_with_renew()
def apply(
    request: Request,
    kind: str,
    namespace: str,
    name: str,
    body: YamlApply,
    current_user: User,
):
    return execute(
        kind,
        lambda: operations(kind, namespace, name).apply(
            body, current_user.username
        ),
    )


@router.get("/manifest/{kind}/{namespace}/{name}/versions")
@auth_with_renew()
def versions(
    request: Request,
    kind: str,
    namespace: str,
    name: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    return execute(
        kind,
        lambda: operations(kind, namespace, name).versions(page, page_size),
    )


@router.get("/manifest/{kind}/{namespace}/{name}/versions/{version_id}")
@auth_with_renew()
def version(
    request: Request,
    kind: str,
    namespace: str,
    name: str,
    version_id: int,
    reveal_secret: bool = Query(False),
):
    return execute(
        kind,
        lambda: operations(kind, namespace, name).version(
            version_id, reveal_secret
        ),
    )
