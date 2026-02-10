"""
SSH 远程操作 API 路由模块

提供通过 SSH 协议远程执行命令和传输文件的 HTTP API 接口，支持：
- 单主机命令执行
- 多主机批量命令执行
- 文件上传到远程主机
- 从远程主机下载文件
- 密码和密钥两种认证方式
"""

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.logger import get_logger
from core.ssh import AsyncSSHClient
from web.utils.auth import User, auth_with_renew

logger = get_logger(__name__)

router = APIRouter(tags=["SSH 远程操作"])


# ============================ Pydantic 模型 ============================


class CommandRequest(BaseModel):
    """单主机命令执行请求模型"""

    host: str = Field(..., description="目标主机地址")
    command: str = Field(..., description="要执行的命令")
    username: str = Field(..., description="SSH 登录用户名")
    password: str | None = Field(None, description="SSH 登录密码（与密钥二选一）")
    client_keys: list[str] | None = Field(None, description="SSH 私钥内容列表")


class MultiCommandRequest(BaseModel):
    """多主机命令执行请求模型"""

    commands: list[dict[str, str]] = Field(
        ...,
        description="命令列表，每个元素包含 host 和 command 字段",
    )
    username: str = Field(..., description="SSH 登录用户名")
    password: str | None = Field(None, description="SSH 登录密码（与密钥二选一）")
    client_keys: list[str] | None = Field(None, description="SSH 私钥内容列表")


class FileTransferRequest(BaseModel):
    """文件传输请求模型"""

    host: str = Field(..., description="目标主机地址")
    local_path: str = Field(..., description="本地文件路径")
    remote_path: str = Field(..., description="远程文件路径")
    username: str = Field(..., description="SSH 登录用户名")
    password: str | None = Field(None, description="SSH 登录密码（与密钥二选一）")
    client_keys: list[str] | None = Field(None, description="SSH 私钥内容列表")


# ============================ 命令执行 ============================


@router.post(
    "/execute-command",
    summary="在单个主机上执行命令",
    description="通过 SSH 协议在指定远程主机上执行单个命令",
)
@auth_with_renew()
async def execute_command(
    request: Request,
    current_user: User,
    command_req: CommandRequest,
):
    """
    在单个主机上执行命令

    通过 SSH 协议连接到指定的远程主机，执行指定的命令并返回结果。

    Args:
        request: FastAPI 请求对象
        current_user: 当前认证用户
        command_req: 命令执行请求参数

    Returns:
        命令执行结果，包含退出码、标准输出和标准错误

    Raises:
        HTTPException: 当命令执行失败时抛出 500 错误
    """
    try:
        client = AsyncSSHClient()

        # 构建连接参数
        kwargs: dict[str, Any] = {"username": command_req.username}
        if command_req.password:
            kwargs["password"] = command_req.password
        if command_req.client_keys:
            kwargs["client_keys"] = command_req.client_keys

        # 执行命令
        result = await client.execute_command(
            command_req.host,
            command_req.command,
            **kwargs
        )
        return result

    except Exception as e:
        logger.error(f"执行命令失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/execute-multiple",
    summary="在多个主机上执行命令",
    description="通过 SSH 协议在多个远程主机上批量执行命令",
)
@auth_with_renew()
async def execute_multiple_commands(
    request: Request,
    current_user: User,
    multi_req: MultiCommandRequest,
):
    """
    在多个主机上执行命令

    通过 SSH 协议连接到多个远程主机，分别执行指定的命令并返回所有结果。

    Args:
        request: FastAPI 请求对象
        current_user: 当前认证用户
        multi_req: 多命令执行请求参数

    Returns:
        所有命令的执行结果列表

    Raises:
        HTTPException: 当命令执行失败时抛出 500 错误
    """
    try:
        client = AsyncSSHClient()

        # 解析主机和命令列表
        hosts_commands = [
            (item["host"], item["command"])
            for item in multi_req.commands
        ]

        # 构建连接参数
        kwargs: dict[str, Any] = {"username": multi_req.username}
        if multi_req.password:
            kwargs["password"] = multi_req.password
        if multi_req.client_keys:
            kwargs["client_keys"] = multi_req.client_keys

        # 执行多个命令
        results = await client.execute_multiple_commands(
            hosts_commands,
            **kwargs
        )
        return results

    except Exception as e:
        logger.error(f"执行多主机命令失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================ 文件传输 ============================


@router.post(
    "/upload-file",
    summary="上传文件到远程主机",
    description="通过 SSH 协议将本地文件上传到远程主机指定路径",
)
@auth_with_renew()
async def upload_file(
    request: Request,
    current_user: User,
    file_req: FileTransferRequest,
):
    """
    上传文件到远程主机

    通过 SSH 协议连接到指定的远程主机，将本地文件上传到指定的远程路径。

    Args:
        request: FastAPI 请求对象
        current_user: 当前认证用户
        file_req: 文件传输请求参数

    Returns:
        上传操作结果

    Raises:
        HTTPException: 当文件上传失败时抛出 500 错误
    """
    try:
        client = AsyncSSHClient()

        # 构建连接参数
        kwargs: dict[str, Any] = {"username": file_req.username}
        if file_req.password:
            kwargs["password"] = file_req.password
        if file_req.client_keys:
            kwargs["client_keys"] = file_req.client_keys

        # 上传文件
        result = await client.upload_file(
            file_req.host,
            file_req.local_path,
            file_req.remote_path,
            **kwargs
        )
        return result

    except Exception as e:
        logger.error(f"上传文件失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/download-file",
    summary="从远程主机下载文件",
    description="通过 SSH 协议从远程主机指定路径下载文件到本地",
)
@auth_with_renew()
async def download_file(
    request: Request,
    current_user: User,
    file_req: FileTransferRequest,
):
    """
    从远程主机下载文件

    通过 SSH 协议连接到指定的远程主机，从指定的远程路径下载文件到本地。

    Args:
        request: FastAPI 请求对象
        current_user: 当前认证用户
        file_req: 文件传输请求参数

    Returns:
        下载操作结果

    Raises:
        HTTPException: 当文件下载失败时抛出 500 错误
    """
    try:
        client = AsyncSSHClient()

        # 构建连接参数
        kwargs: dict[str, Any] = {"username": file_req.username}
        if file_req.password:
            kwargs["password"] = file_req.password
        if file_req.client_keys:
            kwargs["client_keys"] = file_req.client_keys

        # 下载文件
        result = await client.download_file(
            file_req.host,
            file_req.remote_path,
            file_req.local_path,
            **kwargs
        )
        return result

    except Exception as e:
        logger.error(f"下载文件失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))
