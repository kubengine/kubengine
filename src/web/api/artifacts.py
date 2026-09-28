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

import asyncio
import fcntl
import json
import os
import shutil
import sys
import tarfile
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path as FilePath
from typing import Any, Optional, Set

from fastapi import (
    APIRouter,
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
from starlette.concurrency import run_in_threadpool

from core.command import execute_command
from core.config.application import Application
from core.http_api_client.harbor_client import HarborClient
from core.logger import get_logger, with_log_context
from core.orm.image_import import (
    ImageImportLease,
    ImageImportLeaseLost,
    assert_image_import_lease,
    create_image_import_task,
    find_image_import_task,
    find_image_import_tasks,
    replace_image_import_items,
    requeue_image_import_task,
    update_image_import_item,
    update_image_import_task,
)
from core.runtime_files import private_runtime_file
from web.utils.auth import auth_with_renew
from web.utils.page import PageParams, pagination_params
from web.utils.uploads import SecureUploadRoute

logger = get_logger(__name__)

router = APIRouter(tags=["制品管理"], route_class=SecureUploadRoute)


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
    """
    Split an image reference into registry and repository with tag.
    """
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
    """
    Return image-specific errors for missing OCI/Docker archive content.
    """

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
                            if "manifest" in media_type
                            or "index" in media_type
                            else "blob"
                        )
                        errors.setdefault(image_ref, []).append(
                            (
                                f"缺少 {platform_name(platform)} {content_type}："
                                f"{digest}"
                            )
                        )
                        return

                    visit_key = (image_ref, digest)
                    if visit_key in visited:
                        return
                    visited.add(visit_key)
                    if (
                        "manifest" not in media_type
                        and "index" not in media_type
                    ):
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

            # Docker archives do not have index.json. Validate the paths
            # named
            # by manifest.json without reading or hashing large layer
            # payloads.
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
    except (
        tarfile.TarError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise RuntimeError(f"镜像包完整性预检失败：{exc}") from exc


@contextmanager
def _image_import_lock(*, blocking=True):
    """Serialize access to the shared apps containerd namespace."""
    with private_runtime_file("image-import.lock") as lock_file:
        try:
            fcntl.flock(
                lock_file.fileno(),
                fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB),
            )
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


# ============================ 项目管理 ============================


