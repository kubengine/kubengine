"""
制品管理 API 路由模块

提供与 Harbor 镜像仓库交互的 HTTP API 接口，支持：
- 项目管理
- 仓库管理
- 制品管理
- 标签管理
- Chart 上传
- 镜像上传
"""

import os
import shutil
from typing import Any, Optional, Set

from fastapi import APIRouter, Body, Depends, File, HTTPException, Path, Query, Request, UploadFile
from pydantic import BaseModel, Field

from core.command import execute_command
from core.config.application import Application
from core.http_api_client.harbor_client import HarborClient
from core.logger import get_logger
from web.utils.auth import auth_with_renew
from web.utils.page import PageParams, pagination_params
from web.utils.response import success_response

logger = get_logger(__name__)

router = APIRouter(tags=["制品管理"])


# ============================ Pydantic 模型 ============================


class TagCreateRequest(BaseModel):
    """标签创建请求模型"""
    name: str = Field(..., description="标签名称")


# ============================ 辅助函数 ============================


def _validate_file_extension(filename: Optional[str], allowed_extensions: Set[str]) -> bool:
    """
    验证文件扩展名

    Args:
        filename: 文件名
        allowed_extensions: 允许的扩展名集合

    Returns:
        是否合法
    """
    if filename is None:
        return False
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed_extensions


def _check_file_size(file: UploadFile, max_size_mb: int) -> bool:
    """
    验证文件大小

    Args:
        file: 上传的文件对象
        max_size_mb: 最大文件大小（MB）

    Returns:
        是否符合大小限制
    """
    file.file.seek(0, os.SEEK_END)
    file_size = file.file.tell()
    file.file.seek(0)  # 重置文件指针
    return file_size <= max_size_mb * 1024 * 1024


# ============================ 项目管理 ============================


@router.get(
    "/projects",
    summary="获取所有项目",
    description="获取 Harbor 中的所有项目列表或指定项目信息",
)
@auth_with_renew()
async def get_projects(
    request: Request,
    project_id_or_name: Optional[str] = Query(
        None, description="项目 ID 或名称，不提供则获取所有项目"
    ),
):
    """
    获取项目列表

    Args:
        request: FastAPI 请求对象
        project_id_or_name: 项目 ID 或名称

    Returns:
        项目列表或指定项目信息
    """
    client = HarborClient()
    return client.get_projects(project_id_or_name)


@router.get(
    "/projects/{project_id_or_name}",
    summary="获取指定项目",
    description="根据项目 ID 或名称获取项目信息",
)
@auth_with_renew()
async def get_project_by_id_or_name(
    request: Request,
    project_id_or_name: str = Path(..., description="项目 ID 或名称"),
):
    """
    获取指定项目信息

    Args:
        request: FastAPI 请求对象
        project_id_or_name: 项目 ID 或名称

    Returns:
        项目信息
    """
    client = HarborClient()
    return client.get_projects(project_id_or_name)


# ============================ 仓库管理 ============================


@router.get(
    "/projects/{project_name}/repositories",
    summary="获取仓库列表",
    description="根据项目名称获取仓库列表",
)
@auth_with_renew()
async def get_repositories(
    request: Request,
    project_name: str = Path(..., description="项目名称"),
    query: Optional[str] = Query(None, description="搜索关键词"),
    pagination: PageParams = Depends(pagination_params),
):
    """
    获取仓库列表

    Args:
        request: FastAPI 请求对象
        project_name: 项目名称
        query: 搜索关键词
        pagination: 分页参数

    Returns:
        仓库列表
    """
    client = HarborClient()
    return client.get_repositories(project_name, query, pagination)


@router.delete(
    "/projects/{project_name}/repositories/{repository_name}",
    summary="删除仓库",
    description="根据项目名称和仓库名称删除仓库",
)
@auth_with_renew()
async def delete_repository(
    request: Request,
    project_name: str = Path(..., description="项目名称"),
    repository_name: str = Path(..., description="仓库名称"),
):
    """
    删除仓库

    Args:
        request: FastAPI 请求对象
        project_name: 项目名称
        repository_name: 仓库名称

    Returns:
        删除结果
    """
    client = HarborClient()
    return client.delete_repository(project_name, repository_name)


