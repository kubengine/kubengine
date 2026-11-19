from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from jwt import JWT
from starlette.concurrency import run_in_threadpool

from core.orm.auth import revoke_session, revoke_token
from web.utils.auth import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    ALGORITHM,
    LoginRequest,
    TokenResponse,
    User,
    auth_with_renew,
    create_access_token,
    credential_version,
    load_identity,
    signing_key,
    verify_password,
)

router = APIRouter()
jwt = JWT()


@router.post(
    "/login", response_model=TokenResponse, summary="用户登录（获取 JWT 令牌）"
)
async def login(form_data: LoginRequest, request: Request = None):
    from core.logger import get_log_context
    from core.orm import audit

    user_dict = load_identity(form_data.username)
    event = audit.begin_event(
        actor_id=(user_dict or {}).get("user_id", ""),
        username=form_data.username,
        action="identity.login",
        source_ip=request.client.host if request and request.client else "",
        request_id=get_log_context().get("request_id", ""),
    )
    try:
        if (
            not user_dict
            or not user_dict.get("enabled", False)
            or not await run_in_threadpool(
                verify_password,
                form_data.password,
                user_dict.get("password_hash", ""),
            )
        ):
            raise HTTPException(
                401, "用户名或密码错误", headers={"WWW-Authenticate": "Bearer"}
            )
        access_token, expires_at = create_access_token(
            data={
                "sub": form_data.username,
                "credential_version": credential_version(user_dict),
            },
            expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        )
    except HTTPException as exc:
        audit.finish_event(event, exc.status_code)
        raise
    audit.finish_event(event, 200)
    return TokenResponse(
        name=form_data.username,
        access_token=access_token,
        token_type="Bearer",
        expires_at=expires_at,
        renewed=False,
    )


@router.post("/logout", summary="用户登出（失效令牌）")
@auth_with_renew(renew_threshold=0, admin_only=False)
async def logout(request: Request, current_user: User):
    """登出：持久化撤销整个登录会话及其续签令牌。"""
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        try:
            payload = jwt.decode(token, signing_key, algorithms={ALGORITHM})
            revoke_session(payload["sid"])
            revoke_token(token, int(payload["exp"]))
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail="令牌撤销失败，请重试"
            ) from exc
    return {
        "status": "success",
        "detail": f"用户 {current_user.username} 已成功登出",
    }


@router.get("/protected/unified", summary="Bearer Token 鉴权保护接口")
@auth_with_renew(admin_only=False)
async def unified_protected_route(
    request: Request, current_user: User
) -> dict[str, Any]:
    """Bearer Token 鉴权，临近过期时自动刷新。"""
    return {
        "message": f"欢迎访问统一鉴权接口，{current_user.username}！",
        "auth_info": {
            "ak": current_user.ak,
            "ak_sk_expire_at": (
                current_user.ak_sk_expire_at.strftime("%Y-%m-%d %H:%M:%S")
                if current_user.ak_sk_expire_at
                else ""
            ),
        },
    }
