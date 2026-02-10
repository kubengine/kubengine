"""
Kubernetes 集群管理 API 路由模块

提供与 Kubernetes Dashboard API 和 Longhorn 存储系统交互的 HTTP API 接口，支持：
- 节点信息查询
- 集群总览数据获取（包含 CPU、内存、Pod、存储容量等指标）
- 资源列表查询（Pod、Service、Deployment、StatefulSet 等）
- 资源详情查询
- 资源关联的 Pod 列表查询
"""

import asyncio
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Path, Query, Request

from core.http_api_client.dashboard_client import DashboardClient
from core.http_api_client.longhorn_client import LonghornClient
from core.logger import get_logger
from web.utils.auth import auth_with_renew
from web.utils.page import PageParams, pagination_params

logger = get_logger(__name__)

router = APIRouter(tags=["Kubernetes 集群管理"])


# ============================ 节点管理 ============================


@router.get(
    "/node",
    summary="获取节点信息",
    description="获取指定 Kubernetes 节点的详细信息",
)
@auth_with_renew()
async def get_node(
    request: Request,
    name: str = Query(..., description="节点名称"),
):
    """
    获取指定节点的详细信息

    通过 Dashboard API 查询指定 Kubernetes 节点的详细信息，包括节点状态、
    资源分配、标签等元数据信息。

    Args:
        request: FastAPI 请求对象
        name: 节点名称，用于标识要查询的具体节点

    Returns:
        节点详细信息，包含状态、资源分配、标签等数据
    """
    client = DashboardClient()
    return client.node(name=name)


