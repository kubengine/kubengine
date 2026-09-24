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

import fcntl
import json
import os
import shlex
import shutil
import sys
import tarfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path as FilePath
from typing import Any, Optional, Set

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    File,
    HTTPException,
    Path,
    Query,
    Request,
    UploadFile,
)
from pydantic import BaseModel, Field

from core.command import execute_command
from core.config.application import Application
from core.http_api_client.harbor_client import HarborClient
from core.logger import get_logger
from core.orm.image_import import (
    create_image_import_task,
    find_image_import_task,
    find_image_import_tasks,
    replace_image_import_items,
    update_image_import_item,
    update_image_import_task,
)
from web.utils.auth import auth_with_renew
from web.utils.page import PageParams, pagination_params

logger = get_logger(__name__)

router = APIRouter(tags=["制品管理"])


# ============================ Pydantic 模型 ============================


class TagCreateRequest(BaseModel):
    """标签创建请求模型"""

    name: str = Field(..., description="标签名称")


# ============================ 辅助函数 ============================


def _validate_file_extension(
    filename: Optional[str], allowed_extensions: Set[str]
) -> bool:
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
    normalized_filename = filename.lower()
    return any(
        normalized_filename.endswith(f".{extension.lower()}")
        for extension in allowed_extensions
    )


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


def _split_image_ref(ref: str) -> tuple[str, str]:
    """Split an image reference into registry and repository with tag."""
    parts = ref.split("/", 1)
    if len(parts) == 2 and ("." in parts[0] or ":" in parts[0]):
        return parts[0], parts[1]
    return "docker.io", f"library/{ref}" if "/" not in ref else ref


def _parse_image_ref(ref: str) -> dict[str, str]:
    """Parse an image reference into fields used by task details."""
    registry, repo_tag = _split_image_ref(ref)
    if "@" in repo_tag:
        repository, tag = repo_tag.rsplit("@", 1)
        tag = f"@{tag}"
    elif ":" in repo_tag.rsplit("/", 1)[-1]:
        repository, tag = repo_tag.rsplit(":", 1)
    else:
        repository, tag = repo_tag, "latest"
    return {
        "image_ref": ref,
        "registry": registry,
        "repository": repository,
        "tag": tag,
    }