# ============================ 制品管理 ============================


@router.get(
    "/projects/{project_name}/repositories/{repository_name}/artifacts",
    summary="获取制品列表",
    description="根据仓库名称获取制品列表",
)
@auth_with_renew()
async def get_artifacts(
    request: Request,
    project_name: str = Path(..., description="项目名称"),
    repository_name: str = Path(..., description="仓库名称"),
    query: Optional[str] = Query(None, description="搜索关键词"),
    pagination: PageParams = Depends(pagination_params),
):
    """
    获取制品列表

    Args:
        request: FastAPI 请求对象
        project_name: 项目名称
        repository_name: 仓库名称
        query: 搜索关键词
        pagination: 分页参数

    Returns:
        制品列表
    """
    client = HarborClient()
    return client.get_artifacts(project_name, repository_name, query, pagination)


@router.get(
    "/projects/{project_name}/repositories/{repository_name}/artifacts/{digest}",
    summary="获取制品详情",
    description="根据制品摘要获取制品详细信息",
)
@auth_with_renew()
async def get_artifact(
    request: Request,
    project_name: str = Path(..., description="项目名称"),
    repository_name: str = Path(..., description="仓库名称"),
    digest: str = Path(..., description="制品摘要"),
):
    """
    获取制品详情

    Args:
        request: FastAPI 请求对象
        project_name: 项目名称
        repository_name: 仓库名称
        digest: 制品摘要

    Returns:
        制品详情
    """
    client = HarborClient()
    return client.get_artifact(project_name, repository_name, digest)


@router.delete(
    "/projects/{project_name}/repositories/{repository_name}/artifacts/{digest}",
    summary="删除制品",
    description="根据制品摘要删除制品",
)
@auth_with_renew()
async def delete_artifact(
    request: Request,
    project_name: str = Path(..., description="项目名称"),
    repository_name: str = Path(..., description="仓库名称"),
    digest: str = Path(..., description="制品摘要"),
):
    """
    删除制品

    Args:
        request: FastAPI 请求对象
        project_name: 项目名称
        repository_name: 仓库名称
        digest: 制品摘要

    Returns:
        删除结果
    """
    client = HarborClient()
    return client.delete_artifact(project_name, repository_name, digest)


# ============================ Chart Values 管理 ============================


@router.get(
    "/chart_values/{project_name}/{repository_name}/{digest}",
    summary="获取 Chart Values",
    description="获取 Chart 制品的 values.yaml 内容",
)
@auth_with_renew()
async def get_chart_values(
    request: Request,
    project_name: str = Path(..., description="项目名称"),
    repository_name: str = Path(..., description="仓库名称"),
    digest: str = Path(..., description="制品摘要"),
):
    """
    获取 Chart Values

    Args:
        request: FastAPI 请求对象
        project_name: 项目名称
        repository_name: 仓库名称
        digest: 制品摘要

    Returns:
        values.yaml 内容
    """
    client = HarborClient()
    return client.get_chart_values(project_name, repository_name, digest)


# ============================ 标签管理 ============================


@router.get(
    "/projects/{project_name}/repositories/{repository_name}/artifacts/{digest}/tags",
    summary="获取标签列表",
    description="获取制品的标签列表",
)
@auth_with_renew()
async def get_tags(
    request: Request,
    project_name: str = Path(..., description="项目名称"),
    repository_name: str = Path(..., description="仓库名称"),
    digest: str = Path(..., description="制品摘要"),
    pagination: PageParams = Depends(pagination_params),
):
    """
    获取标签列表

    Args:
        request: FastAPI 请求对象
        project_name: 项目名称
        repository_name: 仓库名称
        digest: 制品摘要
        pagination: 分页参数

    Returns:
        标签列表
    """
    client = HarborClient()
    return client.get_tags(project_name, repository_name, digest, pagination)


