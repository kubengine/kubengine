"""应用管理接口
"""

import asyncio
import os
from typing import Any, Optional
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query, Request
import yaml
from core.command import execute_command
from core.config.application import Application
from core.config.config_dict import ConfigDict
from core.http_api_client.helm_resource_check import HelmResourceChecker
from core.misc.time import pendulum_sleep
from core.orm.app import AppSchema, find_applications_paginated, remove_application_by_id, find_application_by_id, create_application, update_application
from core.orm.task import TaskStatus, create_task_record, update_task_record_status
from web.utils.auth import auth_with_renew
from core.orm.cluster import ClusterSchema, ClusterStatus, find_cluster_by_id, remove_cluster_by_id, update_cluster_name, update_cluster_status, create_cluster, find_clusters_paginated
from web.utils.page import PageParams, pagination_params
from core.misc.websocket import connection_manager
from core.logger import get_logger
from web.utils.response import error_response
router = APIRouter()
logger = get_logger(__name__)


@router.get("/list", summary="获取应用配置")
@auth_with_renew()
async def list(request: Request,
               pagination: PageParams = Depends(pagination_params),
               name: Optional[str] = Query(None, description="模糊匹配名称")):
    return find_applications_paginated(
        page=pagination.page,
        page_size=pagination.page_size,
        filters={"name": name}
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


def deploy_app(task_id: int, cluster_id: int):
    logger.info(f"部署集群:{cluster_id}")
    update_task_record_status(task_id, TaskStatus.running)

    try:
        cluster = update_cluster_status(cluster_id, ClusterStatus.creating)
        asyncio.run(connection_manager.broadcast(
            {"action": "update_cluster", "data": cluster.model_dump()}))
        # helm创建资源
        pendulum_sleep(2, 1)
        values_file_path = f"/tmp/cluster_{cluster.cluster_id}_values.yaml"
        try:
            config = ConfigDict(cluster.helm_config)
            config.save_to_file(values_file_path)
            cmds = ["KUBECONFIG=/etc/kubernetes/admin.conf helm",
                    "install",
                    cluster.helm_name or "",
                    f"oci://{Application.DOMAIN}/charts/{cluster.helm_chart}",
                    "--version", cluster.helm_chart_version or "",
                    "-n", "apps",
                    "--create-namespace",
                    "-f", values_file_path]
            res = execute_command(" ".join(cmds))
            if res.is_failure():
                raise HTTPException(
                    status_code=500, detail=f"部署集群:{cluster_id}失败 {res.get_error_lines()}")
            pendulum_sleep(2, 1)
            # 广播通知集群信息
            cluster = update_cluster_status(
                cluster_id, ClusterStatus.checking)
            asyncio.run(connection_manager.broadcast(
                {"action": "update_cluster", "data": cluster.model_dump()}))
            # 检查状态
            pendulum_sleep(2, 1)
            official_checker = HelmResourceChecker(
                namespace="apps",
                release_name=cluster.helm_name or ""
            )

            release_status = official_checker.check_pods_with_polling()
            status = ClusterStatus.healthy if release_status['status'] else ClusterStatus.unhealthy
            if status == ClusterStatus.healthy:
                # 再检查一遍，防止有新pod创建
                pendulum_sleep(2, 1)
                release_status = official_checker.check_pods_with_polling()
                status = ClusterStatus.healthy if release_status[
                    'status'] else ClusterStatus.unhealthy

            cluster = update_cluster_status(cluster_id, status)
            asyncio.run(connection_manager.broadcast(
                {"action": "update_cluster", "data": cluster.model_dump()}))
        except Exception as e:
            raise e
        finally:
            os.remove(values_file_path)

    except Exception as e:
        # 捕捉到异常，集群创建失败
        logger.info(f"集群创建失败 {e}")
        cluster = update_cluster_status(cluster_id, ClusterStatus.unhealthy)
        asyncio.run(connection_manager.broadcast(
            {"action": "update_cluster", "data": cluster.model_dump()}))
    finally:
        update_task_record_status(task_id, TaskStatus.success)


def clean_up_cluster(task_id: int, cluster_id: int):
    logger.info(f"清理集群资源:{cluster_id}")
    update_task_record_status(task_id, TaskStatus.running)
    try:
        cluster = find_cluster_by_id(cluster_id)
        if cluster and cluster.status != ClusterStatus.anomaly.value:
            cmds = ["KUBECONFIG=/etc/kubernetes/admin.conf helm",
                    "uninstall", cluster.helm_name or "", "-n", "apps"]
            res = execute_command(" ".join(cmds))
            if res.is_failure():
                raise HTTPException(
                    status_code=500, detail=f"清理集群资源:{cluster_id}失败  {res.get_error_lines()}")
        remove_cluster_by_id(cluster_id)
        asyncio.run(connection_manager.broadcast(
            {"action": "refresh_clusters"}))
    except Exception as e:
        # 捕捉到异常，集群资源清理失败
        logger.info(f"集群资源清理失败 {e}")
        cluster = update_cluster_status(cluster_id, ClusterStatus.anomaly)
        asyncio.run(connection_manager.broadcast(
            {"action": "update_cluster", "data": cluster.model_dump()}))
    finally:
        update_task_record_status(task_id, TaskStatus.success)


@router.post("/deploy", summary="部署应用")
@auth_with_renew()
async def deploy(request: Request, data: ClusterSchema, background_tasks: BackgroundTasks):
    cluster = create_cluster(data)
    if cluster:
        # 提交后台任务，开始创建资源
        task = create_task_record("api.app.deploy_app", {
            "cluster_id": cluster.cluster_id}, cluster.cluster_id or -1)
        # 添加后台任务（FastAPI自动异步执行）
        background_tasks.add_task(
            deploy_app,
            task_id=task.task_id or -1,
            cluster_id=cluster.cluster_id or -1
        )

        return data


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
    res = execute_command(
        f"KUBECONFIG=/etc/kubernetes/admin.conf helm get manifest {cluster.helm_name} -n apps")
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
    task = create_task_record("api.app.deploy_app", {
        "cluster_id": cluster_ip}, cluster_ip)
    # 添加后台任务（FastAPI自动异步执行）
    background_tasks.add_task(
        clean_up_cluster,
        task_id=task.task_id or -1,
        cluster_id=cluster_ip or -1
    )
    return "processing"
