"""按 Deployment 资源范围授权的运维受理与进度接口。"""

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, StrictBool, StrictInt

from core.services.deployment_operations import DeploymentOperations
from core.services.deployment_releases import DeploymentReleases
from web.api.k8s import get_kubernetes_client
from web.api.resource_yaml import YamlRoute
from web.utils.auth import User, auth_with_renew

router = APIRouter(tags=["Deployment 运维"], route_class=YamlRoute)
BASE = "/deployments/{namespace}/{name}"


class OperationRequest(BaseModel):
    request_id: str = Field(
        min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$"
    )
    uid: str = Field(min_length=1, max_length=255)
    resource_version: str = Field(min_length=1, max_length=255)


class ScaleRequest(OperationRequest):
    replicas: StrictInt = Field(ge=0, le=2147483647)
    confirm_zero: StrictBool = False


class PreviewRequest(BaseModel):
    model_config = {"extra": "forbid"}
    uid: str = Field(min_length=1, max_length=255)
    resource_version: str = Field(min_length=1, max_length=255)


class ContainerImage(BaseModel):
    model_config = {"extra": "forbid"}
    name: str = Field(min_length=1, max_length=63)
    image: str = Field(min_length=1, max_length=512)


class ImagePreviewRequest(PreviewRequest):
    containers: list[ContainerImage] = Field(min_length=1, max_length=100)


class RollbackPreviewRequest(PreviewRequest):
    revision: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    replica_set_uid: str = Field(min_length=1, max_length=255)


class ImageRequest(ImagePreviewRequest):
    request_id: str = Field(
        min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$"
    )
    preview_token: str = Field(min_length=1, max_length=65536)


class RollbackRequest(RollbackPreviewRequest):
    request_id: str = Field(
        min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$"
    )
    preview_token: str = Field(min_length=1, max_length=65536)


def execute(namespace, name, current_user, callback):
    from kubernetes.client.exceptions import ApiException
    from sqlalchemy.exc import SQLAlchemyError
    from urllib3.exceptions import HTTPError

    try:
        return callback(
            DeploymentOperations(
                get_kubernetes_client(), namespace, name, current_user
            )
        )
    except ApiException as exc:
        return (
            exc.status or 502,
            "Kubernetes 拒绝 Deployment 运维请求，请检查集群权限和资源状态",
            {},
        )
    except (HTTPError, OSError):
        return (
            503,
            "集群连接不可用，请按原请求标识查看记录并核实，勿创建新请求重试",
            {},
        )
    except SQLAlchemyError:
        return (
            503,
            "操作存储暂不可用，请按原请求编号核实，勿创建新请求重试",
            {},
        )


@router.get(BASE + "/operations")
@auth_with_renew(permission="resource.read", resource_kind="deployment")
def operations(
    request: Request, namespace: str, name: str, current_user: User
):
    return execute(
        namespace,
        name,
        current_user,
        lambda ops: (200, "success", ops.records()),
    )


@router.get(BASE + "/operations/{operation_id}")
@auth_with_renew(permission="resource.read", resource_kind="deployment")
def operation(
    request: Request,
    namespace: str,
    name: str,
    operation_id: str,
    current_user: User,
):
    return execute(
        namespace,
        name,
        current_user,
        lambda ops: (200, "success", ops.get(operation_id)),
    )


@router.get(BASE + "/history")
@auth_with_renew(permission="resource.read", resource_kind="deployment")
def history(request: Request, namespace: str, name: str, current_user: User):
    return execute(
        namespace,
        name,
        current_user,
        lambda ops: (200, "success", DeploymentReleases(ops).history()),
    )


@router.post(BASE + "/image/preview")
@auth_with_renew(
    permission="resource.update",
    resource_kind="deployment",
    audit_action="deployment.image.preview",
)
def image_preview(
    request: Request,
    namespace: str,
    name: str,
    body: ImagePreviewRequest,
    current_user: User,
):
    return execute(
        namespace,
        name,
        current_user,
        lambda ops: (
            200,
            "预检通过，请核对差异后确认",
            DeploymentReleases(ops).preview("image", body),
        ),
    )


@router.post(BASE + "/rollback/preview")
@auth_with_renew(
    permission="resource.update",
    resource_kind="deployment",
    audit_action="deployment.rollback.preview",
)
def rollback_preview(
    request: Request,
    namespace: str,
    name: str,
    body: RollbackPreviewRequest,
    current_user: User,
):
    return execute(
        namespace,
        name,
        current_user,
        lambda ops: (
            200,
            "预检通过，请核对差异后确认",
            DeploymentReleases(ops).preview("rollback", body),
        ),
    )


@router.post(BASE + "/image", status_code=202)
@auth_with_renew(
    permission="resource.update",
    resource_kind="deployment",
    audit_action="deployment.image.submit",
)
def image(
    request: Request,
    namespace: str,
    name: str,
    body: ImageRequest,
    current_user: User,
):
    return execute(
        namespace,
        name,
        current_user,
        lambda ops: (
            202,
            "操作已受理，等待执行和健康观察",
            ops.submit("image", body),
        ),
    )


@router.post(BASE + "/rollback", status_code=202)
@auth_with_renew(
    permission="resource.update",
    resource_kind="deployment",
    audit_action="deployment.rollback.submit",
)
def rollback(
    request: Request,
    namespace: str,
    name: str,
    body: RollbackRequest,
    current_user: User,
):
    return execute(
        namespace,
        name,
        current_user,
        lambda ops: (
            202,
            "操作已受理，等待执行和健康观察",
            ops.submit("rollback", body),
        ),
    )


@router.post(BASE + "/scale", status_code=202)
@auth_with_renew(
    permission="resource.update",
    resource_kind="deployment",
    audit_action="deployment.scale.submit",
)
def scale(
    request: Request,
    namespace: str,
    name: str,
    body: ScaleRequest,
    current_user: User,
):
    return execute(
        namespace,
        name,
        current_user,
        lambda ops: (
            202,
            "操作已受理，等待执行和健康观察",
            ops.submit("scale", body),
        ),
    )


@router.post(BASE + "/restart", status_code=202)
@auth_with_renew(
    permission="resource.update",
    resource_kind="deployment",
    audit_action="deployment.restart.submit",
)
def restart(
    request: Request,
    namespace: str,
    name: str,
    body: OperationRequest,
    current_user: User,
):
    return execute(
        namespace,
        name,
        current_user,
        lambda ops: (
            202,
            "操作已受理，等待执行和健康观察",
            ops.submit("restart", body),
        ),
    )
