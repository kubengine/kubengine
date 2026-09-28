"""
鉴权认证模块

提供 JWT Token 鉴权、自动刷新和统一响应格式。
"""

import asyncio
import bcrypt
import hashlib
import hmac
from datetime import datetime, timedelta
from functools import wraps
from inspect import signature
from typing import Any, Callable, Optional
from typing import Dict, Tuple, TypeVar, Union
from uuid import uuid4

from core.config import Application
from core.auth_credentials import load_auth_users, load_signing_secret
from core.orm.auth import is_token_revoked, issue_session, session_active
from starlette.concurrency import run_in_threadpool
from web.utils.response import StandardResponse
from fastapi import Header, HTTPException, Request, status
from jwt import JWT, jwk_from_dict
from jwt.utils import get_int_from_datetime
from pydantic import BaseModel

# ============================ 配置常量 ============================

# JWT 算法配置
ALGORITHM: str = Application.AUTH.ALGORITHM

# Token 过期时间配置
ACCESS_TOKEN_EXPIRE_MINUTES: int = Application.AUTH.TOKEN_EXPIRE_MINUTES
TOKEN_RENEW_THRESHOLD_MINUTES: int = Application.AUTH.TOKEN_RENEW_THRESHOLD_MINUTES

# A separate, private key is shared by all API workers. Old CA-signed tokens
# intentionally stop working after this security update.
_app_secret_key = load_signing_secret()
signing_key = jwk_from_dict({"kty": "oct", "k": _app_secret_key})


def credential_version(record: Dict[str, Any]) -> str:
    """Invalidate sessions when password or API credentials are rotated."""
    import json
    material = json.dumps({key: record.get(key, "") for key in
                           ("password_hash", "ak", "sk_hash")}, sort_keys=True)
    return hmac.new(_app_secret_key.encode(), material.encode(), hashlib.sha256).hexdigest()


# JWT 实例
jwt_instance = JWT()


# 泛型类型变量
F = TypeVar("F", bound=Callable[..., Any])


# ============================ 数据模型 ============================


class LoginRequest(BaseModel):
    """登录请求模型"""

    username: str
    password: str


class User(BaseModel):
    """用户模型"""

    username: str
    ak: str
    ak_sk_expire_at: Optional[datetime] = None
    password_hash: str


class TokenResponse(BaseModel):
    """Token 响应模型"""

    name: str
    access_token: str
    token_type: str = "Bearer"
    expires_at: datetime
    renewed: bool = False


