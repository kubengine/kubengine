"""应用管理接口
"""

import json
import re
import tempfile
from typing import Any, Optional
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query, Request
import yaml
from core.command import execute_command
from core.config.application import Application
from core.config.config_dict import ConfigDict
from core.http_api_client.helm_resource_check import HelmResourceChecker
from core.misc.time import pendulum_sleep
from core.orm.app import AppSchema, find_applications_paginated, remove_application_by_id, find_application_by_id, create_application, update_application
from core.orm.task import APP_CLEANUP_TASK, APP_DEPLOY_TASK, create_task_record, execute_task_function
from web.utils.auth import auth_with_renew
from core.orm.cluster import ClusterSchema, ClusterStatus, find_cluster_by_id, remove_cluster_by_id, update_cluster_name, update_cluster_status, create_cluster, find_clusters_paginated
from web.utils.page import PageParams, pagination_params
from core.misc.websocket import connection_manager
from core.logger import get_logger, with_log_context
from web.utils.response import error_response
router = APIRouter()
logger = get_logger(__name__)


@router.get("/list", summary="获取应用配置")
@auth_with_renew()
async def list_apps(request: Request,
               pagination: PageParams = Depends(pagination_params),
               name: Optional[str] = Query(None, description="模糊匹配名称"),
               category: Optional[str] = Query(None, description="按分类筛选")):
    return find_applications_paginated(
        page=pagination.page,
        page_size=pagination.page_size,
        filters={"name": name, "category": category}
    )


@router.get("/get/{app_id}", summary="获取应用配置")
@auth_with_renew()
async def get_app(request: Request, app_id: int = Path(..., description="应用id")):
    return find_application_by_id(app_id)


@router.delete("/del/{app_id}", summary="删除应用")
@auth_with_renew()
async def delete(request: Request, app_id: str = Path(..., description="应用id")):
    return remove_application_by_id(app_id)


@router.post("/add", summary="创建新应用")
@auth_with_renew()
async def create_app(request: Request, app_in: AppSchema):
    """创建新应用（含关联的集群/环境配置项）"""
    return create_application(app_in)


@router.put("/update", summary="更新应用")
@auth_with_renew()
async def update_app(request: Request, app_in: AppSchema):
    """更新应用（含关联的集群/环境配置项）"""
    return update_application(app_in)


@with_log_context(task_id="task_id", cluster_id="cluster_id")
def deploy_app(task_id: int, cluster_id: int):
    logger.info("部署集群: %s", cluster_id)
    previous = find_cluster_by_id(cluster_id)
    if previous and previous.status in {ClusterStatus.cleaning.value, ClusterStatus.anomaly.value}:
        # An older deployment task must not reverse a later cleanup request.
        # Keep the cleanup state intact, outside the deployment failure handler.
        raise RuntimeError("集群正在清理或清理异常，拒绝恢复旧部署任务；保留清理状态并请人工核实")
    try:
        cluster = update_cluster_status(cluster_id, ClusterStatus.creating)
        _notify_cluster(cluster)
        release_name = cluster.helm_name or ""
        if not release_name:
            raise RuntimeError("集群缺少 Helm release 名称")
        existing = _find_helm_release(release_name)
        if existing:
            if existing.get("status") != "deployed":
                raise RuntimeError("已有 Helm release 未完成部署，请检查后重试；保留资源及管理记录")
        else:
            # Unique 0600 file, removed on every normal or exceptional exit.
            with tempfile.NamedTemporaryFile(prefix="kubengine-values-", suffix=".yaml") as values_file:
                ConfigDict(cluster.helm_config or {}).save_to_file(values_file.name)
                _helm_command([
                    "install", release_name,
                    f"oci://{Application.DOMAIN}/charts/{cluster.helm_chart}",
                    "--version", cluster.helm_chart_version or "",
                    "-n", "apps", "--create-namespace", "--timeout", "5m",
                    "-f", values_file.name,
                ])

        cluster = update_cluster_status(cluster_id, ClusterStatus.checking)
        _notify_cluster(cluster)
        checker = HelmResourceChecker(namespace="apps", release_name=release_name)
        for attempt in range(2):
            if not checker.check_pods_with_polling()["status"]:
                raise RuntimeError(f"集群 {cluster_id} 的资源未通过健康检查")
            if attempt == 0:
                pendulum_sleep(2, 1)
        cluster = update_cluster_status(cluster_id, ClusterStatus.healthy)
        _notify_cluster(cluster)
    except Exception:
        logger.exception("集群 %s 部署失败", cluster_id)
        _mark_cluster_failed(cluster_id, ClusterStatus.unhealthy)
        raise


@with_log_context(task_id="task_id", cluster_id="cluster_id")
def clean_up_cluster(task_id: int, cluster_id: int):
    logger.info("清理集群资源: %s", cluster_id)
    try:
        cluster = find_cluster_by_id(cluster_id)
        if cluster is None:
            return  # A previously completed cleanup is safe to recover.
        if not cluster.helm_name:
            raise RuntimeError("集群缺少 Helm release 名称，无法确认资源已清理")
        existing = _find_helm_release(cluster.helm_name)
        if existing is None:
            if cluster.status != ClusterStatus.pending.value:
                raise RuntimeError(
                    "Helm release 记录不存在，但不能确认 Kubernetes 资源已清理；"
                    "保留管理记录，请人工核实残留资源后处理"
                )
        else:
            if existing.get("status") == "uninstalled":
                raise RuntimeError(
                    "Helm release 已标记卸载，但不能确认资源已清理；"
                    "保留管理记录，请人工核实残留资源后处理"
                )
            # Persist before invoking Helm: it may purge release history even
            # when waiting for deletion fails, or the process may crash.
            cluster = update_cluster_status(cluster_id, ClusterStatus.cleaning)
            _notify_cluster(cluster)
            _helm_command([
                "uninstall", cluster.helm_name, "-n", "apps",
                "--wait", "--cascade", "foreground", "--timeout", "5m",
            ])
        remove_cluster_by_id(cluster_id)
        _notify({"action": "refresh_clusters"})
    except Exception:
        logger.exception("集群 %s 清理失败，保留管理记录", cluster_id)
        _mark_cluster_failed(cluster_id, ClusterStatus.anomaly)
        raise