def _inspect_image_archive(file_path: str) -> dict[str, str]:
    """Return image-specific errors for missing OCI/Docker archive content."""

    def normalized_name(name: str) -> str:
        return name[2:] if name.startswith("./") else name

    def platform_name(platform: Optional[dict[str, Any]]) -> str:
        if not platform:
            return "未知平台"
        os_name = platform.get("os") or "unknown"
        architecture = platform.get("architecture") or "unknown"
        variant = platform.get("variant")
        return f"{os_name}/{architecture}" + (f"/{variant}" if variant else "")

    try:
        with tarfile.open(file_path, mode="r:*") as archive:
            members = {
                normalized_name(member.name): member
                for member in archive.getmembers()
                if member.isfile()
            }

            def read_json(member_name: str) -> dict[str, Any]:
                member = members.get(member_name)
                if member is None:
                    raise ValueError(f"缺少 {member_name}")
                if member.size > 16 * 1024 * 1024:
                    raise ValueError(f"元数据文件过大：{member_name}")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError(f"无法读取 {member_name}")
                value = json.load(stream)
                if not isinstance(value, dict):
                    raise ValueError(f"元数据格式错误：{member_name}")
                return value

            errors: dict[str, list[str]] = {}
            if "index.json" in members:
                index = read_json("index.json")
                visited: set[tuple[str, str]] = set()

                def walk_descriptor(
                    descriptor: dict[str, Any],
                    image_ref: str,
                    inherited_platform: Optional[dict[str, Any]] = None,
                ) -> None:
                    digest = str(descriptor.get("digest") or "")
                    media_type = str(descriptor.get("mediaType") or "")
                    platform = descriptor.get("platform") or inherited_platform
                    if ":" not in digest:
                        errors.setdefault(image_ref, []).append(
                            f"{platform_name(platform)} 描述符缺少有效 digest"
                        )
                        return
                    algorithm, encoded = digest.split(":", 1)
                    member_name = f"blobs/{algorithm}/{encoded}"
                    if member_name not in members:
                        content_type = (
                            "manifest"
                            if "manifest" in media_type or "index" in media_type
                            else "blob"
                        )
                        errors.setdefault(image_ref, []).append(
                            f"缺少 {platform_name(platform)} {content_type}：{digest}"
                        )
                        return

                    visit_key = (image_ref, digest)
                    if visit_key in visited:
                        return
                    visited.add(visit_key)
                    if "manifest" not in media_type and "index" not in media_type:
                        return

                    document = read_json(member_name)
                    children: list[dict[str, Any]] = []
                    children.extend(document.get("manifests") or [])
                    config = document.get("config")
                    if isinstance(config, dict):
                        children.append(config)
                    children.extend(document.get("layers") or [])
                    for child in children:
                        if isinstance(child, dict):
                            walk_descriptor(child, image_ref, platform)

                for descriptor in index.get("manifests") or []:
                    if not isinstance(descriptor, dict):
                        continue
                    annotations = descriptor.get("annotations") or {}
                    image_ref = (
                        annotations.get("io.containerd.image.name")
                        or annotations.get("org.opencontainers.image.ref.name")
                        or str(descriptor.get("digest") or "未知镜像")
                    )
                    walk_descriptor(descriptor, str(image_ref))

                return {
                    image_ref: "；".join(messages)
                    for image_ref, messages in errors.items()
                }

            # Docker archives do not have index.json. Validate the paths named
            # by manifest.json without reading or hashing large layer payloads.
            if "manifest.json" in members:
                member = members["manifest.json"]
                if member.size > 16 * 1024 * 1024:
                    raise ValueError("元数据文件过大：manifest.json")
                stream = archive.extractfile(member)
                manifests = json.load(stream) if stream is not None else []
                if not isinstance(manifests, list):
                    raise ValueError("元数据格式错误：manifest.json")
                for manifest in manifests:
                    if not isinstance(manifest, dict):
                        continue
                    refs = manifest.get("RepoTags") or ["未知镜像"]
                    content_paths = [
                        manifest.get("Config"),
                        *(manifest.get("Layers") or []),
                    ]
                    missing = [
                        str(path)
                        for path in content_paths
                        if path and normalized_name(str(path)) not in members
                    ]
                    for image_ref in refs:
                        if missing:
                            errors[str(image_ref)] = [
                                "缺少 blob：" + "、".join(missing)
                            ]
                return {
                    image_ref: "；".join(messages)
                    for image_ref, messages in errors.items()
                }
            return {}
    except (tarfile.TarError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"镜像包完整性预检失败：{exc}") from exc


@contextmanager
def _image_import_lock():
    """Serialize access to the shared apps containerd namespace."""
    lock_path = FilePath("/tmp/kubengine-image-import.lock")
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


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


@router.post(
    "/upload/chart", summary="上传 Chart 模板", description="上传 Helm Chart 到仓库"
)
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

        return 200, "文件上传成功", data

    finally:
        # 清理临时文件
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.debug(f"已删除临时文件: {file_path}")
            except Exception as e:
                logger.warning(f"删除临时文件失败：{str(e)}")


def _prune_apps_namespace() -> Optional[str]:
    """Clean the temporary image namespace and return an error if it fails."""
    try:
        result = execute_command("ctr -n apps i prune --all", timeout=600)
        if result.is_failure():
            return f"清理 apps namespace 失败：{result.get_error_lines()}"
    except Exception as exc:
        return f"清理 apps namespace 异常：{exc}"
    return None


def _mark_task_items_failed(
    task_id: int, image_refs: list[str], stage: str, error: str
) -> None:
    for image_ref in image_refs:
        update_image_import_item(task_id, image_ref, "failed", stage, error)


def _finish_image_import_task(task_id: int) -> None:
    task = find_image_import_task(task_id)
    if task is None:
        return
    items = task.get("items", [])
    success_count = sum(item["status"] == "success" for item in items)
    failed_count = sum(item["status"] == "failed" for item in items)
    if failed_count == 0 and success_count == len(items) and items:
        status = "success"
    elif success_count > 0:
        status = "partial_success"
    else:
        status = "failed"
    update_image_import_task(
        task_id,
        status=status,
        total_count=len(items),
        success_count=success_count,
        failed_count=failed_count,
        completed_at=datetime.now(),
    )


