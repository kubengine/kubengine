"""用户、内置角色与授权配置；身份字段不接受客户端覆盖。"""

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from core.orm import identity
from web.utils.auth import (
    User,
    auth_with_renew,
    get_password_hash,
    verify_password,
)

router = APIRouter(tags=["用户与授权"])
RoleName = Literal["platform_admin", "namespace_operator", "readonly"]
SensitivePermission = Literal[
    "secret.read", "pod.terminal", "pod.files.read", "pod.files.write"
]
Namespace = Annotated[
    str,
    Field(
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$",
    ),
]
Password = Annotated[str, Field(min_length=12, max_length=72)]


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Grants(StrictBody):
    role: RoleName = "readonly"
    namespaces: list[Namespace] = Field(default_factory=list, max_length=1000)
    sensitive_permissions: list[SensitivePermission] = Field(
        default_factory=list, max_length=4
    )


class PasswordBody(StrictBody):
    password: Password

    @field_validator("password")
    @classmethod
    def password_bytes(cls, value):
        if len(value.encode("utf-8")) > 72:
            raise ValueError("密码 UTF-8 编码不能超过 72 字节")
        return value


class CreateUser(Grants, PasswordBody):
    username: str = Field(
        min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$"
    )


class UpdateUser(Grants):
    enabled: bool
    revision: int = Field(ge=1)


class ChangePassword(PasswordBody):
    current_password: str = Field(min_length=1, max_length=1024)


async def execute(function, *args, **kwargs):
    try:
        return await run_in_threadpool(function, *args, **kwargs)
    except identity.IdentityError as exc:
        raise HTTPException(exc.code, str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(503, "身份存储不可用，请稍后重试") from exc


@router.get("/me", summary="当前用户与有效权限")
@auth_with_renew(admin_only=False)
async def me(request: Request, current_user: User):
    # 只返回明确的公开字段，避免密码哈希和凭据版本意外序列化。
    return {
        "user_id": current_user.user_id,
        "username": current_user.username,
        "role": current_user.role,
        "enabled": current_user.enabled,
        "namespaces": current_user.namespaces,
        "sensitive_permissions": current_user.sensitive_permissions,
        "revision": current_user.revision,
        "permissions": ["profile.read", "password.change"]
        + (
            [
                "platform.manage",
                "users.manage",
                "audit.read",
                "resource.read",
                "resource.create",
                "resource.update",
                "secret.read",
                "pod.terminal",
                "pod.files.read",
                "pod.files.write",
            ]
            if current_user.role == "platform_admin"
            else [
                "resource.read",
                *(
                    ["resource.create", "resource.update"]
                    if current_user.role == "namespace_operator"
                    else []
                ),
                *current_user.sensitive_permissions,
            ]
        ),
        "authorization_stage": "rbac",
        "resource_access_enabled": True,
    }


@router.post("/me/password", summary="修改自己的密码并撤销所有会话")
@auth_with_renew(renew_threshold=0, admin_only=False)
async def change_own_password(
    request: Request, body: ChangePassword, current_user: User
):
    if not await run_in_threadpool(
        verify_password, body.current_password, current_user.password_hash
    ):
        raise HTTPException(400, "当前密码错误")
    password_hash = await run_in_threadpool(get_password_hash, body.password)
    await execute(
        identity.change_password,
        current_user.user_id,
        password_hash,
        expected_hash=current_user.password_hash,
    )
    return {"reauthenticate": True}


@router.get("/users", summary="分页查询用户，不返回密码哈希")
@auth_with_renew()
async def users(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    return await execute(identity.list_users, page, page_size)


@router.get("/roles", summary="内置角色与敏感权限")
@auth_with_renew()
async def roles(request: Request):
    return {
        "roles": [
            {"name": name, "label": label}
            for name, label in identity.ROLES.items()
        ],
        "sensitive_permissions": sorted(identity.SENSITIVE_PERMISSIONS),
    }


@router.post("/users", summary="管理员创建用户")
@auth_with_renew()
async def create_user(request: Request, body: CreateUser, current_user: User):
    password_hash = await run_in_threadpool(get_password_hash, body.password)
    return await execute(
        identity.create_user,
        current_user.user_id,
        body.username,
        password_hash,
        body.role,
        body.namespaces,
        body.sensitive_permissions,
    )


@router.put("/users/{user_id}", summary="启停用户及配置角色和授权")
@auth_with_renew()
async def update_user(
    request: Request, user_id: str, body: UpdateUser, current_user: User
):
    return await execute(
        identity.update_user,
        current_user.user_id,
        user_id,
        body.revision,
        body.enabled,
        body.role,
        body.namespaces,
        body.sensitive_permissions,
    )


@router.post("/users/{user_id}/password", summary="管理员重置用户密码")
@auth_with_renew(renew_threshold=0)
async def reset_password(
    request: Request, user_id: str, body: PasswordBody, current_user: User
):
    password_hash = await run_in_threadpool(get_password_hash, body.password)
    await execute(
        identity.change_password,
        user_id,
        password_hash,
        actor_id=current_user.user_id,
    )
    return {"reauthenticate": user_id == current_user.user_id}