@router.post(
    "/projects/{project_name}/repositories/{repository_name}/artifacts/{digest}/tags",
    summary="添加标签",
    description="为制品添加标签",
)
@auth_with_renew()
async def add_tag(
    request: Request,
    project_name: str = Path(..., description="项目名称"),
    repository_name: str = Path(..., description="仓库名称"),
    digest: str = Path(..., description="制品摘要"),
    body: TagCreateRequest = Body(..., description="标签信息"),
):
    """
    添加标签

    Args:
        request: FastAPI 请求对象
        project_name: 项目名称
        repository_name: 仓库名称
        digest: 制品摘要
        body: 标签信息

    Returns:
        添加结果
    """
    client = HarborClient()
    return client.add_tag(project_name, repository_name, digest, body.name)


@router.delete(
    "/projects/{project_name}/repositories/{repository_name}/artifacts/{digest}/tags/{tag_name}",
    summary="删除标签",
    description="删除制品标签",
)
@auth_with_renew()
async def delete_tag(
    request: Request,
    project_name: str = Path(..., description="项目名称"),
    repository_name: str = Path(..., description="仓库名称"),
    digest: str = Path(..., description="制品摘要"),
    tag_name: str = Path(..., description="标签名称"),
):
    """
    删除标签

    Args:
        request: FastAPI 请求对象
        project_name: 项目名称
        repository_name: 仓库名称
        digest: 制品摘要
        tag_name: 标签名称

    Returns:
        删除结果
    """
    client = HarborClient()
    return client.delete_tag(project_name, repository_name, digest, tag_name)


# ============================ 文件上传 ============================


@router.post("/upload/chart", summary="上传 Chart 模板", description="上传 Helm Chart 到仓库")
@auth_with_renew()
async def upload_chart(
    request: Request,
    file: UploadFile = File(..., description="要上传的 Chart 文件"),
):
    """
    上传 Chart 模板

    支持 .tgz 和 .tar.gz 格式的 Chart 文件上传，最大文件大小 2MB。

    Args:
        request: FastAPI 请求对象
        file: 上传的文件

    Returns:
        上传结果
    """
    ALLOWED_EXTENSIONS: Set[str] = {"tgz", "tar.gz"}
    MAX_SIZE_MB = 2

    # 1. 验证文件扩展名
    if not _validate_file_extension(file.filename, ALLOWED_EXTENSIONS):
        raise HTTPException(
            status_code=400,
            detail=f"文件类型不允许！仅支持：{','.join(ALLOWED_EXTENSIONS)}",
        )

    # 2. 验证文件大小
    if not _check_file_size(file, MAX_SIZE_MB):
        raise HTTPException(
            status_code=400,
            detail=f"文件大小超过限制！最大支持 {MAX_SIZE_MB}MB",
        )

    # 3. 保存文件到本地
    file_path = os.path.join("/tmp", file.filename or "")
    try:
        # 流式写入（避免大文件占用过多内存）
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as e:
        logger.error(f"文件保存失败：{str(e)}")
        raise HTTPException(status_code=500, detail=f"文件保存失败：{str(e)}")
    finally:
        await file.close()

    # 4. 推送 chart 到仓库
    try:
        file_size_mb = os.path.getsize(file_path) / 1024 / 1024
        logger.info(f"开始推送 Chart: {file.filename} ({file_size_mb:.2f}MB)")

        cmd = (
            f"KUBECONFIG=/etc/kubernetes/admin.conf "
            f"helm push {file_path} "
            f"oci://{Application.DOMAIN}/charts "
            f"--username admin --password Harbor@123"
        )
        result = execute_command(cmd)

        if result.is_failure():
            error_msg = f"推送 Chart 到仓库失败：{result.get_error_lines()}"
            logger.error(error_msg)
            raise HTTPException(status_code=500, detail=error_msg)

        logger.info(f"Chart 推送成功: {file.filename}")

        data: dict[str, Any] = {
            "filename": file.filename,
            "content_type": file.content_type,
            "file_path": file_path,
            "file_size_mb": round(file_size_mb, 2),
        }

        return success_response(data=data, message="文件上传成功")

    finally:
        # 清理临时文件
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.debug(f"已删除临时文件: {file_path}")
            except Exception as e:
                logger.warning(f"删除临时文件失败：{str(e)}")