# ============================ 密码工具函数 ============================


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    验证密码

    Args:
        plain_password: 明文密码
        hashed_password: 哈希密码

    Returns:
        密码是否匹配
    """
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def get_password_hash(password: str) -> str:
    """
    生成密码哈希

    Args:
        password: 明文密码

    Returns:
        哈希后的密码字符串
    """
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


# ============================ Token 工具函数 ============================


def create_access_token(
    data: Dict[str, Any],
    expires_delta: Optional[timedelta] = None,
) -> Tuple[str, datetime]:
    """
    生成 JWT 访问令牌

    Args:
        data: 要编码到 token 中的数据
        expires_delta: 过期时间增量

    Returns:
        (access_token, expire_time) 元组
    """
    to_encode = data.copy()
    record = load_auth_users().get(str(data.get("sub", "")))
    if not record:
        raise HTTPException(status_code=401, detail="未知用户")
    current_version = credential_version(record)
    authenticated_version = data.get("credential_version", current_version)
    if not isinstance(authenticated_version, str) or not hmac.compare_digest(
        authenticated_version, current_version
    ):
        raise HTTPException(status_code=401, detail="凭据已变更，请重新登录")
    # Keep the version actually authenticated. A concurrent rotation after this
    # check must invalidate this token, rather than upgrade the old session.
    to_encode["credential_version"] = authenticated_version
    expire = datetime.now() + (expires_delta or timedelta(minutes=15))
    to_encode.update(
        {"exp": get_int_from_datetime(expire), "jti": str(uuid4())}
    )
    session_id = data.get("sid") or uuid4().hex
    try:
        issue_session(session_id, str(data["sub"]), authenticated_version,
                      to_encode["exp"], renew=bool(data.get("sid")))
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="登录会话已失效，请重新登录") from exc
    to_encode["sid"] = session_id
    encoded_jwt = jwt_instance.encode(to_encode, signing_key, alg=ALGORITHM)
    return encoded_jwt, expire


def is_token_blacklisted(token: str) -> bool:
    """
    检查 Token 是否在黑名单中

    Args:
        token: JWT Token 字符串

    Returns:
        是否在黑名单中
    """
    return is_token_revoked(token)


async def get_current_user(
    authorization: Optional[str] = Header(None),
    ak: Optional[str] = Header(None),
    timestamp: Optional[str] = Header(None),
    nonce: Optional[str] = Header(None),
    signature: Optional[str] = Header(None),
) -> Tuple[User, str]:
    """Authenticate a bearer token; never fall back to a default API secret.

    The legacy AK/SK scheme cannot verify HMAC using its stored bcrypt hash.
    Until a versioned, request-bound protocol is available it fails closed.
    """
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="请使用 Bearer Token；旧 AK/SK 签名认证已停用",
                            headers={"WWW-Authenticate": "Bearer"})
    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt_instance.decode(token, signing_key, algorithms={ALGORITHM})
        username = payload.get("sub", "")
        record = load_auth_users().get(username)
        if (not token or not record or not payload.get("exp")
                or not hmac.compare_digest(str(payload.get("credential_version", "")), credential_version(record))):
            raise ValueError("Invalid session")
        if is_token_blacklisted(token):
            raise ValueError("Revoked session")
        if not session_active(str(payload.get("sid", "")), username, payload["credential_version"]):
            raise ValueError("Expired or revoked login session")
        return User(username=username, **record), "token"
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Token 无效或已失效，请重新登录",
                            headers={"WWW-Authenticate": "Bearer"}) from exc


# ============================ 响应转换 ============================


def convert_to_standard(
    raw_response: Union[tuple[Any, ...], Dict[str, Any], object, Any],
    default_code: int = 200,
    default_message: str = "操作成功",
) -> StandardResponse[Any]:
    """
    将任意返回值转换为标准响应模型

    转换规则：
        - 元组 (code, message, data) → 完整映射
        - StandardResponse 对象 → 直接返回
        - 其他对象 → 作为 data 字段，使用默认 code 和 message

    Args:
        raw_response: 原始返回值
        default_code: 默认业务状态码
        default_message: 默认提示信息

    Returns:
        标准响应对象
    """
    if isinstance(raw_response, tuple) and len(raw_response) >= 3:  # type: ignore
        return StandardResponse(
            code=raw_response[0],  # type: ignore
            message=raw_response[1],  # type: ignore
            data=raw_response[2],  # type: ignore
        )
    if isinstance(raw_response, StandardResponse):
        return raw_response  # type: ignore
    return StandardResponse(
        code=default_code,
        message=default_message,
        data=raw_response,  # type: ignore
    )


# ============================ 鉴权装饰器 ============================


def auth_with_renew(
    renew_threshold: int = TOKEN_RENEW_THRESHOLD_MINUTES,
) -> Callable[[F], F]:
    """
    鉴权装饰器：Bearer Token 鉴权、自动刷新和统一响应

    Args:
        renew_threshold: Token 刷新阈值（分钟），小于此值时自动刷新

    Returns:
        装饰器函数

    Example:
        ```python
        @router.get("/protected")
        @auth_with_renew()
        async def protected_route(request: Request, current_user: User):
            return {"message": "Hello"}
        ```
    """

    def decorator(func: F) -> F:
        endpoint_signature = signature(func)
        public_signature = endpoint_signature.replace(return_annotation=StandardResponse[Any], parameters=[
            parameter for name, parameter in endpoint_signature.parameters.items()
            if name != "current_user"
        ])

        @wraps(func)
        async def wrapper(request: Request, *args: Any, **kwargs: Any) -> StandardResponse[Any]:
            headers = request.headers
            current_user, auth_type = await get_current_user(
                authorization=headers.get("authorization"),
                ak=headers.get("ak"),
                timestamp=headers.get("timestamp"),
                nonce=headers.get("nonce"),
                signature=headers.get("signature"),
            )

            token: Optional[str] = None
            token_expire: Optional[datetime] = None
            new_token: Optional[str] = None

            # 仅 Token 鉴权时处理刷新逻辑
            if auth_type == "token":
                auth_header = headers.get("authorization")
                if auth_header and auth_header.startswith("Bearer "):
                    token = auth_header.split(" ", 1)[1]
                    try:
                        payload = jwt_instance.decode(
                            token,
                            signing_key,
                            algorithms={ALGORITHM},
                        )
                        token_expire = datetime.fromtimestamp(
                            payload.get("exp", 0))
                    except Exception:
                        raise HTTPException(
                            status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="无效的 Token，无法解析",
                            headers={"WWW-Authenticate": "Bearer"},
                        )

                # Token 刷新逻辑
                if token_expire:
                    remaining_seconds = (
                        token_expire - datetime.now()).total_seconds()
                    remaining_minutes = remaining_seconds / 60

                    if remaining_seconds <= 0:
                        raise HTTPException(
                            status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Token 已过期，请重新登录",
                        )

                    # 触发 Token 刷新
                    if remaining_minutes < renew_threshold:
                        new_token, _ = create_access_token(
                            data={"sub": current_user.username,
                                  "sid": payload["sid"],
                                  "credential_version": payload["credential_version"]},
                            expires_delta=timedelta(
                                minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
                        )

            # Bind only business parameters; identity always comes from authentication.
            bound = public_signature.bind(request, *args, **kwargs)
            func_kwargs = dict(bound.arguments)
            if "current_user" in endpoint_signature.parameters:
                func_kwargs["current_user"] = current_user
            if asyncio.iscoroutinefunction(func):
                res = await func(**func_kwargs)
            else:
                res = await run_in_threadpool(func, **func_kwargs)

            # 转换为标准响应
            response_data = convert_to_standard(res)

            # 添加新 Token（如果有）
            if new_token and session_active(payload["sid"], current_user.username, payload["credential_version"]):
                response_data.new_access_token = new_token  # type: ignore
                response_data.token_type = "Bearer"  # type: ignore

            return response_data

        wrapper.__signature__ = public_signature  # type: ignore[attr-defined]
        wrapper.__annotations__ = dict(func.__annotations__)
        wrapper.__annotations__.pop("current_user", None)
        wrapper.__annotations__["return"] = StandardResponse[Any]
        return wrapper  # type: ignore

    return decorator
