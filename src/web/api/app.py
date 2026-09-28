"""应用管理接口
"""

from typing import Any, Optional
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query, Request
import yaml
from core.orm.app import AppSchema, find_applications_paginated, remove_application_by_id, find_application_by_id, create_application, update_application
from core.orm.task import APP_CLEANUP_TASK, APP_DEPLOY_TASK, create_task_record
from core.services.app_deployment import _helm_command, _notify
from web.utils.auth import auth_with_renew, User
from core.orm.submission import submission_identity, replay_submission, record_submission
from core.orm.cluster import ClusterSchema, ClusterStatus, find_cluster_by_id, update_cluster_name, create_cluster, find_clusters_paginated
from core.orm.engine import get_db
from sqlalchemy import text
from web.utils.page import PageParams, pagination_params
from core.logger import get_logger
from web.utils.response import error_response
router = APIRouter()
logger = get_logger(__name__)


@router.get("/list", summary="获取应用配置")
@auth_with_renew()
def list_apps(request: Request,
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
def get_app(request: Request, app_id: int = Path(..., description="应用id")):
    return find_application_by_id(app_id)


@router.delete("/del/{app_id}", summary="删除应用")
@auth_with_renew()
def delete(request: Request, app_id: str = Path(..., description="应用id")):
    return remove_application_by_id(app_id)


@router.post("/add", summary="创建新应用")
@auth_with_renew()
def create_app(request: Request, app_in: AppSchema):
    """创建新应用（含关联的集群/环境配置项）"""
    return create_application(app_in)


@router.put("/update", summary="更新应用")
@auth_with_renew()
def update_app(request: Request, app_in: AppSchema):
    """更新应用（含关联的集群/环境配置项）"""
    return update_application(app_in)


@router.post("/deploy", summary="部署应用")
@auth_with_renew()
def deploy(request: Request, data: ClusterSchema, background_tasks: BackgroundTasks, current_user: User = None):
    if not data.name:
        raise HTTPException(status_code=500, detail="集群名称(name)不能为空")
    if not data.helm_chart:
        raise HTTPException(status_code=500, detail="Helm Chart(helm_chart)不能为空")
    if not data.helm_chart_version:
        raise HTTPException(status_code=500, detail="Chart版本(helm_chart_version)不能为空")
    identity = submission_identity(current_user.username if current_user else "", request.headers.get("Idempotency-Key"), data)
    # Intent, resource and task are committed together, including replay keys.
    with get_db() as db:
        from core.orm.cluster import Cluster
        db.execute(text("BEGIN IMMEDIATE"))
        existing = replay_submission(db, identity)
        if existing is not None:
            return existing
        cluster = create_cluster(data, db=db)
        create_task_record(APP_DEPLOY_TASK, {"cluster_id": cluster.cluster_id}, cluster.cluster_id, db=db)
        cluster = ClusterSchema.model_validate(db.get(Cluster, cluster.cluster_id))
        record_submission(db, identity, cluster)
        db.commit()
    _notify({"action": "refresh_clusters"})
    return cluster


@router.post("/cluster/{cluster_id}/retry", summary="重试应用部署")
@auth_with_renew()
def retry_cluster(request: Request, cluster_id: int):
    with get_db() as db:
        from core.orm.cluster import Cluster
        db.execute(text("BEGIN IMMEDIATE"))
        cluster = db.get(Cluster, cluster_id)
        if cluster is None:
            raise HTTPException(status_code=404, detail="应用集群不存在")
        if cluster.status in {ClusterStatus.cleaning, ClusterStatus.anomaly}:
            raise HTTPException(status_code=409, detail="应用正在清理或清理异常，不能恢复部署")
        from core.orm.task import Task, TaskStatus
        cleanup = db.query(Task).filter_by(resource_id=cluster_id, task_func_path=APP_CLEANUP_TASK).filter(
            Task.status.in_([TaskStatus.pending, TaskStatus.running]),
        ).all()
        if any(isinstance(task.params, dict) and task.params.get("resource_version") == cluster.operation_version for task in cleanup):
            raise HTTPException(status_code=409, detail="已有清理请求，不能重新部署")
        task = create_task_record(APP_DEPLOY_TASK, {"cluster_id": cluster_id}, cluster_id, db=db)
        db.commit()
        return task


@router.get("/cluster", summary="获取集群配置")
@auth_with_renew()
def cluster(request: Request,
                  pagination: PageParams = Depends(pagination_params),
                  name: Optional[str] = Query(None, description="模糊匹配名称"),):
    return find_clusters_paginated(
        page=pagination.page,
        page_size=pagination.page_size,
        filters={"name": name}
    )


@router.get("/cluster/{cluster_id}", summary="获取集群信息")
@auth_with_renew()
def get_cluster_by_id(request: Request, cluster_id: int = Path(..., description="集群id")):
    return find_cluster_by_id(cluster_id)


@router.get("/clusterInfo/{cluster_id}", summary="获取集群资源详情(helm 资源)")
@auth_with_renew()
def cluster_info(request: Request, cluster_id: int = Path(..., description="集群id")):
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
def delete_cluster(request: Request, background_tasks: BackgroundTasks, cluster_ip: int = Path(..., description="集群id")):
    try:
        create_task_record(APP_CLEANUP_TASK, {"cluster_id": cluster_ip}, cluster_ip)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="应用集群不存在") from exc
    return "processing"
