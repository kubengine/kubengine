"""
鉴权认证模块

提供 JWT Token 和 AK/SK 两种鉴权方式，支持 Token 自动刷新和统一响应格式。
"""

import asyncio
import base64
import bcrypt
import hashlib
import hmac
from datetime import datetime, timedelta
from functools import wraps
from inspect import signature
from pathlib import Path
from typing import Any, Callable, Optional
from typing import Dict, Tuple, TypeVar, Union
from uuid import uuid4

from core.config import Application
from core.misc.ca import create_cert
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

# AK/SK 过期时间（天）
AK_SK_EXPIRE_DAYS: int = 90

# 用户数据存储
USERS: Dict[str, Dict[str, Any]] = {
    "admin": {
        "password_hash": Application.AUTH.USERS_ADMIN_PASSWORD_HASH,
        "ak": Application.AUTH.USERS_ADMIN_AK,
        "sk_hash": Application.AUTH.USERS_ADMIN_SK_HASH,
    }
}

# JWT 签名密钥
_app_secret_key: str = "your-strong-secret-key-keep-it-safe-never-expose-in-production"
if not Path(Application.TLS_CONFIG.CA_KEY).exists():
    create_cert()
with open(Application.TLS_CONFIG.CA_KEY, "r", encoding="utf-8") as f:
    _app_secret_key = f.read().strip()

signing_key = jwk_from_dict({"kty": "oct", "k": _app_secret_key})

# JWT 实例
jwt_instance = JWT()

# Token 黑名单
token_blacklist: Dict[str, datetime] = {}

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
    return bcrypt.checkpw(
        plain_password.encode("utf-8"),
        hashed_password.encode("utf-8"),
    )


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
    expire = datetime.now() + (expires_delta or timedelta(minutes=15))
    to_encode.update(
        {"exp": get_int_from_datetime(expire), "jti": str(uuid4())}
    )
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
    if token not in token_blacklist:
        return False
    if token_blacklist[token] < datetime.now():
        del token_blacklist[token]
        return False
    return True


# ============================ AK/SK 工具函数 ============================


def verify_ak_sk_signature(
    ak: str,
    timestamp: str,
    nonce: str,
    signature: str,
    user: User,
) -> bool:
    """
    验证 AK/SK 签名

    Args:
        ak: Access Key
        timestamp: 时间戳字符串（格式：YYYYMMDDHHMMSS）
        nonce: 随机数
        signature: 签名
        user: 用户对象

    Returns:
        签名是否有效
    """
    # 验证时间戳（5分钟内有效）
    try:
        request_time = datetime.strptime(timestamp, "%Y%m%d%H%M%S")
        if abs((datetime.now() - request_time).total_seconds()) > 300:
            return False
    except ValueError:
        return False

    # 获取用户的 SK（实际应用中应从安全存储获取）
    user_data = USERS.get(user.username, {})
    original_sk = user_data.get("sk", "SK12345678")

    # 计算期望签名
    sign_string = f"{ak}{timestamp}{nonce}".encode("utf-8")
    hmac_obj = hmac.new(
        original_sk.encode("utf-8"),
        sign_string,
        hashlib.sha256,
    )
    expected_signature = base64.b64encode(hmac_obj.digest()).decode("utf-8")

    # 使用常量时间比较防止时序攻击
    return hmac.compare_digest(signature, expected_signature)


# ============================ 鉴权依赖 ============================


async def get_current_user(
    authorization: Optional[str] = Header(None),
    ak: Optional[str] = Header(None),
    timestamp: Optional[str] = Header(None),
    nonce: Optional[str] = Header(None),
    signature: Optional[str] = Header(None),
) -> Tuple[User, str]:
    """
    统一鉴权依赖：Token 或 AK/SK 二选一

    Args:
        authorization: Authorization 头（Bearer Token）
        ak: Access Key
        timestamp: 请求时间戳
        nonce: 随机数
        signature: 请求签名

    Returns:
        (当前用户, 鉴权类型) 元组，鉴权类型为 "token" 或 "aksk"

    Raises:
        HTTPException: 鉴权失败时抛出 401 错误
    """
    user: Optional[User] = None
    auth_type = ""

    # 验证 Authorization 头格式
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization 头格式错误，应为 'Bearer <token>'",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = authorization.split(" ", 1)[1]

    # 尝试 Token 鉴权
    if token:
        if is_token_blacklisted(token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="令牌已失效（已登出）",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            payload = jwt_instance.decode(
                token, signing_key, algorithms={ALGORITHM})
            username: str = payload.get("sub", "")
            if username and (user_dict := USERS.get(username)):
                user = User(username=username, **user_dict)
                auth_type = "token"
        except Exception:
            pass

    # Token 鉴权失败，尝试 AK/SK 鉴权
    if user is None and all([ak, timestamp, nonce, signature]):
        user_dict = next(
            (u for u in USERS.values() if u.get("ak") == ak),
            None,
        )
        if user_dict:
            user = User(**user_dict)
            # 类型断言：all() 已确保参数不为 None
            if verify_ak_sk_signature(
                ak,  # type: ignore
                timestamp,  # type: ignore
                nonce,  # type: ignore
                signature,  # type: ignore
                user,
            ):
                auth_type = "aksk"
            else:
                user = None

    # 两种鉴权都失败
    if user is None or not auth_type:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token 或 AK/SK 鉴权失败，请检查凭证",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user, auth_type


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
    鉴权装饰器：支持 Token/AKSK 二选一鉴权 + Token 自动刷新 + 统一响应

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
                            data={"sub": current_user.username},
                            expires_delta=timedelta(
                                minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
                        )

            # 执行原接口函数
            sig = signature(func)
            params = sig.parameters
            func_args: list[Any] = [request]
            func_kwargs = kwargs.copy()

            # 检测是否需要 current_user 参数
            if "current_user" in params:
                func_args.append(current_user)

            # 执行原函数
            if asyncio.iscoroutinefunction(func):
                res = await func(*func_args, *args, **func_kwargs)
            else:
                res = func(*func_args, *args, **func_kwargs)

            # 转换为标准响应
            response_data = convert_to_standard(res)

            # 添加新 Token（如果有）
            if new_token:
                response_data.new_access_token = new_token  # type: ignore
                response_data.token_type = "Bearer"  # type: ignore

            return response_data

        # 设置类型注解（运行时动态设置，类型检查器无法推断）
        wrapper.__annotations__[
            "return"] = StandardResponse[Any]  # type: ignore
        return wrapper  # type: ignore

    return decorator
