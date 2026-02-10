"""
Dashboard API 客户端模块

提供与 Kubernetes Dashboard API 交互的客户端类，支持命名空间、节点、Pod 等资源的查询和管理。
"""

import os
from typing import Any, Dict, List, Optional, Tuple

import requests
import yaml

from core.config import Application
from core.command import execute_command
from core.http_api_client.basic_client import BasicClient
from core.logger import get_logger
logger = get_logger(__name__)


class DashboardClient(BasicClient):
    """
    Dashboard API 客户端类

    提供与 Kubernetes Dashboard API 交互的方法，包括：
    - 命名空间管理
    - 节点查询
    - Pod 管理
    - IP 池配置
    - 资源查询

    Attributes:
        base_url: Dashboard 基础 URL
        base_uri: API 基础路径
        token: 访问令牌
        manifest_dir: 配置清单目录路径
    """

    _logger = logger

    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        token_file: Optional[str] = None,
        manifest_dir: Optional[str] = None,
    ) -> None:
        """
        初始化 Dashboard 客户端

        Args:
            base_url: Dashboard 基础 URL，默认从配置获取
            token: 访问令牌
            token_file: 令牌文件路径
            manifest_dir: 配置清单目录路径
        """
        super().__init__()
        self.base_url = base_url or f"https://dashboard.{Application.DOMAIN}"
        self.base_uri = "/api/v1"
        self.token = token
        if token is None:
            self.token = self._read_token(
                token_file
                if token_file is not None
                else os.path.join(Application.ROOT_DIR, "config", "admin-user.token")
            )
        self.manifest_dir = manifest_dir or os.path.join(
            Application.ROOT_DIR, "config", "manifest"
        )

    def _read_token(self, token_file: str) -> str:
        """
        从文件中读取访问令牌

        Args:
            token_file: 令牌文件路径

        Returns:
            读取到的令牌内容
        """
        with open(token_file, "r", encoding="utf-8") as f:
            return f.read().strip()

    def get_namespace(self) -> Tuple[int, str, Dict[str, Any]]:
        """
        获取所有命名空间列表

        Returns:
            标准化响应结果 (code, message, data)
        """
        url = f"{self.base_url}{self.base_uri}/namespace"
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {self.token}"},
            verify=self.verify_file,
        )
        return self.api_result(response)

    def node(self, name: Optional[str] = None) -> Tuple[int, str, Dict[str, Any]]:
        """
        获取节点信息

        Args:
            name: 节点名称，为空则获取所有节点

        Returns:
            标准化响应结果 (code, message, data)
        """
        url = (
            f"{self.base_url}{self.base_uri}/node"
            if name is None
            else f"{self.base_url}{self.base_uri}/node/{name}"
        )
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {self.token}"},
            verify=self.verify_file,
        )
        return self.api_result(response)

    def app_pods(
        self,
        name: str,
        items_per_page: int = 100,
        page: int = 1,
        sort_by: str = "d,creationTimestamp",
    ) -> Tuple[int, str, Dict[str, Any]]:
        """
        获取指定应用的 Pod 列表

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
            sort_by: 排序方式
            filter_by: 过滤条件

        Returns:
            标准化响应结果 (code, message, data)
        """
        url = (
            f"{self.base_url}{self.base_uri}/pod"
            if namespace is None
            else f"{self.base_url}{self.base_uri}/pod/{namespace}"
        )
        params: dict[str, Any] = {
            "itemsPerPage": items_per_page,
            "page": page,
            "sortBy": sort_by,
        }
        if filter_by is not None:
            params["filterBy"] = filter_by

        response = requests.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {self.token}"},
            verify=self.verify_file,
        )
        return self.api_result(response)

    def get_ip_pool(self) -> Tuple[int, str, Dict[str, Any]]:
        """
        获取 IP 池配置信息

        Returns:
            标准化响应结果 (code, message, data)
        """
        manifests = os.path.join(self.manifest_dir, "metallb-ippool.yaml")
        if not os.path.isfile(manifests):
            return 500, f"IP pool manifest file not found: {manifests}", {}

        with open(manifests, "r", encoding="utf-8") as f:
            ip_pool_data = yaml.safe_load(f)
        return 200, "success", ip_pool_data

    def update_ip_pool(self, ip_pools: List[str]) -> Tuple[int, str, Dict[str, Any]]:
        """
        更新 IP 池配置

        Args:
            ip_pools: 新的 IP 池地址列表

        Returns:
            标准化响应结果 (code, message, data)
        """
        manifests = os.path.join(self.manifest_dir, "metallb-ippool.yaml")
        if not os.path.isfile(manifests):
            return 500, f"IP pool manifest file not found: {manifests}", {}

        with open(manifests, "r", encoding="utf-8") as f:
            ip_pool_data = yaml.safe_load(f)

        ip_pool_data["spec"]["addresses"] = ip_pools

        with open(manifests, "w", encoding="utf-8") as f:
            yaml.dump(
                ip_pool_data,
                f,
                default_flow_style=False,
                allow_unicode=True,
            )

        cmd = f"KUBECONFIG=/etc/kubernetes/admin.conf kubectl apply -f {manifests}"
        result = execute_command(cmd)
        if result.is_failure():
            return 500, f"Failed to update IP pool: {result.get_error_lines()}", {}

        return 200, "success", {}

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
        获取资源列表

        Args:
            resource_type: 资源类型
            namespace: 命名空间名称
            items_per_page: 每页显示的资源数量
            page: 页码
            sort_by: 排序方式
            filter_by: 过滤条件

        Returns:
            标准化响应结果 (code, message, data)
        """
        url = (
            f"{self.base_url}{self.base_uri}/{resource_type}"
            if namespace is None
            else f"{self.base_url}{self.base_uri}/{resource_type}/{namespace}"
        )
        params: dict[str, Any] = {
            "itemsPerPage": items_per_page,
            "page": page,
            "sortBy": sort_by,
        }
        if filter_by is not None:
            params["filterBy"] = filter_by

        response = requests.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {self.token}"},
            verify=self.verify_file,
        )
        return self.api_result(response)

    def resource_detail(
        self,
        resource_type: str,
        namespace: str,
        name: str,
    ) -> Tuple[int, str, Dict[str, Any]]:
        """
        获取资源详情

        Args:
            resource_type: 资源类型
            namespace: 命名空间
            name: 资源名称

        Returns:
            标准化响应结果 (code, message, data)
        """
        no_namespace_resources = ["persistentvolume"]
        url = (
            f"{self.base_url}{self.base_uri}/{resource_type}/{namespace}/{name}"
            if resource_type not in no_namespace_resources
            else f"{self.base_url}{self.base_uri}/{resource_type}/{name}"
        )
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {self.token}"},
            verify=self.verify_file,
        )
        return self.api_result(response)

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
        url = (
            f"{self.base_url}{self.base_uri}/{resource_type}/{namespace}/{resource_name}/pod"
        )
        params: dict[str, Any] = {
            "itemsPerPage": items_per_page,
            "page": page,
            "sortBy": sort_by,
        }
        response = requests.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {self.token}"},
            verify=self.verify_file,
        )
        return self.api_result(response)
