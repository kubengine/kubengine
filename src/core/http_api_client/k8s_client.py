"""
Kubernetes 客户端模块

使用官方 Kubernetes Python SDK 直接与 Kubernetes API 集群交互，提供资源管理、监控等功能。
"""

import os
from typing import Any, Dict, List, Optional, Tuple

import yaml
from kubernetes import client, config
from kubernetes.client.rest import ApiException

from core.command import execute_command
from core.config import Application
from core.logger import get_logger

logger = get_logger(__name__)


class K8sClient:
    """
    Kubernetes 客户端类

    使用官方 Kubernetes Python SDK 与 Kubernetes API 交互，提供：
    - 命名空间管理
    - 节点管理
    - Pod 管理
    - 资源管理（Deployment、StatefulSet、DaemonSet 等）
    - IP 池配置
    - 节点污点管理
    - Metrics 数据获取

    Attributes:
        core_v1: Kubernetes Core V1 API 客户端
        apps_v1: Kubernetes Apps V1 API 客户端
        storage_v1: Kubernetes Storage V1 API 客户端
        custom_api: Kubernetes Custom Objects API 客户端
    """

    _logger = logger

    # 不需要命名空间的资源类型列表
    NO_NAMESPACE_RESOURCES = [
        "persistentvolume",
        "persistentvolumeclaim",
        "storageclass",
    ]

    def __init__(
        self,
        kubeconfig_path: Optional[str] = None,
        manifest_dir: Optional[str] = None,
    ) -> None:
        """
        初始化 Kubernetes 客户端

        Args:
            kubeconfig_path: kubeconfig 文件路径
            manifest_dir: 配置清单目录路径
        """
        self.manifest_dir = manifest_dir or os.path.join(
            Application.ROOT_DIR, "config", "manifest"
        )

        # 加载 Kubernetes 配置
        try:
            # 尝试使用集群内配置（运行在 Pod 中）
            config.load_incluster_config()
            self._logger.info("Loaded in-cluster Kubernetes configuration")
        except config.ConfigException:
            # 如果不是在集群中运行，使用 kubeconfig 文件
            try:
                if kubeconfig_path:
                    config.load_kube_config(config_file=kubeconfig_path)
                else:
                    # 尝试默认路径
                    default_kubeconfig = "/etc/kubernetes/admin.conf"
                    if os.path.exists(default_kubeconfig):
                        config.load_kube_config(config_file=default_kubeconfig)
                    else:
                        # 尝试用户默认 kubeconfig
                        config.load_kube_config()
                self._logger.info("Loaded kubeconfig Kubernetes configuration")
            except Exception as e:
                self._logger.error(
                    f"Failed to load Kubernetes configuration: {e}")
                raise

        # 初始化 Kubernetes API 客户端
        self.core_v1 = client.CoreV1Api()
        self.apps_v1 = client.AppsV1Api()
        self.storage_v1 = client.StorageV1Api()
        self.custom_api = client.CustomObjectsApi()

    def _api_result(
        self, data: Any, error_msg: str = "Success"
    ) -> Tuple[int, str, Any]:
        """
        统一格式化返回结果

        Args:
            data: 返回的数据
            error_msg: 成功时的消息

        Returns:
            标准化响应结果 (code, message, data)
        """
        return 200, error_msg, data

    def _handle_exception(
        self, e: ApiException, operation: str
    ) -> Tuple[int, str, Dict[str, Any]]:
        """
        处理 Kubernetes API 异常

        Args:
            e: API 异常对象
            operation: 操作描述

        Returns:
            标准化错误响应结果
        """
        status_code: int = e.status if e.status else 500  # type: ignore[assignment]
        # type: ignore[annotation-unchecked]
        error_msg: str = f"Failed to {operation}: {e.status} - {e.reason}"
        if e.body:
            try:
                body: dict[str, Any] = e.body if isinstance(  # type: ignore[assignment]
                    e.body, dict) else {}
                error_detail = body.get("message", "")
                error_msg = f"Failed to {operation}: {error_detail}"
            except Exception:
                pass
        self._logger.error(error_msg)
        return status_code, error_msg, {}  # type: ignore

    def get_namespace(self) -> Tuple[int, str, Dict[str, List[str]]]:
        """
        获取所有命名空间列表

        Returns:
            标准化响应结果 (code, message, data)
        """
        try:
            namespaces = self.core_v1.list_namespace()  # type: ignore
            namespace_list = [  # type: ignore
                ns.metadata.name for ns in namespaces.items]  # type: ignore
            return self._api_result({"namespaces": namespace_list})
        except ApiException as e:
            return self._handle_exception(e, "get namespaces")

    def node(
        self, name: Optional[str] = None
    ) -> Tuple[int, str, Any]:
        """
        获取节点信息

        Args:
            name: 节点名称，为空则获取所有节点

        Returns:
            标准化响应结果 (code, message, data)
        """
        try:
            if name is None:
                nodes = self.core_v1.list_node()  # type: ignore
                return self._api_result({"nodes": nodes.items})
            else:
                node = self.core_v1.read_node(name=name)  # type: ignore
                return self._api_result(node)
        except ApiException as e:
            return self._handle_exception(e, f"get node {name}")

    def app_pods(
        self,
        name: str,
        items_per_page: int = 100,
        page: int = 1,
        sort_by: str = "d,creationTimestamp",
    ) -> Tuple[int, str, Dict[str, Any]]:
        """
        获取指定应用的 Pod 列表（在 apps 命名空间下）

        Args:
            name: 应用名称
            items_per_page: 每页显示的 Pod 数量
            page: 页码
            sort_by: 排序方式

        Returns:
            标准化响应结果 (code, message, data)
        """
        return self.pod(
            namespace="apps",
            items_per_page=items_per_page,
            page=page,
            sort_by=sort_by,
            filter_by=f"name,{name}",
        )

    def pod(
        self,
        namespace: Optional[str] = None,
        items_per_page: int = 10,
        page: int = 1,
        sort_by: str = "d,creationTimestamp",
        filter_by: Optional[str] = None,
    ) -> Tuple[int, str, Dict[str, Any]]:
        """
        获取 Pod 列表

        Args:
            namespace: 命名空间名称
            items_per_page: 每页显示的 Pod 数量
            page: 页码
            sort_by: 排序方式（注意：k8s SDK 不支持此参数，仅为接口兼容）
            filter_by: 过滤条件，格式如 "name,pod-name"

        Returns:
            标准化响应结果 (code, message, data)
        """
        try:
            # 解析过滤条件
            field_selector = None
            if filter_by:
                filter_parts = filter_by.split(",")
                if len(filter_parts) == 2:
                    if filter_parts[0] == "name":
                        field_selector = f"metadata.name={filter_parts[1]}"
                    elif filter_parts[0] == "namespace":
                        field_selector = f"metadata.namespace={filter_parts[1]}"

            # 获取 Pod 列表
            if namespace:
                pods = self.core_v1.list_namespaced_pod(  # type: ignore
                    namespace=namespace,
                    field_selector=field_selector,
                )
            else:
                pods = self.core_v1.list_pod_for_all_namespaces(  # type: ignore
                    field_selector=field_selector
                )

            # 实现分页（在内存中分页）
            all_pods = pods.items  # type: ignore
            total = len(all_pods)  # type: ignore

            # 计算分页
            start_idx = (page - 1) * items_per_page
            end_idx = start_idx + items_per_page
            paginated_pods = all_pods[start_idx:end_idx]  # type: ignore

            return self._api_result(
                {
                    "pods": paginated_pods,
                    "total": total,
                    "items_per_page": items_per_page,
                    "page": page,
                }
            )
        except ApiException as e:
            return self._handle_exception(e, "get pods")

    def get_ip_pool(self) -> Tuple[int, str, Dict[str, Any]]:
        """
        获取 IP 池配置信息

        从本地 manifest 文件读取 MetalLB 的 IP 池配置。

        Returns:
            标准化响应结果 (code, message, data)
        """
        manifests = os.path.join(self.manifest_dir, "metallb-ippool.yaml")
        if not os.path.isfile(manifests):
            return 500, f"IP pool manifest file not found: {manifests}", {}

        try:
            with open(manifests, "r", encoding="utf-8") as f:
                ip_pool_data = yaml.safe_load(f)
            return self._api_result(ip_pool_data)
        except Exception as e:
            self._logger.error(f"Failed to read IP pool file: {e}")
            return 500, f"Failed to read IP pool: {str(e)}", {}

    def update_ip_pool(
        self, ip_pools: List[str]
    ) -> Tuple[int, str, Dict[str, Any]]:
        """
        更新 IP 池配置

        更新本地 manifest 文件并应用到集群。

        Args:
            ip_pools: 新的 IP 池地址列表

        Returns:
            标准化响应结果 (code, message, data)
        """
        manifests = os.path.join(self.manifest_dir, "metallb-ippool.yaml")
        if not os.path.isfile(manifests):
            return 500, f"IP pool manifest file not found: {manifests}", {}

        try:
            # 读取现有配置
            with open(manifests, "r", encoding="utf-8") as f:
                ip_pool_data: dict[str, Any] = yaml.safe_load(
                    f)  # type: ignore[assignment]

            # 更新 IP 池地址
            if "spec" not in ip_pool_data:
                ip_pool_data["spec"] = {}
            # type: ignore[assignment]
            spec: dict[str, Any] = ip_pool_data["spec"]
            spec["addresses"] = ip_pools

            # 写入文件
            with open(manifests, "w", encoding="utf-8") as f:
                yaml.dump(
                    ip_pool_data,
                    f,
                    default_flow_style=False,
                    allow_unicode=True,
                )

            # 应用到集群
            cmd = f"KUBECONFIG=/etc/kubernetes/admin.conf kubectl apply -f {manifests}"
            result = execute_command(cmd)
            ret_code: int = result.get("ret", 0)  # type: ignore[assignment]
            if ret_code != 0:
                # type: ignore[assignment]
                err_msg: str = result.get(  # type: ignore
                    "err", "Unknown error")
                return 500, f"Failed to update IP pool: {err_msg}", {}

            return self._api_result({})
        except Exception as e:
            self._logger.error(f"Failed to update IP pool: {e}")
            return 500, f"Failed to update IP pool: {str(e)}", {}

    def resource_list(
        self,
        resource_type: str,
        namespace: Optional[str] = None,
        items_per_page: int = 10,
        page: int = 1,
        sort_by: str = "d,creationTimestamp",
        filter_by: Optional[str] = None,
    ) -> Tuple[int, str, Dict[str, Any]]:
        """
        获取 Kubernetes 资源列表

        支持的资源类型：pod, service, deployment, statefulset, daemonset,
        persistentvolume, persistentvolumeclaim, storageclass 等

        Args:
            resource_type: 资源类型（小写）
            namespace: 命名空间名称
            items_per_page: 每页显示的资源数量
            page: 页码
            sort_by: 排序方式（注意：k8s SDK 不支持此参数，仅为接口兼容）
            filter_by: 过滤条件，格式如 "name,resource-name"

        Returns:
            标准化响应结果 (code, message, data)
        """
        try:
            # 解析过滤条件
            field_selector = None
            if filter_by:
                filter_parts = filter_by.split(",")
                if len(filter_parts) == 2:
                    if filter_parts[0] == "name":
                        field_selector = f"metadata.name={filter_parts[1]}"

            # 根据资源类型调用对应的 API
            result = self._list_resource(
                resource_type, namespace, field_selector
            )

            if result[0] != 200:
                return result  # type: ignore

            items = result[2]
            total = len(items)

            # 实现分页
            start_idx = (page - 1) * items_per_page
            end_idx = start_idx + items_per_page
            paginated_items = items[start_idx:end_idx]

            return self._api_result(
                {
                    resource_type: paginated_items,
                    "total": total,
                    "items_per_page": items_per_page,
                    "page": page,
                }
            )
        except ApiException as e:
            return self._handle_exception(e, f"get {resource_type} list")

    def _list_resource(
        self,
        resource_type: str,
        namespace: Optional[str],
        field_selector: Optional[str],
    ) -> Tuple[int, str, List[Any]]:
        """
        根据资源类型获取列表（内部方法）

        Args:
            resource_type: 资源类型
            namespace: 命名空间
            field_selector: 字段选择器

        Returns:
            资源列表
        """
        items: List[Any] = []  # type: ignore[annotation]
        # Core API 资源
        if resource_type == "pod":
            if namespace:
                items = self.core_v1.list_namespaced_pod(  # type: ignore
                    namespace, field_selector=field_selector
                ).items
            else:
                items = self.core_v1.list_pod_for_all_namespaces(  # type: ignore
                    field_selector=field_selector
                ).items
            return self._api_result(items)

        elif resource_type == "service" or resource_type == "svc":
            if namespace:
                items = self.core_v1.list_namespaced_service(  # type: ignore
                    namespace, field_selector=field_selector
                ).items
            else:
                items = self.core_v1.list_service_for_all_namespaces(  # type: ignore
                    field_selector=field_selector
                ).items
            return self._api_result(items)

        elif resource_type == "namespace":
            items = self.core_v1.list_namespace().items  # type: ignore
            return self._api_result(items)

        elif resource_type == "persistentvolume" or resource_type == "pv":
            items = self.core_v1.list_persistent_volume(  # type: ignore
                field_selector=field_selector
            ).items
            return self._api_result(items)

        elif (resource_type == "persistentvolumeclaim" or resource_type == "pvc"):
            if namespace:
                items = self.core_v1.list_namespaced_persistent_volume_claim(  # type: ignore
                    namespace, field_selector=field_selector
                ).items
            else:
                items = self.core_v1.list_persistent_volume_claim_for_all_namespaces(  # type: ignore
                    field_selector=field_selector
                ).items
            return self._api_result(items)

        elif resource_type == "configmap" or resource_type == "cm":
            if namespace:
                items = self.core_v1.list_namespaced_config_map(  # type: ignore
                    namespace, field_selector=field_selector
                ).items
            else:
                items = self.core_v1.list_config_map_for_all_namespaces(  # type: ignore
                    field_selector=field_selector
                ).items
            return self._api_result(items)

        elif resource_type == "secret":
            if namespace:
                items = self.core_v1.list_namespaced_secret(  # type: ignore
                    namespace, field_selector=field_selector
                ).items
            else:
                items = self.core_v1.list_secret_for_all_namespaces(  # type: ignore
                    field_selector=field_selector
                ).items
            return self._api_result(items)

        elif resource_type == "node":
            items = self.core_v1.list_node(  # type: ignore
                field_selector=field_selector
            ).items
            return self._api_result(items)

        # Apps API 资源
        elif resource_type == "deployment" or resource_type == "deploy":
            if namespace:
                items = self.apps_v1.list_namespaced_deployment(  # type: ignore
                    namespace, field_selector=field_selector
                ).items
            else:
                items = self.apps_v1.list_deployment_for_all_namespaces(  # type: ignore
                    field_selector=field_selector
                ).items
            return self._api_result(items)

        elif resource_type == "statefulset" or resource_type == "sts":
            if namespace:
                items = self.apps_v1.list_namespaced_stateful_set(  # type: ignore
                    namespace, field_selector=field_selector
                ).items
            else:
                items = self.apps_v1.list_stateful_set_for_all_namespaces(  # type: ignore
                    field_selector=field_selector
                ).items
            return self._api_result(items)

        elif resource_type == "daemonset" or resource_type == "ds":
            if namespace:
                items = self.apps_v1.list_namespaced_daemon_set(  # type: ignore
                    namespace, field_selector=field_selector
                ).items
            else:
                items = self.apps_v1.list_daemon_set_for_all_namespaces(  # type: ignore
                    field_selector=field_selector
                ).items
            return self._api_result(items)

        elif resource_type == "replicaset" or resource_type == "rs":
            if namespace:
                items = self.apps_v1.list_namespaced_replica_set(  # type: ignore
                    namespace, field_selector=field_selector
                ).items
            else:
                items = self.apps_v1.list_replica_set_for_all_namespaces(  # type: ignore
                    field_selector=field_selector
                ).items
            return self._api_result(items)

        # Storage API 资源
        elif resource_type == "storageclass" or resource_type == "sc":
            items = self.storage_v1.list_storage_class(  # type: ignore
                field_selector=field_selector
            ).items
            return self._api_result(items)

        else:
            return 400, f"Unsupported resource type: {resource_type}", []

    def resource_detail(
        self,
        resource_type: str,
        namespace: str,
        name: str,
    ) -> Tuple[int, str, Any]:
        """
        获取资源详情

        Args:
            resource_type: 资源类型
            namespace: 命名空间
            name: 资源名称

        Returns:
            标准化响应结果 (code, message, data)
        """
        try:
            # 无命名空间资源
            if resource_type in self.NO_NAMESPACE_RESOURCES:
                return self._get_resource_without_namespace(
                    resource_type, name
                )
            else:
                return self._get_resource_with_namespace(
                    resource_type, namespace, name
                )
        except ApiException as e:
            return self._handle_exception(
                e, f"get {resource_type} {namespace}/{name}"
            )

    def _get_resource_with_namespace(
        self, resource_type: str, namespace: str, name: str
    ) -> Tuple[int, str, Any]:
        """
        获取需要命名空间的资源详情（内部方法）

        Args:
            resource_type: 资源类型
            namespace: 命名空间
            name: 资源名称

        Returns:
            标准化响应结果 (code, message, data)
        """
        # Core API 资源
        if resource_type == "pod":
            return self._api_result(
                self.core_v1.read_namespaced_pod(name, namespace)
            )
        elif resource_type == "service" or resource_type == "svc":
            return self._api_result(
                self.core_v1.read_namespaced_service(name, namespace)
            )
        elif resource_type == "configmap" or resource_type == "cm":
            return self._api_result(
                self.core_v1.read_namespaced_config_map(name, namespace)
            )
        elif resource_type == "secret":
            return self._api_result(
                self.core_v1.read_namespaced_secret(name, namespace)
            )
        elif resource_type == "persistentvolumeclaim" or resource_type == "pvc":
            return self._api_result(
                self.core_v1.read_namespaced_persistent_volume_claim(
                    name, namespace
                )
            )

        # Apps API 资源
        elif resource_type == "deployment" or resource_type == "deploy":
            return self._api_result(
                self.apps_v1.read_namespaced_deployment(name, namespace)
            )
        elif resource_type == "statefulset" or resource_type == "sts":
            return self._api_result(
                self.apps_v1.read_namespaced_stateful_set(name, namespace)
            )
        elif resource_type == "daemonset" or resource_type == "ds":
            return self._api_result(
                self.apps_v1.read_namespaced_daemon_set(name, namespace)
            )
        elif resource_type == "replicaset" or resource_type == "rs":
            return self._api_result(
                self.apps_v1.read_namespaced_replica_set(name, namespace)
            )
        else:
            return (
                400,
                f"Unsupported resource type with namespace: {resource_type}",
                {},
            )

    def _get_resource_without_namespace(
        self, resource_type: str, name: str
    ) -> Tuple[int, str, Any]:
        """
        获取不需要命名空间的资源详情（内部方法）

        Args:
            resource_type: 资源类型
            name: 资源名称

        Returns:
            标准化响应结果 (code, message, data)
        """
        if resource_type == "persistentvolume" or resource_type == "pv":
            return self._api_result(self.core_v1.read_persistent_volume(name))
        elif resource_type == "storageclass" or resource_type == "sc":
            return self._api_result(self.storage_v1.read_storage_class(name))
        elif resource_type == "namespace":
            return self._api_result(self.core_v1.read_namespace(name))
        elif resource_type == "node":
            return self._api_result(self.core_v1.read_node(name))
        else:
            return (
                400,
                f"Unsupported resource type without namespace: {resource_type}",
                {},
            )

    def resource_pod(
        self,
        resource_type: str,
        namespace: str,
        resource_name: str,
        items_per_page: int = 10,
        page: int = 1,
        sort_by: str = "d,creationTimestamp",
    ) -> Tuple[int, str, Dict[str, Any]]:
        """
        获取资源关联的 Pod 列表

        Args:
            resource_type: 资源类型
            namespace: 命名空间
            resource_name: 资源名称
            items_per_page: 每页显示的 Pod 数量
            page: 页码
            sort_by: 排序方式

        Returns:
            标准化响应结果 (code, message, data)
        """
        try:
            # 构建标签选择器来获取关联的 Pod
            label_selector = None

            if resource_type == "deployment" or resource_type == "deploy":
                # Deployment 的 Pod 通常带有 app=<deployment-name> 标签
                label_selector = f"app={resource_name}"
            elif resource_type == "statefulset" or resource_type == "sts":
                # StatefulSet 的 Pod 通常带有 app=<statefulset-name> 标签
                label_selector = f"app={resource_name}"
            elif resource_type == "daemonset" or resource_type == "ds":
                # DaemonSet 的 Pod 通常带有 app=<daemonset-name> 标签
                label_selector = f"app={resource_name}"
            else:
                # 对于其他资源类型，尝试使用通用标签
                label_selector = f"app={resource_name}"

            # 获取 Pod 列表
            pods = self.core_v1.list_namespaced_pod(  # type: ignore
                namespace=namespace,
                label_selector=label_selector,
            )

            # 实现分页
            all_pods = pods.items  # type: ignore
            total = len(all_pods)  # type: ignore

            start_idx = (page - 1) * items_per_page
            end_idx = start_idx + items_per_page
            paginated_pods = all_pods[start_idx:end_idx]  # type: ignore

            return self._api_result(
                {
                    "pods": paginated_pods,
                    "total": total,
                    "items_per_page": items_per_page,
                    "page": page,
                }
            )
        except ApiException as e:
            return self._handle_exception(
                e, f"get pods for {resource_type} {namespace}/{resource_name}"
            )

    def node_taints(self, node_name: str) -> Tuple[int, str, Dict[str, Any]]:
        """
        获取节点污点信息

        Args:
            node_name: 节点名称

        Returns:
            标准化响应结果 (code, message, data)
        """
        try:
            node = self.core_v1.read_node(name=node_name)  # type: ignore
            taints = node.spec.taints or []  # type: ignore
            return self._api_result({"taints": taints})
        except ApiException as e:
            return self._handle_exception(e, f"get taints for node {node_name}")

    def add_node_taint(
        self,
        node_name: str,
        key: str,
        value: str = "",
        effect: str = "NoSchedule",
    ) -> Tuple[int, str, Dict[str, Any]]:
        """
        为节点添加污点

        Args:
            node_name: 节点名称
            key: 污点键
            value: 污点值
            effect: 污点效果

        Returns:
            标准化响应结果 (code, message, data)
        """
        try:
            # 获取当前节点信息
            node = self.core_v1.read_node(name=node_name)  # type: ignore

            # 初始化污点列表（如果不存在）
            if node.spec.taints is None:  # type: ignore
                node.spec.taints = []  # type: ignore

            # 检查污点是否已存在
            for taint in node.spec.taints:  # type: ignore
                if taint.key == key and taint.effect == effect:
                    return (
                        400,
                        f"Taint {key}:{effect} already exists on node {node_name}",
                        {},
                    )

            # 创建新污点
            from kubernetes.client import V1Taint

            new_taint = V1Taint(key=key, value=value, effect=effect)

            # 添加污点
            node.spec.taints.append(new_taint)  # type: ignore

            # 更新节点
            self.core_v1.patch_node(name=node_name, body=node)

            self._logger.info(
                f"Successfully added taint {key}={value}:{effect} to node {node_name}"
            )
            return self._api_result({})
        except ApiException as e:
            return self._handle_exception(e, f"add taint to node {node_name}")

    def remove_node_taint(
        self,
        node_name: str,
        key: str,
        effect: Optional[str] = None,
        value: Optional[str] = None,
    ) -> Tuple[int, str, Dict[str, Any]]:
        """
        删除节点污点

        Args:
            node_name: 节点名称
            key: 污点键
            effect: 污点效果
            value: 污点值

        Returns:
            标准化响应结果 (code, message, data)
        """
        try:
            # 获取当前节点信息
            node = self.core_v1.read_node(name=node_name)  # type: ignore

            if node.spec.taints is None:  # type: ignore
                return 404, f"No taints found on node {node_name}", {}

            # 过滤要保留的污点
            filtered_taints = []
            removed_count = 0
            removed_taints = []

            for taint in node.spec.taints:  # type: ignore
                should_keep = True

                # 检查键是否匹配
                if taint.key == key:
                    # 如果指定了 effect，检查是否匹配
                    if effect is None or taint.effect == effect:
                        # 如果指定了 value，检查是否匹配
                        if value is None or taint.value == value:
                            should_keep = False
                            removed_count += 1
                            removed_taints.append(
                                taint.to_dict())  # type: ignore

                if should_keep:
                    filtered_taints.append(
                        {
                            "key": taint.key,
                            "value": taint.value or "",
                            "effect": taint.effect,
                        }
                    )

            if removed_count == 0:
                return (
                    404,
                    f"No matching taint found with key '{key}' on node {node_name}",
                    {},
                )

            # 构建patch body
            patch_body: dict[str, Any] = {
                "spec": {
                    "taints": filtered_taints if filtered_taints else None
                }
            }

            # 使用 JSON Merge Patch
            self.core_v1.patch_node(name=node_name, body=patch_body)

            self._logger.info(
                f"Successfully removed {removed_count} taint(s) with key '{key}' from node {node_name}"
            )
            return self._api_result(
                {
                    "removed_count": removed_count,
                    "removed_taints": removed_taints,
                }
            )
        except ApiException as e:
            return self._handle_exception(e, f"remove taint from node {node_name}")

    def update_node_taints(
        self, node_name: str, taints: List[Dict[str, Any]]
    ) -> Tuple[int, str, Dict[str, Any]]:
        """
        批量更新节点污点（替换所有污点）

        Args:
            node_name: 节点名称
            taints: 新的污点列表

        Returns:
            标准化响应结果 (code, message, data)
        """
        try:
            # 获取当前节点信息
            node = self.core_v1.read_node(name=node_name)  # type: ignore

            # 验证污点格式并构建污点对象
            from kubernetes.client import V1Taint

            valid_taints = []

            for taint_dict in taints:
                if not isinstance(taint_dict, dict) or "key" not in taint_dict:  # type: ignore
                    return 400, f"Invalid taint format: {taint_dict}", {}

                if "effect" not in taint_dict:
                    taint_dict["effect"] = "NoSchedule"

                # 验证 effect 值
                valid_effects = [
                    "NoSchedule",
                    "PreferNoSchedule",
                    "NoExecute",
                ]
                if taint_dict["effect"] not in valid_effects:
                    return (
                        400,
                        f"Invalid effect '{taint_dict['effect']}', must be one of {valid_effects}",
                        {},
                    )

                taint = V1Taint(
                    key=taint_dict["key"],
                    value=taint_dict.get("value", ""),
                    effect=taint_dict["effect"],
                )
                valid_taints.append(taint)

            # 更新污点列表
            node.spec.taints = valid_taints if valid_taints else None  # type: ignore

            # 更新节点
            self.core_v1.patch_node(name=node_name, body=node)

            self._logger.info(
                f"Successfully updated taints for node {node_name}, total: {len(valid_taints)}"   # type: ignore
            )
            return self._api_result({"total_taints": len(valid_taints)})   # type: ignore
        except ApiException as e:
            return self._handle_exception(e, f"update taints for node {node_name}")

    def pod_metrics(
        self,
        namespace: Optional[str] = None,
        pod_name: Optional[str] = None,
    ) -> Tuple[int, str, Dict[str, Any]]:
        """
        获取 Pod 的 Metrics 数据（CPU 和内存使用情况）

        Args:
            namespace: 命名空间
            pod_name: Pod 名称

        Returns:
            标准化响应结果 (code, message, data)
        """
        try:
            if namespace and pod_name:
                # 获取特定 Pod 的 metrics
                metrics = self.custom_api.get_namespaced_custom_object(  # type: ignore
                    group="metrics.k8s.io",
                    version="v1beta1",
                    namespace=namespace,
                    plural="pods",
                    name=pod_name,
                )
                return self._api_result({"metrics": metrics})
            elif namespace:
                # 获取命名空间下所有 Pod 的 metrics
                metrics = self.custom_api.list_namespaced_custom_object(  # type: ignore
                    group="metrics.k8s.io",
                    version="v1beta1",
                    namespace=namespace,
                    plural="pods",
                )
                return self._api_result({"metrics": metrics})
            else:
                # 获取所有命名空间下 Pod 的 metrics
                metrics = self.custom_api.list_cluster_custom_object(  # type: ignore
                    group="metrics.k8s.io",
                    version="v1beta1",
                    plural="pods",
                )
                return self._api_result({"metrics": metrics})
        except ApiException as e:
            return self._handle_exception(e, "get pod metrics")

    def node_metrics(
        self, node_name: Optional[str] = None
    ) -> Tuple[int, str, Dict[str, Any]]:
        """
        获取节点的 Metrics 数据

        Args:
            node_name: 节点名称

        Returns:
            标准化响应结果 (code, message, data)
        """
        try:
            if node_name:
                # 获取特定节点的 metrics
                metrics = self.custom_api.get_cluster_custom_object(  # type: ignore
                    group="metrics.k8s.io",
                    version="v1beta1",
                    plural="nodes",
                    name=node_name,
                )
                return self._api_result({"metrics": metrics})
            else:
                # 获取所有节点的 metrics
                metrics = self.custom_api.list_cluster_custom_object(  # type: ignore
                    group="metrics.k8s.io",
                    version="v1beta1",
                    plural="nodes",
                )
                return self._api_result({"metrics": metrics})
        except ApiException as e:
            return self._handle_exception(e, "get node metrics")

    def pod_with_metrics(
        self,
        namespace: Optional[str] = None,
        pod_name: Optional[str] = None,
        items_per_page: int = 10,
        page: int = 1,
        filter_by: Optional[str] = None,
    ) -> Tuple[int, str, Dict[str, Any]]:
        """
        获取 Pod 列表及其 Metrics 数据

        Args:
            namespace: 命名空间名称
            pod_name: Pod 名称
            items_per_page: 每页显示的 Pod 数量
            page: 页码
            filter_by: 过滤条件

        Returns:
            标准化响应结果 (code, message, data)
        """
        try:
            # 获取 Pod 列表
            if pod_name:
                pod_result = self._get_resource_with_namespace(
                    "pod", namespace, pod_name  # type: ignore
                )
                pod_data: Any = pod_result[2]  # type: ignore[assignment]
                pods = [pod_data] if pod_result[0] == 200 else []
            else:
                pod_result = self.pod(
                    namespace,
                    items_per_page=items_per_page,
                    page=page,
                    sort_by="d,creationTimestamp",
                    filter_by=filter_by,
                )
                if pod_result[0] != 200:
                    return pod_result
                pods = pod_result[2].get("pods", [])

            # 获取 Metrics 数据
            if pod_name:
                metrics_result = self.pod_metrics(namespace, pod_name)
            else:
                metrics_result = self.pod_metrics(namespace)

            # 合并 Pod 信息和 Metrics 数据
            pods_with_metrics = []
            metrics_data: Dict[str, Any] = {}

            # 将 Metrics 数据转换为字典，方便查找
            if metrics_result[0] == 200:
                metrics_items = (
                    metrics_result[2]
                    .get("metrics", {})
                    .get("items", [])
                )
                for item in metrics_items:
                    metadata: dict[str, Any] = item.get(
                        "metadata", {})  # type: ignore[assignment]
                    namespace_val: str = metadata.get("namespace", "")
                    name_val: str = metadata.get("name", "")
                    pod_metrics_key = f"{namespace_val}/{name_val}"
                    metrics_data[pod_metrics_key] = item

            # 为每个 Pod 添加 Metrics 信息
            for pod in pods:
                pod_key = f"{pod.metadata.namespace}/{pod.metadata.name}"
                pod_info = pod.to_dict()

                # 添加 Metrics 数据
                if pod_key in metrics_data:
                    # type: ignore[assignment]
                    metric_item: dict[str, Any] = metrics_data[pod_key]
                    containers: list[dict[str, Any]] = metric_item.get(
                        "containers", [])  # type: ignore[assignment]
                    if containers and len(containers) > 0:
                        usage: dict[str, Any] = containers[0].get(
                            "usage", {})  # type: ignore[index]
                        pod_info["metrics"] = {
                            "cpu": usage.get("cpu", ""),
                            "memory": usage.get("memory", ""),
                            "timestamp": metric_item.get("timestamp", ""),
                        }
                    else:
                        pod_info["metrics"] = None
                else:
                    pod_info["metrics"] = None

                pods_with_metrics.append(pod_info)

            if pod_name:
                return self._api_result(
                    pods_with_metrics[0] if pods_with_metrics else {}
                )
            else:
                return self._api_result(
                    {
                        "pods": pods_with_metrics,
                        "total": len(pods),
                        "items_per_page": items_per_page,
                        "page": page,
                    }
                )
        except Exception as e:
            return self._handle_exception(e, "get pods with metrics")  # type: ignore

    def resource_metrics(
        self,
        resource_type: str,
        namespace: Optional[str] = None,
        resource_name: Optional[str] = None,
    ) -> Tuple[int, str, Dict[str, Any]]:
        """
        获取资源的 Metrics 数据（通用方法）

        Args:
            resource_type: 资源类型
            namespace: 命名空间
            resource_name: 资源名称

        Returns:
            标准化响应结果 (code, message, data)
        """
        try:
            if resource_type == "pods":
                return self.pod_metrics(namespace, resource_name)
            elif resource_type == "nodes":
                return self.node_metrics(resource_name)
            else:
                return (
                    400,
                    f"Unsupported resource type for metrics: {resource_type}",
                    {},
                )
        except Exception as e:
            return self._handle_exception(e, f"get {resource_type} metrics")  # type: ignore