def process_image_import_task(task_id: int, retry_failed: bool = False) -> None:
    """Import an archive and persist the result of every contained image."""
    task = find_image_import_task(task_id, include_file_path=True)
    if task is None:
        return
    file_path = str(task.get("file_path") or "")
    existing_items = task.get("items", [])
    retry_refs = {
        item["image_ref"] for item in existing_items if item["status"] == "failed"
    }
    update_image_import_task(
        task_id,
        status="processing",
        error_message=None,
        completed_at=None,
    )
    namespace_touched = False

    try:
        if not file_path or not os.path.exists(file_path):
            raise RuntimeError("原始镜像文件已不存在")
        validation_errors = _inspect_image_archive(file_path)
        if validation_errors:
            summary = "镜像包引用内容不完整：" + "；".join(
                f"{image_ref}: {error}"
                for image_ref, error in validation_errors.items()
            )
            update_image_import_task(task_id, error_message=summary)
        with _image_import_lock():
            namespace_touched = True
            cleanup_error = _prune_apps_namespace()
            if cleanup_error:
                raise RuntimeError(cleanup_error)

            import_result = execute_command(
                f"ctr -n apps i import {shlex.quote(file_path)}", timeout=600
            )
            if import_result.is_failure():
                error = f"导入镜像失败：{import_result.get_error_lines()}"
                if retry_refs:
                    _mark_task_items_failed(task_id, list(retry_refs), "import", error)
                raise RuntimeError(error)

            list_result = execute_command("ctr -n apps i ls -q", timeout=120)
            if list_result.is_failure():
                raise RuntimeError(f"获取镜像信息失败：{list_result.get_error_lines()}")
            image_refs = [
                line.strip() for line in list_result.get_output_lines() if line.strip()
            ]
            if not image_refs:
                raise RuntimeError("导入后 apps namespace 中没有镜像")

            if not retry_failed or not existing_items:
                item_refs = list(
                    dict.fromkeys([*image_refs, *validation_errors.keys()])
                )
                replace_image_import_items(
                    task_id, [_parse_image_ref(ref) for ref in item_refs]
                )
                update_image_import_task(
                    task_id,
                    total_count=len(item_refs),
                    success_count=0,
                    failed_count=0,
                )
                target_refs = image_refs
            else:
                target_refs = [ref for ref in image_refs if ref in retry_refs]
                for ref in target_refs:
                    update_image_import_item(task_id, ref, "processing", "retry", None)

            for image_ref, error in validation_errors.items():
                update_image_import_item(
                    task_id,
                    image_ref,
                    "failed",
                    "validate",
                    f"镜像包完整性校验失败：{error}",
                )
            target_refs = [
                ref for ref in target_refs if ref not in validation_errors
            ]

            registries = {_split_image_ref(ref)[0] for ref in target_refs}
            harbor_client = HarborClient()
            invalid_registries: set[str] = set()
            for registry in sorted(registries):
                if not harbor_client.create_project(registry, public=True):
                    invalid_registries.add(registry)
                    error = f"Harbor 项目 [{registry}] 创建失败"
                    refs = [
                        ref
                        for ref in target_refs
                        if _split_image_ref(ref)[0] == registry
                    ]
                    _mark_task_items_failed(task_id, refs, "create_project", error)

            push_refs = [
                ref
                for ref in target_refs
                if _split_image_ref(ref)[0] not in invalid_registries
            ]
            proxy_registries = {_split_image_ref(ref)[0] for ref in push_refs}
            if proxy_registries:
                proxy_command = (
                    f"{shlex.quote(sys.executable)} -m cli.app "
                    # 镜像导入只配置本机。集群同步属于独立的运维变更，
                    # 必须由管理员显式执行 add-proxy（不带 --no-sync）。
                    "image ctr add-proxy -y --no-sync "
                    + " ".join(
                        shlex.quote(registry) for registry in sorted(proxy_registries)
                    )
                )
                proxy_result = execute_command(proxy_command, timeout=600)
                if proxy_result.is_failure():
                    error = (
                        "containerd 透明代理配置失败："
                        f"{proxy_result.get_error_lines()}"
                    )
                    _mark_task_items_failed(task_id, push_refs, "proxy", error)
                    push_refs = []

            registry_auth = (
                f"{Application.REGISTRY.USERNAME}:" f"{Application.REGISTRY.PASSWORD}"
            )
            for image_ref in push_refs:
                update_image_import_item(task_id, image_ref, "processing", "push", None)
                push_result = execute_command(
                    "ctr -n apps i push "
                    "--hosts-dir /etc/containerd/certs.d/ "
                    f"-u {shlex.quote(registry_auth)} "
                    f"{shlex.quote(image_ref)}",
                    timeout=600,
                )
                if push_result.is_failure():
                    update_image_import_item(
                        task_id,
                        image_ref,
                        "failed",
                        "push",
                        f"推送失败：{push_result.get_error_lines()}",
                    )
                else:
                    update_image_import_item(
                        task_id, image_ref, "success", "completed", None
                    )

            _finish_image_import_task(task_id)
    except Exception as exc:
        logger.exception("镜像导入任务 %s 失败", task_id)
        current = find_image_import_task(task_id)
        if current:
            pending_refs = [
                item["image_ref"]
                for item in current.get("items", [])
                if item["status"] in {"pending", "processing"}
            ]
            _mark_task_items_failed(task_id, pending_refs, "system", str(exc))
        update_image_import_task(
            task_id,
            status="failed",
            error_message=str(exc),
            completed_at=datetime.now(),
        )
        _finish_image_import_task(task_id)
    finally:
        # Pruning must use the same lock as import/push. Otherwise another task
        # could import images between lock release and this final cleanup.
        if namespace_touched:
            with _image_import_lock():
                cleanup_error = _prune_apps_namespace()
            if cleanup_error:
                logger.warning(cleanup_error)


