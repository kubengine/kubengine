"""
Longhorn 存储 API 客户端模块

提供与 Longhorn 存储系统 API 交互的客户端类，支持节点、卷和存储容量的管理。
"""

from typing import Any, Dict, Tuple

import requests

from core.config import Application
from core.http_api_client.basic_client import BasicClient
from core.logger import get_logger

logger = get_logger(__name__)


class LonghornClient(BasicClient):
    """
    Longhorn API 客户端类

    提供与 Longhorn 存储系统 API 交互的方法，包括：
    - 节点管理
    - 卷管理
    - 存储容量查询

    Attributes:
        base_url: Longhorn 服务的基础 URL
        base_uri: API 基础路径
    """

    _logger = logger

    def __init__(self, base_url: str | None = None) -> None:
        """
        初始化 Longhorn 客户端

        Args:
            base_url: Longhorn API 的基础 URL，如果未提供则使用默认域名
        """
        super().__init__()
        self.base_url = base_url or f"https://longhorn.{Application.DOMAIN}"
        self.base_uri = "/v1"

    def nodes(self) -> Tuple[int, str, Dict[str, Any]]:
        """
        获取 Longhorn 节点列表

        Returns:
            标准化响应结果 (code, message, data)
        """
        url = f"{self.base_url}{self.base_uri}/nodes"
        response = requests.get(url, verify=self.verify_file)
        return self.api_result(response)

    def capacity(self) -> Tuple[int, str, Dict[str, Any]]:
        """
        获取 Longhorn 存储容量信息

        计算所有节点上所有磁盘的总容量和可用容量。

        Returns:
            标准化响应结果 (code, message, data)，data 包含：
                - total: 总容量（GB）
                - available: 可用容量（GB）
        """
        code, _, data = self.nodes()
        if code != 200:
            return code, "获取Longhorn节点失败，无法计算容量。", {}

        nodes_data = data["data"]
        total = 0
        available = 0

        for node in nodes_data:
            if "disks" in node:
                for _, disk_info in node["disks"].items():
                    total += disk_info.get("storageMaximum", 0)
                    available += disk_info.get("storageAvailable", 0)

        return 200, "success", {
            "total": round(total / (1024**3), 1),  # 转换为GB
            "available": round(available / (1024**3), 1)  # 转换为GB
        }

    def volumes(self) -> Tuple[int, str, Dict[str, Any]]:
        """
        获取 Longhorn 卷列表

        Returns:
            标准化响应结果 (code, message, data)
        """
        url = f"{self.base_url}{self.base_uri}/volumes"
        response = requests.get(url, verify=self.verify_file)
        return self.api_result(response)

    def delete_volume(self, name: str) -> Tuple[int, str, Dict[str, Any]]:
        """
        删除指定名称的 Longhorn 卷

        Args:
            name: 要删除的卷的名称

        Returns:
            标准化响应结果 (code, message, data)
        """
        url = f"{self.base_url}{self.base_uri}/volumes/{name}"
        response = requests.delete(url, verify=self.verify_file)
        return self.api_result(response)