def _helm_command(arguments: list[str]):
    result = execute_command(
        ["helm", *arguments], timeout=360,
        env={"KUBECONFIG": "/etc/kubernetes/admin.conf"},
    )
    if result.is_failure():
        raise RuntimeError(f"Helm 操作失败: {result.get_error_lines()}")
    return result


def _find_helm_release(release_name: str) -> Optional[dict[str, Any]]:
    # --ignore-not-found can swallow history-query errors in Helm. Inspect
    # structured discovery instead, without treating command failure as absence.
    result = _helm_command([
        "list", "--all", "-n", "apps", "--filter",
        f"^{re.escape(release_name)}$", "--output", "json",
    ])
    releases = json.loads("\n".join(result.get_output_lines()))
    if (not isinstance(releases, list) or len(releases) > 1
            or any(not isinstance(item, dict) or item.get("name") != release_name
                   or not isinstance(item.get("status"), str) for item in releases)):
        raise RuntimeError("Helm release 查询返回了无效数据")
    return releases[0] if releases else None


def _notify(message: dict[str, Any]) -> None:
    try:
        connection_manager.broadcast_from_thread(message)
    except Exception:
        logger.exception("集群通知发送失败")


def _notify_cluster(cluster: ClusterSchema) -> None:
    try:
        _notify({"action": "update_cluster", "data": cluster.model_dump(mode="json")})
    except Exception:
        logger.exception("集群通知序列化失败")


def _mark_cluster_failed(cluster_id: int, status: ClusterStatus) -> None:
    try:
        _notify_cluster(update_cluster_status(cluster_id, status))
    except Exception:
        logger.exception("无法更新集群 %s 的失败状态", cluster_id)


@router.post("/deploy", summary="部署应用")
@auth_with_renew()
async def deploy(request: Request, data: ClusterSchema, background_tasks: BackgroundTasks):
    if not data.name:
        raise HTTPException(status_code=500, detail="集群名称(name)不能为空")
    if not data.helm_chart:
        raise HTTPException(status_code=500, detail="Helm Chart(helm_chart)不能为空")
    if not data.helm_chart_version:
        raise HTTPException(status_code=500, detail="Chart版本(helm_chart_version)不能为空")
    cluster = create_cluster(data)
    if cluster:
        # 提交后台任务，开始创建资源
        task = create_task_record(APP_DEPLOY_TASK, {
            "cluster_id": cluster.cluster_id}, cluster.cluster_id or -1)
        # 添加后台任务（FastAPI自动异步执行）
        background_tasks.add_task(
            execute_task_function,
            task_id=task.task_id or -1,
            task_func_path=APP_DEPLOY_TASK,
            task_params={"cluster_id": cluster.cluster_id},
        )

        return cluster


@router.get("/cluster", summary="获取集群配置")
@auth_with_renew()
async def cluster(request: Request,
                  pagination: PageParams = Depends(pagination_params),
                  name: Optional[str] = Query(None, description="模糊匹配名称"),):
    return find_clusters_paginated(
        page=pagination.page,
        page_size=pagination.page_size,
        filters={"name": name}
    )


@router.get("/cluster/{cluster_id}", summary="获取集群信息")
@auth_with_renew()
async def get_cluster_by_id(request: Request, cluster_id: int = Path(..., description="集群id")):
    return find_cluster_by_id(cluster_id)


@router.get("/clusterInfo/{cluster_id}", summary="获取集群资源详情(helm 资源)")
@auth_with_renew()
async def cluster_info(request: Request, cluster_id: int = Path(..., description="集群id")):
    cluster = find_cluster_by_id(cluster_id)
    if cluster is None:
        return error_response(f"集群资源[{cluster_id}]不存在", 201, 200)
    res = _helm_command(["get", "manifest", cluster.helm_name or "", "-n", "apps"])
    result: dict[str, Any] = {"helm_name": cluster.helm_name}
    if res.is_success():
        out = res.get_output_lines()
        data = yaml.safe_load_all("\n".join(out))
        for item in data:
            if len(result.get(item['kind'], [])) > 0:
                result.get(item['kind']).append(item)  # type: ignore
            else:
                result.update({item['kind']: []})
                result.get(item['kind']).append(item)  # type: ignore

    return result


@router.put("/cluster/{cluster_id}/name")
@auth_with_renew()
def update_cluster_name_api(request: Request, data: ClusterSchema, cluster_id: int = Path(..., description="集群id")):
    return update_cluster_name(cluster_id, data.name or "")


@router.delete("/cluster/{cluster_ip}", summary="删除集群")
@auth_with_renew()
async def delete_cluster(request: Request, background_tasks: BackgroundTasks, cluster_ip: int = Path(..., description="集群id")):
    # 提交后台任务，开始创建资源
    task = create_task_record(APP_CLEANUP_TASK, {
        "cluster_id": cluster_ip}, cluster_ip)
    # 添加后台任务（FastAPI自动异步执行）
    background_tasks.add_task(
        execute_task_function,
        task_id=task.task_id or -1,
        task_func_path=APP_CLEANUP_TASK,
        task_params={"cluster_id": cluster_ip},
    )
    return "processing"
