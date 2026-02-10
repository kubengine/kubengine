"""健康检查接口
"""
from fastapi import APIRouter

from core.config.application import Application

router = APIRouter()


@router.get("/health")
async def health_check():
    """健康检查端点"""
    return {"status": "healthy", "service": Application.DOMAIN}
