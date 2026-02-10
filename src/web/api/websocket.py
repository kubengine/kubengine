"""
WebSocket 实时通信 API 路由模块

提供 WebSocket 连接和异步任务管理的 HTTP/WebSocket API 接口，支持：
- WebSocket 连接管理
- 异步任务创建和执行
- 任务状态跟踪和更新
- 实时消息推送
"""

import asyncio
from json import JSONDecodeError
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from core.logger import get_logger
from core.misc.websocket import connection_manager
from core.orm.task import TaskStatus
from web.utils.auth import get_current_user

logger = get_logger(__name__)

router = APIRouter(tags=["WebSocket 实时通信"])

# ============================ 全局状态管理 ============================

# 任务状态存储（task_id -> 状态信息）
task_statuses: dict[str, dict[str, Any]] = {}
# 异步锁：保证状态更新/读取的线程安全
status_lock = asyncio.Lock()


# ============================ Pydantic 模型 ============================


class ResourceCreateRequest(BaseModel):
    """创建资源的请求参数模型"""

    resource_name: str = Field(..., description="资源名称")
    resource_type: str = Field(..., description="资源类型（如 redis、mysql、cluster）")
    config: dict[str, Any] | None = Field(None, description="资源配置")


# ============================ 任务状态管理 ============================


async def update_task_status(task_id: str, status: TaskStatus, message: str) -> None:
    """
    更新任务状态到全局存储

    Args:
        task_id: 任务 ID
        status: 任务状态
        message: 状态消息
    """
    async with status_lock:
        task_statuses[task_id] = {
            "task_id": task_id,
            "status": status.value,
            "message": message,
            "timestamp": asyncio.get_event_loop().time(),
        }


async def get_task_status(task_id: str) -> dict[str, Any] | None:
    """
    从全局存储获取任务状态

    Args:
        task_id: 任务 ID

    Returns:
        任务状态信息，如果不存在则返回 None
    """
    async with status_lock:
        return task_statuses.get(task_id)


# ============================ 后台任务 ============================


async def create_resource_task(
    task_id: str,
    resource_name: str,
    resource_type: str,
    config: dict[str, Any] | None = None,
) -> None:
    """
    后台异步创建资源，仅更新全局状态，不主动推送

    该函数模拟资源创建的完整流程，包括：
    1. 配置校验
    2. 基础资源创建
    3. 资源初始化
    4. 完成确认

    Args:
        task_id: 任务 ID
        resource_name: 资源名称
        resource_type: 资源类型
        config: 资源配置
    """
    # 初始化任务状态
    await update_task_status(task_id, TaskStatus.pending, "任务待执行")

    try:
        # 步骤1：推送「任务开始」状态
        await update_task_status(task_id, TaskStatus.running, "开始创建资源...")

        # 模拟步骤1：校验配置（耗时1秒）
        await asyncio.sleep(1)
        config_desc = config if config else "默认配置"
        await update_task_status(
            task_id,
            TaskStatus.running,
            f"校验 {resource_type} 配置：{config_desc}",
        )

        # 模拟步骤2：创建基础资源（耗时2秒）
        await asyncio.sleep(2)
        await update_task_status(
            task_id,
            TaskStatus.running,
            f"创建 {resource_name} 基础资源成功",
        )

        # 模拟步骤3：初始化资源（耗时1.5秒）
        await asyncio.sleep(1.5)
        await update_task_status(
            task_id,
            TaskStatus.running,
            f"初始化 {resource_name} 资源配置",
        )

        # 步骤4：任务完成
        await update_task_status(
            task_id,
            TaskStatus.success,
            f"{resource_name}（{resource_type}）创建成功！",
        )

    except Exception as e:
        # 异常时更新失败状态
        logger.error(f"创建资源失败: {e}")
        await update_task_status(
            task_id,
            TaskStatus.failed,
            f"创建资源失败：{str(e)}",
        )


# ============================ HTTP 路由 ============================