@router.get(
    "/overview",
    summary="获取集群总览数据",
    description="获取集群监控大屏所需的综合数据，包括节点指标和存储容量",
)
@auth_with_renew()
async def get_overview(request: Request):
    """
    获取集群总览数据，包括节点指标和存储容量

    该接口提供集群监控大屏所需的综合数据，并发获取以下信息：
    1. CPU 使用率趋势数据
    2. 内存使用率趋势数据
    3. 集群总 CPU/内存资源分配情况
    4. 所有节点的资源分配详情
    5. 集群 Pod 分配统计
    6. 存储容量信息（通过 Longhorn）

    使用 asyncio.gather() 实现并发请求，提高响应速度。

    Args:
        request: FastAPI 请求对象

    Returns:
        包含以下字段的综合数据：
            - cpuUsageRate: CPU 使用率时间序列数据列表
            - memoryUsageRate: 内存使用率时间序列数据列表
            - totalCpuRequests: 集群 CPU 请求总量
            - totalCpuCapacity: 集群 CPU 总容量
            - totalMemoryRequests: 集群内存请求总量（字节）
            - totalMemoryCapacity: 集群内存总容量（字节）
            - totalAllocatedPods: 已分配 Pod 总数
            - totalPodCapacity: Pod 总容量
            - nodes: 节点列表，每个节点包含资源分配详情
            - storage_capacity: 存储容量信息
    """

    async def get_capacity() -> tuple[int, str, dict[str, Any]]:
        """
        异步获取存储容量信息

        通过 Longhorn 客户端获取存储系统的容量数据，包括可用空间、
        已用空间等存储指标。

        Returns:
            (code, message, data) 格式的存储容量响应
        """
        client = LonghornClient()
        return client.capacity()

    async def process_node() -> tuple[int, str, dict[str, Any]]:
        """
        处理节点数据并计算集群统计信息

        该函数执行以下操作：
        1. 从 Dashboard API 获取原始节点数据
        2. 解析并转换 CPU 和内存使用率的时间序列数据
        3. 计算集群级别的 CPU、内存、Pod 资源汇总统计
        4. 转换节点列表数据格式，提取关键字段并保留 2 位小数

        Returns:
            (code, message, data) 格式的处理结果
        """
        client = DashboardClient()
        code, msg, data = client.node()

        if code != 200:
            return code, msg, data

        result_data: dict[str, Any] = {}

        # 获取累积指标数据，包含 CPU 和内存的时间序列数据
        cumulative_metrics = data.get("cumulativeMetrics", [])

        # 遍历所有指标，提取 CPU 使用率和内存使用率数据
        for metric in cumulative_metrics:
            metric_name = metric.get("metricName", "")
            data_points = metric.get("dataPoints", [])

            # 处理 CPU 使用率指标
            if metric_name == "cpu/usage_rate":
                cpu_usage_rate = []
                for data_point in data_points:
                    timestamp = data_point.get("x", 0)
                    value = data_point.get("y", 0)
                    cpu_usage_rate.append({
                        "x": datetime.fromtimestamp(timestamp).strftime("%H:%M"),
                        "y": round(value / 1000, 2),  # 毫秒 -> 秒
                        "type": "CPU Usage"
                    })
                result_data["cpuUsageRate"] = cpu_usage_rate

            # 处理内存使用率指标
            elif metric_name == "memory/usage":
                memory_usage_rate = []
                for data_point in data_points:
                    timestamp = data_point.get("x", 0)
                    value = data_point.get("y", 0)
                    memory_usage_rate.append({
                        "x": datetime.fromtimestamp(timestamp).strftime("%H:%M"),
                        "y": round(value / 1024 / 1024 / 1024, 2),  # 字节 -> GB
                        "type": "Memory Usage"
                    })
                result_data["memoryUsageRate"] = memory_usage_rate

        # 获取所有节点列表
        nodes = data.get("nodes", [])

        # 计算集群级别的资源汇总数据
        total_cpu_requests = 0
        total_cpu_capacity = 0
        total_memory_requests = 0
        total_memory_capacity = 0
        total_allocated_pods = 0
        total_pod_capacity = 0

        for node in nodes:
            allocated_resources = node.get("allocatedResources", {})
            total_cpu_requests += allocated_resources.get("cpuRequests", 0)
            total_cpu_capacity += allocated_resources.get("cpuCapacity", 0)
            total_memory_requests += allocated_resources.get(
                "memoryRequests", 0)
            total_memory_capacity += allocated_resources.get(
                "memoryCapacity", 0)
            total_allocated_pods += allocated_resources.get("allocatedPods", 0)

        # Pod 总容量（取最后一个节点的 podCapacity）
        if nodes:
            total_pod_capacity = nodes[-1].get(
                "allocatedResources", {}).get("podCapacity", 0)

        # 转换节点数据格式，提取并重组关键字段
        new_nodes = []
        for node in nodes:
            object_meta = node.get("objectMeta", {})
            allocated_resources = node.get("allocatedResources", {})
            new_node = {
                "key": object_meta.get("name", ""),
                "name": object_meta.get("name", ""),
                "labels": object_meta.get("labels", {}),
                "creationTimestamp": object_meta.get("creationTimestamp", ""),
                "ready": node.get("ready", False),
                # CPU 相关指标
                "cpuRequests": allocated_resources.get("cpuRequests", 0),
                "cpuRequestsFraction": round(allocated_resources.get("cpuRequestsFraction", 0), 2),
                "cpuLimits": allocated_resources.get("cpuLimits", 0),
                "cpuLimitsFraction": round(allocated_resources.get("cpuLimitsFraction", 0), 2),
                "cpuCapacity": allocated_resources.get("cpuCapacity", 0),
                # 内存相关指标
                "memoryRequests": allocated_resources.get("memoryRequests", 0),
                "memoryRequestsFraction": round(allocated_resources.get("memoryRequestsFraction", 0), 2),
                "memoryLimits": allocated_resources.get("memoryLimits", 0),
                "memoryLimitsFraction": round(allocated_resources.get("memoryLimitsFraction", 0), 2),
                "memoryCapacity": allocated_resources.get("memoryCapacity", 0),
                # Pod 相关指标
                "allocatedPods": allocated_resources.get("allocatedPods", 0),
                "podFraction": round(allocated_resources.get("podFraction", 0), 2),
            }
            new_nodes.append(new_node)

        # 将所有计算结果批量更新到结果字典中
        result_data.update({
            "totalCpuRequests": total_cpu_requests,
            "totalCpuCapacity": total_cpu_capacity,
            "totalMemoryRequests": total_memory_requests,
            "totalMemoryCapacity": total_memory_capacity,
            "totalAllocatedPods": total_allocated_pods,
            "totalPodCapacity": total_pod_capacity,
            "nodes": new_nodes
        })

        return code, msg, result_data

    # 使用 asyncio.gather() 并发执行节点数据处理和存储容量获取
    # 提高接口响应速度，避免串行等待
    node_res, capacity_res = await asyncio.gather(process_node(), get_capacity())

    # 检查节点数据处理结果，如果失败则直接返回错误
    if node_res[0] != 200:
        return node_res

    # 检查存储容量获取结果，如果失败则直接返回错误
    if capacity_res[0] != 200:
        return capacity_res

    # 将存储容量数据合并到节点结果中
    node_res[2]["storage_capacity"] = capacity_res[2]

    # 返回合并后的完整结果
    return node_res


