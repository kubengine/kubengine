"""
KubEngine FastAPI 应用主入口模块

提供 FastAPI 应用的初始化配置和路由管理，包括：
- 应用生命周期管理（启动/关闭）
- 路由注册和中间件配置
- 全局异常处理器
- 静态文件服务
- SPA（单页应用）路由支持
"""

import os
import platform
import sys
import threading
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.logger import get_logger
from core.orm.engine import Base, engine
from core.orm.task import recover_unfinished_tasks_async
from web.api.artifacts import router as artifacts_router
from web.api.app import router as apps_router
from web.api.auth_routes import router as auth_router
from web.api.health import router as health_router
from web.api.k8s import router as k8s_router
from web.api.ssh import router as ssh_router
from web.api.websocket import router as websocket_router
from web.utils.response import error_response

logger = get_logger(__name__)

# 静态文件目录
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


# ============================ 工具函数 ============================


def print_kubengine_welcome() -> None:
    """
    打印 KubEngine 服务启动欢迎信息

    显示应用版本、运行环境等信息。
    """
    welcome_info = f"""
  KubEngine FastAPI 服务启动成功
  版本信息： KubEngine v1.0.0 | FastAPI 0.104.1
  运行环境： {platform.system()} {platform.release()} | Python {sys.version.split()[0]}
  提示： KubEngine 专注于云原生容器平台管理，轻量高效！
    """

    print("=" * 80)
    print(welcome_info)
    print("=" * 80)
    logger.info("KubEngine 服务已启动")


# ============================ 应用生命周期 ============================


async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    应用生命周期管理

    处理应用启动和关闭时的初始化和清理工作。

    Args:
        app: FastAPI 应用实例

    Yields:
        None
    """
    # 启动逻辑
    print_kubengine_welcome()

    # 创建数据库表
    Base.metadata.create_all(bind=engine)
    logger.info("数据库表已创建")

    # 恢复未完成的任务
    # 启动独立线程执行任务恢复，daemon=True 表示线程随主线程退出而退出
    recover_thread = threading.Thread(
        target=recover_unfinished_tasks_async,
        daemon=True,
    )
    recover_thread.start()
    logger.info("任务恢复线程已启动")

    yield  # 分割线：启动完成，服务开始接收请求

    # 关闭逻辑（如需要可添加清理代码）
    logger.info("KubEngine 服务正在关闭...")


# ============================ 应用初始化 ============================


app = FastAPI(
    title="KubEngine",
    description="Kubernetes deployment and management API",
    version="1.0.0",
    lifespan=lifespan,  # type: ignore
)

# 挂载 API 路由
app.include_router(auth_router, prefix="/api/v1")
app.include_router(health_router, prefix="/api/v1")
app.include_router(ssh_router, prefix="/api/v1/ssh")
app.include_router(k8s_router, prefix="/api/v1/k8s")
app.include_router(apps_router, prefix="/api/v1/app")
app.include_router(artifacts_router, prefix="/api/v1/artifacts")
app.include_router(websocket_router, prefix="/api/v1")

logger.info("API 路由已注册")


# ============================ 中间件配置 ============================


# 跨域配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
logger.info("CORS 中间件已配置")

# 静态文件目录配置
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    logger.info(f"静态文件目录已挂载: {STATIC_DIR}")
else:
    logger.warning(f"静态文件目录不存在: {STATIC_DIR}")


# ============================ 异常处理器 ============================


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
):
    """
    处理 Pydantic 参数校验异常

    当请求参数不符合 Pydantic 模型定义时触发（如字段缺失、类型错误）。

    Args:
        request: FastAPI 请求对象
        exc: 参数校验异常

    Returns:
        标准错误响应
    """
    errors = exc.errors()
    logger.warning(f"参数校验失败: {errors}")
    return error_response(
        message=f"参数校验失败: {errors}",
        code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
):
    """
    处理 FastAPI/Starlette HTTP 异常

    当发生 HTTP 异常时触发（如 404、401、403 等）。

    Args:
        request: FastAPI 请求对象
        exc: HTTP 异常

    Returns:
        标准错误响应
    """
    logger.warning(f"HTTP 异常: {exc.status_code} - {exc.detail}")
    return error_response(
        message=exc.detail or "请求异常",
        code=exc.status_code,
        status_code=exc.status_code,
    )


@app.exception_handler(HTTPException)
async def business_exception_handler(
    request: Request,
    exc: HTTPException,
):
    """
    处理自定义业务异常

    当业务代码抛出 HTTPException 时触发。

    Args:
        request: FastAPI 请求对象
        exc: 业务异常

    Returns:
        标准错误响应
    """
    logger.warning(f"业务异常: {exc.status_code} - {exc.detail}")
    return error_response(
        message=exc.detail,
        code=exc.status_code,
        status_code=exc.status_code,
    )


@app.exception_handler(Exception)
async def general_exception_handler(
    request: Request,
    exc: Exception,
):
    """
    处理通用异常

    捕获所有未处理的异常，避免服务器错误信息泄露。

    Args:
        request: FastAPI 请求对象
        exc: 异常对象

    Returns:
        标准错误响应
    """
    logger.error(f"未捕获的异常: {type(exc).__name__} - {str(exc)}")
    return error_response(
        message="服务器内部错误",
        code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


# ============================ 路由处理器 ============================


@app.get("/")
async def serve_index():
    """
    服务首页

    返回应用的首页 HTML 文件。

    Returns:
        index.html 文件响应
    """
    index_path = os.path.join(STATIC_DIR, "index.html")
    return FileResponse(index_path)


@app.get("/{full_path:path}")
async def spa_fallback(full_path: str):
    """
    SPA 路由回退处理

    匹配所有未定义的路由，返回 index.html，让前端路由处理。
    支持 React、Vue 等单页应用的前端路由。

    Args:
        full_path: 路径参数

    Returns:
        静态文件响应或 404 错误
    """
    # 排除 API 路径（避免 API 被前端路由接管）
    if full_path.startswith("api/"):
        logger.debug(f"API 路径未找到: {full_path}")
        return error_response(
            message="Not Found",
            code=404,
            status_code=status.HTTP_404_NOT_FOUND,
        )

    # 判断是否是静态文件请求（包含文件扩展名）
    if "." in full_path:
        file_path = os.path.join(STATIC_DIR, full_path)
        if os.path.exists(file_path):
            return FileResponse(file_path)

    # 返回 index.html，让前端路由处理
    index_path = os.path.join(STATIC_DIR, "index.html")
    return FileResponse(index_path)
