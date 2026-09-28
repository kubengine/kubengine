from datetime import datetime, timedelta
from typing import Any
from fastapi import APIRouter, HTTPException, Request, status
from jwt import JWT
from core.orm.auth import revoke_token
from starlette.concurrency import run_in_threadpool

from web.utils.auth import (
    LoginRequest,
    TokenResponse,
    User,
    load_auth_users,
    ACCESS_TOKEN_EXPIRE_MINUTES,
    signing_key,
    ALGORITHM,

    auth_with_renew,
    verify_password,
    create_access_token,
    credential_version,
)

router = APIRouter()
jwt = JWT()


@router.post("/login", response_model=TokenResponse, summary="用户登录（获取 JWT 令牌）")
async def login(form_data: LoginRequest):
    """用户登录接口，验证用户名密码后返回 JWT 访问令牌"""
    user_dict = load_auth_users().get(form_data.username)
    if not user_dict or not await run_in_threadpool(verify_password, form_data.password, user_dict.get("password_hash", "")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token, expires_at = create_access_token(
        data={"sub": form_data.username,
              "credential_version": credential_version(user_dict)},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    return TokenResponse(
        name=form_data.username,
        access_token=access_token,
        token_type="Bearer",
        expires_at=expires_at,
        renewed=False
    )


@router.post("/logout", summary="用户登出（失效令牌）")
@auth_with_renew(renew_threshold=0)
async def logout(request: Request, current_user: User):
    """登出：持久化撤销当前 Token，不签发续期令牌。"""
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        try:
            payload = jwt.decode(token, signing_key, algorithms={ALGORITHM})
            revoke_token(token, int(payload["exp"]))
        except Exception as exc:
            raise HTTPException(status_code=503, detail="令牌撤销失败，请重试") from exc
    return {
        "status": "success",
        "detail": f"用户 {current_user.username} 已成功登出"
    }


@router.get("/protected/unified", summary="Bearer Token 鉴权保护接口")
@auth_with_renew()
async def unified_protected_route(request: Request, current_user: User) -> dict[str, Any]:
    """Bearer Token 鉴权，临近过期时自动刷新。"""
    return {
        "message": f"欢迎访问统一鉴权接口，{current_user.username}！",
        "auth_info": {
            "ak": current_user.ak,
            "ak_sk_expire_at": current_user.ak_sk_expire_at.strftime("%Y-%m-%d %H:%M:%S") if current_user.ak_sk_expire_at else ""
        }
    }