async def _create_image_import(
    file: UploadFile, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    allowed_extensions: Set[str] = {"tar", "tgz", "tar.gz"}
    if not _validate_file_extension(file.filename, allowed_extensions):
        raise HTTPException(
            status_code=400,
            detail=f"文件类型不允许！仅支持：{','.join(allowed_extensions)}",
        )

    filename = os.path.basename(file.filename or "images.tar")
    task = create_image_import_task(filename, file.content_type, "")
    task_id = int(task["task_id"])
    task_dir = FilePath(Application.ROOT_DIR) / "tmp" / "image-imports" / str(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    file_path = task_dir / filename
    try:
        with file_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        update_image_import_task(
            task_id,
            file_path=str(file_path),
            file_size=file_path.stat().st_size,
        )
    except Exception as exc:
        update_image_import_task(
            task_id,
            status="failed",
            error_message=f"保存上传文件失败：{exc}",
            completed_at=datetime.now(),
        )
        raise HTTPException(status_code=500, detail=f"文件保存失败：{exc}")
    finally:
        await file.close()

    background_tasks.add_task(process_image_import_task, task_id)
    result = find_image_import_task(task_id, include_items=False)
    return result or task


@router.post(
    "/image-import-tasks",
    summary="创建镜像导入任务",
    description="上传离线镜像文件并在后台导入 Harbor",
)
@auth_with_renew()
async def create_image_import_api(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="要上传的镜像文件"),
):
    task = await _create_image_import(file, background_tasks)
    return 200, "镜像导入任务已创建", task


@router.post("/upload/image", summary="上传镜像", description="创建镜像导入任务")
@auth_with_renew()
async def upload_image(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="要上传的镜像文件"),
):
    task = await _create_image_import(file, background_tasks)
    return 200, "镜像导入任务已创建", task


@router.get("/image-import-tasks", summary="获取镜像导入任务")
@auth_with_renew()
async def list_image_import_tasks_api(
    request: Request,
    pagination: PageParams = Depends(pagination_params),
):
    return find_image_import_tasks(pagination.page, pagination.page_size)


@router.get("/image-import-tasks/{task_id}", summary="获取镜像导入详情")
@auth_with_renew()
async def get_image_import_task_api(
    request: Request,
    task_id: int = Path(..., description="镜像导入任务 ID"),
):
    task = find_image_import_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="镜像导入任务不存在")
    return task


@router.post("/image-import-tasks/{task_id}/retry", summary="重试失败镜像")
@auth_with_renew()
async def retry_image_import_task_api(
    request: Request,
    background_tasks: BackgroundTasks,
    task_id: int = Path(..., description="镜像导入任务 ID"),
):
    task = find_image_import_task(task_id, include_file_path=True)
    if task is None:
        raise HTTPException(status_code=404, detail="镜像导入任务不存在")
    if task["status"] in {"pending", "processing"}:
        raise HTTPException(status_code=409, detail="镜像导入任务正在处理")
    if not os.path.exists(str(task.get("file_path") or "")):
        raise HTTPException(status_code=410, detail="原始镜像文件已不存在")
    background_tasks.add_task(process_image_import_task, task_id, True)
    update_image_import_task(task_id, status="pending", completed_at=None)
    return find_image_import_task(task_id, include_items=False)