# ============================ 资源管理 ============================


@router.get(
    "/dashboard/resource/{type}",
    summary="查询资源列表",
    description="通过 Dashboard API 获取 Kubernetes 资源列表，支持分页和过滤",
)
@auth_with_renew()
async def get_resource_list(
    request: Request,
    type: str = Path(...,
                     description="资源类型（如 pod、service、deployment、statefulset 等）"),
    pagination: PageParams = Depends(pagination_params),
    name: str | None = Query(None, description="资源名称过滤（如 StatefulSet 名称）"),
    namespace: str | None = Query(None, description="命名空间过滤"),
):
    """
    查询 Kubernetes 资源列表

    通过 Dashboard API 获取指定类型的资源列表，支持分页和过滤功能。
    可查询的资源类型包括：Pod、Service、Deployment、StatefulSet 等。

    Args:
        request: FastAPI 请求对象
        type: 资源类型
        pagination: 分页参数对象
        name: 可选，资源名称过滤
        namespace: 可选，命名空间过滤

    Returns:
        资源列表数据，包含分页信息
    """
    client = DashboardClient()

    # 构建过滤条件，如果指定了 name 参数则设置 filterBy
    filter_by = None
    if name is not None:
        filter_by = f"name,{name}"  # 格式为 "name,资源名称"

    # 调用 Dashboard API 获取资源列表
    return client.resource_list(
        resource_type=type,
        namespace=namespace,
        items_per_page=pagination.page_size,
        page=pagination.page,
        filter_by=filter_by
    )


@router.get(
    "/dashboard/resourcedetail/{type}/{namespace}/{name}",
    summary="查询资源详情",
    description="通过 Dashboard API 获取指定资源的完整配置信息",
)
@auth_with_renew()
async def get_resource_detail(
    request: Request,
    type: str = Path(..., description="资源类型"),
    name: str = Path(..., description="资源名称"),
    namespace: str = Path(..., description="命名空间"),
):
    """
    查询指定 Kubernetes 资源的详细信息

    通过 Dashboard API 获取指定资源的完整配置信息，包括：
    - 元数据（metadata）
    - 规格信息（spec）
    - 状态信息（status）
    - 事件记录等

    Args:
        request: FastAPI 请求对象
        type: 资源类型
        name: 资源名称
        namespace: 命名空间

    Returns:
        资源详情数据，包含完整配置对象
    """
    client = DashboardClient()
    return client.resource_detail(type, namespace, name)


@router.get(
    "/dashboard/resourcepod/{type}/{namespace}/{name}",
    summary="查询资源关联的 Pod 列表",
    description="通过 Dashboard API 获取与指定资源相关联的 Pod 列表",
)
@auth_with_renew()
async def get_resource_pod(
    request: Request,
    type: str = Path(..., description="父资源类型（如 deployment、statefulset）"),
    name: str = Path(..., description="父资源名称"),
    namespace: str = Path(..., description="命名空间"),
    pagination: PageParams = Depends(pagination_params),
):
    """
    查询指定资源关联的 Pod 列表

    通过 Dashboard API 获取与指定资源相关联的 Pod 列表。
    例如：查询 Deployment 管理的所有 Pod，或 StatefulSet 管理的所有 Pod。

    Args:
        request: FastAPI 请求对象
        type: 父资源类型
        name: 父资源名称
        namespace: 命名空间
        pagination: 分页参数对象

    Returns:
        Pod 列表数据，包含分页信息和 Pod 详情
    """
    client = DashboardClient()
    return client.resource_pod(
        resource_type=type,
        namespace=namespace,
        resource_name=name,
        items_per_page=pagination.page_size,
        page=pagination.page
    )
