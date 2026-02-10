from datetime import datetime, timedelta
from typing import Any
from fastapi import APIRouter, HTTPException, Request, status
from jwt import JWT

from web.utils.auth import (
    LoginRequest,
    TokenResponse,
    User,
    USERS,
    ACCESS_TOKEN_EXPIRE_MINUTES,
    signing_key,
    ALGORITHM,
    token_blacklist,
    auth_with_renew,
    verify_password,
    create_access_token,
)

router = APIRouter()
jwt = JWT()


@router.post("/login", response_model=TokenResponse, summary="用户登录（获取 JWT 令牌）")
async def login(form_data: LoginRequest):
    """用户登录接口，验证用户名密码后返回 JWT 访问令牌"""
    user_dict = USERS.get(form_data.username)
    if not user_dict or not verify_password(form_data.password, user_dict["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token, expires_at = create_access_token(
        data={"sub": form_data.username},
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
@auth_with_renew()
async def logout(request: Request, current_user: User):
    """登出：将当前 Token 加入黑名单（AKSK 鉴权无令牌，无需处理）"""
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        try:
            payload = jwt.decode(token, signing_key, algorithms={ALGORITHM})
            token_blacklist[token] = datetime.fromtimestamp(payload["exp"])
        except Exception:
            pass
    return {
        "status": "success",
        "detail": f"用户 {current_user.username} 已成功登出"
    }


@router.get("/protected/unified", summary="统一鉴权保护接口（Token/AKSK 二选一）")
@auth_with_renew()
async def unified_protected_route(request: Request, current_user: User) -> dict[str, Any]:
    """支持 Token 或 AKSK 鉴权，Token 鉴权自动刷新，AKSK 鉴权无刷新逻辑"""
    return {
        "message": f"欢迎访问统一鉴权接口，{current_user.username}！",
        "auth_info": {
            "ak": current_user.ak,
            "ak_sk_expire_at": current_user.ak_sk_expire_at.strftime("%Y-%m-%d %H:%M:%S") if current_user.ak_sk_expire_at else ""
        }
    }