@router.get(
    "/projects",
    summary="获取所有项目",
    description="获取 Harbor 中的所有项目列表或指定项目信息",
)
@auth_with_renew()
def get_projects(
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
def get_project_by_id_or_name(
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
def get_repositories(
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
def delete_repository(
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
def get_artifacts(
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
    return client.get_artifacts(
        project_name, repository_name, query, pagination
    )


@router.get(
    (
        "/projects/{project_name}/repositories/{repository_name}/artifacts/"
        "{digest}"
    ),
    summary="获取制品详情",
    description="根据制品摘要获取制品详细信息",
)
@auth_with_renew()
def get_artifact(
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
    (
        "/projects/{project_name}/repositories/{repository_name}/artifacts/"
        "{digest}"
    ),
    summary="删除制品",
    description="根据制品摘要删除制品",
)
@auth_with_renew()
def delete_artifact(
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


# ============================ Chart Values 管理
# ============================


@router.get(
    "/chart_values/{project_name}/{repository_name}/{digest}",
    summary="获取 Chart Values",
    description="获取 Chart 制品的 values.yaml 内容",
)
@auth_with_renew()
def get_chart_values(
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
    (
        "/projects/{project_name}/repositories/{repository_name}/artifacts/"
        "{digest}/tags"
    ),
    summary="获取标签列表",
    description="获取制品的标签列表",
)
@auth_with_renew()
def get_tags(
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
    (
        "/projects/{project_name}/repositories/{repository_name}/artifacts/"
        "{digest}/tags"
    ),
    summary="添加标签",
    description="为制品添加标签",
)
@auth_with_renew()
def add_tag(
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
    (
        "/projects/{project_name}/repositories/{repository_name}/artifacts/"
        "{digest}/tags/{tag_name}"
    ),
    summary="删除标签",
    description="删除制品标签",
)
@auth_with_renew()
def delete_tag(
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
    "/upload/chart",
    summary="上传 Chart 模板",
    description="上传 Helm Chart 到仓库",
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

    try:
        if not _validate_file_extension(file.filename, ALLOWED_EXTENSIONS):
            raise HTTPException(
                status_code=400,
                detail=f"文件类型不允许！仅支持：{','.join(ALLOWED_EXTENSIONS)}",
            )

        def _push_chart() -> dict[str, Any]:
            if not _check_file_size(file, MAX_SIZE_MB):
                raise HTTPException(
                    status_code=400,
                    detail=f"文件大小超过限制！最大支持 {MAX_SIZE_MB}MB",
                )

            # The client filename is metadata only. Each upload owns its
            # private
            # directory, including cleanup on copy errors and failed
            # pushes.
            with tempfile.TemporaryDirectory(
                prefix="kubengine-chart-"
            ) as task_dir:
                file_path = FilePath(task_dir) / "chart.tgz"
                try:
                    with file_path.open("xb") as buffer:
                        shutil.copyfileobj(file.file, buffer)
                except OSError:
                    logger.exception("Chart 文件保存失败")
                    raise HTTPException(
                        status_code=500, detail="文件保存失败"
                    ) from None

                file_size_mb = file_path.stat().st_size / 1024 / 1024
                logger.info("开始推送 Chart (%.2fMB)", file_size_mb)
                result = execute_command(
                    [
                        "helm",
                        "push",
                        str(file_path),
                        f"oci://{Application.DOMAIN}/charts",
                        "--username",
                        Application.REGISTRY.USERNAME,
                        "--password",
                        Application.REGISTRY.PASSWORD,
                    ],
                    env={"KUBECONFIG": "/etc/kubernetes/admin.conf"},
                    timeout=600,
                )
                if result.is_failure():
                    logger.error("推送 Chart 到仓库失败")
                    raise HTTPException(
                        status_code=500, detail="推送 Chart 到仓库失败"
                    )

                logger.info("Chart 推送成功")
                return {
                    "filename": os.path.basename(
                        (file.filename or "").replace("\\", "/")
                    ),
                    "content_type": file.content_type,
                    "file_size_mb": round(file_size_mb, 2),
                }

        # File copies and Helm execution must not block the request
        # event loop.
        data = await run_in_threadpool(_push_chart)
        return 200, "文件上传成功", data
    finally:
        await file.close()


def _prune_apps_namespace(namespace: str = "apps") -> Optional[str]:
    """
    Clean the temporary image namespace and return an error if it fails.
    """
    try:
        result = execute_command(
            ["ctr", "-n", namespace, "i", "prune", "--all"], timeout=600
        )
        if result.is_failure():
            return (
                f"清理 {namespace} namespace 失败：{result.get_error_lines()}"
            )
    except Exception as exc:
        return f"清理 {namespace} namespace 异常：{exc}"
    return None


def _cleanup_image_namespace(namespace: str) -> Optional[str]:
    error = _prune_apps_namespace(namespace)
    if error or not namespace.startswith("kubengine-import-"):
        return error
    try:
        namespaces = execute_command(
            ["ctr", "namespaces", "list", "-q"], timeout=120
        )
        if namespaces.is_failure():
            return "无法确认临时镜像 namespace 的清理结果"
        if namespace in {
            line.strip() for line in namespaces.get_output_lines()
        }:
            result = execute_command(
                ["ctr", "namespaces", "remove", namespace], timeout=120
            )
            if result.is_failure():
                return f"删除临时 namespace 失败：{result.get_error_lines()}"
    except Exception as exc:
        return f"删除临时 namespace 异常：{exc}"
    return None


def _mark_task_items_failed(
    task_id: int,
    image_refs: list[str],
    stage: str,
    error: str,
    *,
    lease: ImageImportLease,
) -> None:
    for image_ref in image_refs:
        update_image_import_item(
            task_id, image_ref, "failed", stage, error, lease=lease
        )


def _finish_image_import_task(
    task_id: int, *, lease: ImageImportLease
) -> None:
    task = find_image_import_task(task_id)
    if task is None:
        return
    items = task.get("items", [])
    success_count = sum(item["status"] == "success" for item in items)
    failed_count = sum(item["status"] == "failed" for item in items)
    if task.get("cleanup_pending"):
        status = "failed"
    elif failed_count == 0 and success_count == len(items) and items:
        status = "success"
    elif success_count > 0:
        status = "partial_success"
    else:
        status = "failed"
    update_image_import_task(
        task_id,
        lease=lease,
        status=status,
        error_message=task.get("error_message") or task.get("cleanup_error"),
        total_count=len(items),
        success_count=success_count,
        failed_count=failed_count,
        completed_at=datetime.now(),
        lease_owner=None,
        lease_expires_at=None,
        heartbeat_at=None,
        retry_failed=False,
    )


@with_log_context(task_id="task_id")
def process_image_import_task(
    task_id: int,
    retry_failed: bool = False,
    *,
    lease: ImageImportLease,
    lease_lost: Optional[threading.Event] = None,
) -> None:
    """
    Import an archive and persist the result of every contained image.
    """

    def check_lease() -> None:
        if lease_lost is not None and lease_lost.is_set():
            raise ImageImportLeaseLost(
                f"Image-import task {task_id} lease was lost"
            )
        assert_image_import_lease(task_id, lease)

    check_lease()
    task = find_image_import_task(task_id, include_file_path=True)
    if task is None:
        return
    file_path = str(task.get("file_path") or "")
    namespace = task["namespace"]
    existing_items = task.get("items", [])
    retry_refs = {
        item["image_ref"]
        for item in existing_items
        if item["status"] != "success"
    }
    update_image_import_task(
        task_id,
        lease=lease,
        status="processing",
        error_message=None,
        completed_at=None,
    )
    try:
        if not file_path or not os.path.exists(file_path):
            raise RuntimeError("原始镜像文件已不存在")
        validation_errors = _inspect_image_archive(file_path)
        if validation_errors:
            summary = "镜像包引用内容不完整：" + "；".join(
                f"{image_ref}: {error}"
                for image_ref, error in validation_errors.items()
            )
            update_image_import_task(
                task_id, error_message=summary, lease=lease
            )
        with _image_import_lock():
            # A claimant may have waited behind another task long enough
            # to
            # lose its lease. Never touch the namespace before checking
            # again.
            check_lease()
            try:
                cleanup_error = _prune_apps_namespace(namespace)
                if cleanup_error:
                    raise RuntimeError(cleanup_error)
                check_lease()
                import_result = execute_command(
                    ["ctr", "-n", namespace, "i", "import", file_path],
                    timeout=600,
                )
                check_lease()
                if import_result.is_failure():
                    raise RuntimeError(
                        f"导入镜像失败：{import_result.get_error_lines()}"
                    )

                list_result = execute_command(
                    ["ctr", "-n", namespace, "i", "ls", "-q"], timeout=120
                )
                check_lease()
                if list_result.is_failure():
                    raise RuntimeError(
                        f"获取镜像信息失败：{list_result.get_error_lines()}"
                    )
                image_refs = [
                    line.strip()
                    for line in list_result.get_output_lines()
                    if line.strip()
                ]
                if not image_refs:
                    raise RuntimeError("导入后 apps namespace 中没有镜像")

                if not existing_items:
                    item_refs = list(
                        dict.fromkeys([*image_refs, *validation_errors])
                    )
                    replace_image_import_items(
                        task_id,
                        [_parse_image_ref(ref) for ref in item_refs],
                        lease=lease,
                    )
                    update_image_import_task(
                        task_id,
                        lease=lease,
                        total_count=len(item_refs),
                        success_count=0,
                        failed_count=0,
                    )
                    unfinished_refs = set(item_refs)
                else:
                    # Both an interrupted first run and a user retry
                    # resume all
                    # unfinished items. A completed push must not be
                    # reset.
                    unfinished_refs = retry_refs

                missing_refs = (
                    unfinished_refs - set(image_refs) - set(validation_errors)
                )
                _mark_task_items_failed(
                    task_id,
                    list(missing_refs),
                    "import",
                    "镜像包导入后未找到该镜像",
                    lease=lease,
                )
                for image_ref, error in validation_errors.items():
                    if image_ref in unfinished_refs:
                        update_image_import_item(
                            task_id,
                            image_ref,
                            "failed",
                            "validate",
                            f"镜像包完整性校验失败：{error}",
                            lease=lease,
                        )
                target_refs = [
                    ref
                    for ref in image_refs
                    if ref in unfinished_refs and ref not in validation_errors
                ]
                for ref in target_refs:
                    update_image_import_item(
                        task_id,
                        ref,
                        "processing",
                        "retry" if existing_items else "import",
                        lease=lease,
                    )

                registries = {_split_image_ref(ref)[0] for ref in target_refs}
                harbor_client = HarborClient()
                invalid_registries: set[str] = set()
                for registry in sorted(registries):
                    check_lease()
                    created = harbor_client.create_project(
                        registry, public=True
                    )
                    check_lease()
                    if not created:
                        invalid_registries.add(registry)
                        refs = [
                            ref
                            for ref in target_refs
                            if _split_image_ref(ref)[0] == registry
                        ]
                        _mark_task_items_failed(
                            task_id,
                            refs,
                            "create_project",
                            f"Harbor 项目 [{registry}] 创建失败",
                            lease=lease,
                        )

                push_refs = [
                    ref
                    for ref in target_refs
                    if _split_image_ref(ref)[0] not in invalid_registries
                ]
                proxy_registries = {
                    _split_image_ref(ref)[0] for ref in push_refs
                }
                if proxy_registries:
                    check_lease()
                    proxy_command = [
                        sys.executable,
                        "-m",
                        "cli.app",
                        # Only configure this host; cluster-wide sync is
                        # explicit.
                        "image",
                        "ctr",
                        "add-proxy",
                        "-y",
                        "--no-sync",
                        # ctr reads --hosts-dir for each push. Restarting
                        # containerd here races with socket creation.
                        "--no-restart",
                        *sorted(proxy_registries),
                    ]
                    proxy_result = execute_command(proxy_command, timeout=600)
                    check_lease()
                    if proxy_result.is_failure():
                        _mark_task_items_failed(
                            task_id,
                            push_refs,
                            "proxy",
                            (
                                "containerd 透明代理配置失败："
                                f"{proxy_result.get_error_lines()}"
                            ),
                            lease=lease,
                        )
                        push_refs = []

                registry_auth = (
                    f"{Application.REGISTRY.USERNAME}:"
                    f"{Application.REGISTRY.PASSWORD}"
                )
                for image_ref in push_refs:
                    check_lease()
                    update_image_import_item(
                        task_id, image_ref, "processing", "push", lease=lease
                    )
                    push_result = execute_command(
                        [
                            "ctr",
                            "-n",
                            namespace,
                            "i",
                            "push",
                            "--hosts-dir",
                            "/etc/containerd/certs.d/",
                            "-u",
                            registry_auth,
                            image_ref,
                        ],
                        timeout=600,
                    )
                    check_lease()
                    if push_result.is_failure():
                        update_image_import_item(
                            task_id,
                            image_ref,
                            "failed",
                            "push",
                            f"推送失败：{push_result.get_error_lines()}",
                            lease=lease,
                        )
                    else:
                        update_image_import_item(
                            task_id,
                            image_ref,
                            "success",
                            "completed",
                            lease=lease,
                        )

                check_lease()
            finally:
                # Keep the same lock through cleanup, even after losing
                # the DB
                # lease. The new attempt cannot touch this namespace
                # until we
                # finish cleaning our own execution and release this
                # lock.
                cleanup_error = _cleanup_image_namespace(namespace)
                update_image_import_task(
                    task_id,
                    lease=lease,
                    cleanup_pending=bool(cleanup_error),
                    cleanup_error=cleanup_error,
                    cleanup_retry_at=(
                        datetime.now() + timedelta(seconds=60)
                        if cleanup_error
                        else None
                    ),
                )
                if cleanup_error:
                    logger.warning(cleanup_error)
            _finish_image_import_task(task_id, lease=lease)
    except ImageImportLeaseLost:
        logger.warning("镜像导入任务 %s 已丢失租约，停止旧执行", task_id)
        raise
    except Exception as exc:
        logger.exception("镜像导入任务 %s 失败", task_id)
        check_lease()
        current = find_image_import_task(task_id)
        if current:
            unfinished_refs = [
                item["image_ref"]
                for item in current.get("items", [])
                if item["status"] != "success"
            ]
            _mark_task_items_failed(
                task_id, unfinished_refs, "system", str(exc), lease=lease
            )
        update_image_import_task(task_id, error_message=str(exc), lease=lease)
        _finish_image_import_task(task_id, lease=lease)


async def _create_image_import(file: UploadFile) -> dict[str, Any]:
    allowed_extensions: Set[str] = {"tar", "tgz", "tar.gz"}
    if not _validate_file_extension(file.filename, allowed_extensions):
        raise HTTPException(
            status_code=400,
            detail=f"文件类型不允许！仅支持：{','.join(allowed_extensions)}",
        )

    from core.image_storage import store_archive

    filename = os.path.basename(
        (file.filename or "images.tar").replace("\\", "/")
    )
    task = create_image_import_task(filename, file.content_type, "")
    task_id = int(task["task_id"])
    cancelled = threading.Event()
    copying = asyncio.create_task(
        run_in_threadpool(store_archive, task_id, file.file, cancelled)
    )
    try:
        await asyncio.shield(copying)
    except asyncio.CancelledError:
        cancelled.set()
        try:
            await copying
        except Exception:
            pass
        raise
    except HTTPException:
        raise
    except Exception:
        logger.exception("保存上传镜像失败")
        raise HTTPException(
            status_code=500, detail="保存上传镜像失败"
        ) from None
    finally:
        await file.close()

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
    file: UploadFile = File(..., description="要上传的镜像文件"),
):
    task = await _create_image_import(file)
    return 200, "镜像导入任务已创建", task


@router.post(
    "/upload/image", summary="上传镜像", description="创建镜像导入任务"
)
@auth_with_renew()
async def upload_image(
    request: Request,
    file: UploadFile = File(..., description="要上传的镜像文件"),
):
    task = await _create_image_import(file)
    return 200, "镜像导入任务已创建", task


@router.get("/image-import-tasks", summary="获取镜像导入任务")
@auth_with_renew()
def list_image_import_tasks_api(
    request: Request,
    pagination: PageParams = Depends(pagination_params),
):
    return find_image_import_tasks(pagination.page, pagination.page_size)


@router.get("/image-import-tasks/{task_id}", summary="获取镜像导入详情")
@auth_with_renew()
def get_image_import_task_api(
    request: Request,
    task_id: int = Path(..., description="镜像导入任务 ID"),
):
    task = find_image_import_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="镜像导入任务不存在")
    return task


@router.post("/image-import-tasks/{task_id}/retry", summary="重试失败镜像")
@auth_with_renew()
def retry_image_import_task_api(
    request: Request,
    task_id: int = Path(..., description="镜像导入任务 ID"),
):
    task = find_image_import_task(task_id, include_file_path=True)
    if task is None:
        raise HTTPException(status_code=404, detail="镜像导入任务不存在")
    if task.get("cleanup_pending"):
        from core.orm.image_import import request_cleanup_retry

        request_cleanup_retry(task_id)
        return find_image_import_task(task_id, include_items=False)
    if task["status"] in {"uploading", "pending", "processing"}:
        raise HTTPException(status_code=409, detail="镜像导入任务正在处理")
    if not os.path.exists(str(task.get("file_path") or "")):
        raise HTTPException(status_code=410, detail="原始镜像文件已不存在")
    if not requeue_image_import_task(task_id):
        raise HTTPException(
            status_code=409, detail="镜像导入任务已被其他请求调度"
        )
    return find_image_import_task(task_id, include_items=False)