@router.post(
    "/create-resource",
    summary="创建资源",
    description="接收创建资源请求，返回 task_id，后台异步执行任务",
)
async def trigger_resource_create(
    request: ResourceCreateRequest,
    background_tasks: BackgroundTasks,
):
    """
    接收创建资源请求，返回 task_id，后台异步执行任务

    该接口会立即返回任务 ID，实际的资源创建过程在后台异步执行。
    客户端可以通过 WebSocket 查询任务执行状态。

    Args:
        request: 资源创建请求参数
        background_tasks: FastAPI 后台任务管理器

    Returns:
        包含任务 ID 的响应数据
    """
    # 生成唯一任务 ID
    task_id = str(uuid.uuid4())

    # 初始化任务状态
    await update_task_status(task_id, TaskStatus.pending, "任务已接收，等待执行")

    # 添加后台任务（FastAPI 自动异步执行）
    background_tasks.add_task(
        create_resource_task,
        task_id=task_id,
        resource_name=request.resource_name,
        resource_type=request.resource_type,
        config=request.config,
    )

    logger.info(f"创建资源任务已提交: {task_id}, 资源: {request.resource_name}")

    # 立即返回，不阻塞
    result: dict[str, Any] = {
        "code": 200,
        "msg": "请求已接收，后台开始创建资源",
        "data": {"task_id": task_id},
    }
    return result


# ============================ WebSocket 路由 ============================


@router.websocket(
    "/ws",
    name="websocket_endpoint",
)
async def websocket_endpoint(
    websocket: WebSocket,
    token: str | None = Query(None, description="认证令牌"),
):
    """
    WebSocket 端点

    提供实时双向通信功能，当前支持：
    - ping/pong 心跳检测
    - 任务状态查询（可扩展）

    连接建立流程：
    1. 接受 WebSocket 连接
    2. 验证用户身份（通过 token 参数）
    3. 将连接加入连接池
    4. 循环接收并处理客户端消息

    前端指令格式：
    - {"action": "ping"} - 心跳检测

    Args:
        websocket: WebSocket 连接对象
        token: 认证令牌（通过查询参数传递）
    """
    # 接受 WebSocket 连接
    await websocket.accept()
    logger.info("WebSocket 连接已建立")

    # 验证用户身份
    try:
        await get_current_user(
            authorization=token,
            ak="",
            timestamp="",
            nonce="",
            signature="",
        )
        logger.info("WebSocket 鉴权成功")
    except HTTPException as e:
        # 鉴权失败，拒绝连接
        logger.warning(f"WebSocket 鉴权失败: {e.detail}")
        await websocket.close(code=1008, reason=e.detail)
        return

    # 将连接加入连接池
    await connection_manager.connect(websocket)
    logger.info("WebSocket 连接已加入连接池")

    # 标记连接状态，避免重复操作
    is_connected = True

    try:
        # 循环接收客户端消息
        while is_connected:
            # 接收前端的指令
            data = await websocket.receive_text()
            logger.debug(f"收到 WebSocket 消息: {data}")

            try:
                import json

                # 使用 json.loads 替代 eval，更安全
                req = json.loads(data)

                # 处理 ping 指令
                if req.get("action") == "ping":
                    await websocket.send_json({"status": "ok"})
                    logger.debug("响应 ping 消息")

                else:
                    # 未知指令，返回提示
                    await websocket.send_json({
                        "status": "error",
                        "message": f"未知指令: {req.get('action')}",
                    })
                    logger.warning(f"收到未知指令: {req.get('action')}")

            except JSONDecodeError as e:
                # JSON 解析失败
                await websocket.send_json({
                    "status": "error",
                    "message": f"指令格式错误：{str(e)}",
                })
                logger.warning(f"JSON 解析失败: {e}")

            except Exception as e:
                # 其他处理错误
                await websocket.send_json({
                    "status": "error",
                    "message": f"处理指令失败：{str(e)}",
                })
                logger.error(f"处理 WebSocket 消息失败: {e}")

    except WebSocketDisconnect:
        logger.info("WebSocket 连接已断开（正常关闭）")
        await connection_manager.disconnect(websocket)

    except Exception as e:
        logger.error(f"WebSocket 异常：{e}")
        await connection_manager.disconnect(websocket)