@router.post("/upload/image", summary="上传镜像", description="上传容器镜像到仓库")
@auth_with_renew()
async def upload_image(
    request: Request,
    file: UploadFile = File(..., description="要上传的镜像文件"),
):
    """
    上传镜像

    支持 .tar、.tgz、.tar.gz 格式的镜像文件上传，最大文件大小 10GB。

    Args:
        request: FastAPI 请求对象
        file: 上传的文件

    Returns:
        上传结果
    """
    ALLOWED_EXTENSIONS: Set[str] = {"tar", "tgz", "tar.gz"}
    MAX_SIZE_MB = 10240  # 10GB

    # 1. 验证 file extension
    if not _validate_file_extension(file.filename, ALLOWED_EXTENSIONS):
        raise HTTPException(
            status_code=400,
            detail=f"文件类型不允许！仅支持：{','.join(ALLOWED_EXTENSIONS)}",
        )

    # 2. 验证文件大小
    if not _check_file_size(file, MAX_SIZE_MB):
        raise HTTPException(
            status_code=400,
            detail=f"文件大小超过限制！最大支持 {MAX_SIZE_MB}MB",
        )

    # 3. 保存文件到本地
    file_path = os.path.join("/tmp", file.filename or "")
    try:
        # 流式写入（避免大文件占用过多内存）
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as e:
        logger.error(f"文件保存失败：{str(e)}")
        raise HTTPException(status_code=500, detail=f"文件保存失败：{str(e)}")
    finally:
        await file.close()

    # 4. 导入并推送镜像到仓库
    try:
        file_size_mb = os.path.getsize(file_path) / 1024 / 1024
        logger.info(f"开始导入镜像: {file.filename} ({file_size_mb:.2f}MB)")

        # 清理旧的未使用镜像
        execute_command("ctr -n apps i prune --all")

        # 导入镜像
        import_result = execute_command(f"ctr -n apps i import {file_path}")
        if import_result.is_failure():
            error_msg = f"导入镜像失败：{import_result.get_error_lines()}"
            logger.error(error_msg)
            raise HTTPException(status_code=500, detail=error_msg)

        logger.info(f"镜像导入成功: {file.filename}")

        # 获取镜像信息并验证镜像名称
        list_result = execute_command(
            "ctr -n apps i ls |awk '{print $1}'|grep -v REF")
        if list_result.is_failure():
            error_msg = f"获取镜像信息失败：{list_result.get_error_lines()}"
            logger.error(error_msg)
            raise HTTPException(status_code=500, detail=error_msg)

        image_names: str = "\n".join(list_result.get_output_lines())
        image_name_list = image_names.split()

        # 验证镜像名称格式
        for image_name in image_name_list:
            if not image_name.strip().startswith(f"{Application.DOMAIN}/apps"):
                error_msg = (
                    f"镜像名称有误 [{image_name}]，"
                    f"请变更为 {Application.DOMAIN}/apps/xxx 格式"
                )
                logger.error(error_msg)
                raise HTTPException(status_code=500, detail=error_msg)

        # 推送镜像到仓库
        for image_name in image_name_list:
            logger.info(f"开始推送镜像: {image_name.strip()}")
            push_result = execute_command(
                f"ctr -n apps i push -u admin:Harbor@123 {image_name.strip()}"
            )
            if push_result.is_failure():
                error_msg = f"推送镜像 [{image_name}] 失败：{push_result.get_error_lines()}"
                logger.error(error_msg)
                raise HTTPException(status_code=500, detail=error_msg)
            logger.info(f"镜像推送成功: {image_name.strip()}")

        data: dict[str, Any] = {
            "filename": file.filename,
            "content_type": file.content_type,
            "file_path": file_path,
            "file_size_mb": round(file_size_mb, 2),
            "images": image_name_list,
        }

        return success_response(data=data, message="文件上传成功")

    finally:
        # 清理临时文件
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.debug(f"已删除临时文件: {file_path}")
            except Exception as e:
                logger.warning(f"删除临时文件失败：{str(e)}")

        # 清理未使用的镜像
        try:
            execute_command("ctr -n apps i prune --all")
            logger.debug("已清理未使用的镜像")
        except Exception as e:
            logger.warning(f"清理镜像失败：{str(e)}")
