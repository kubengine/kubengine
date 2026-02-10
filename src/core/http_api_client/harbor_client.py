"""
Harbor 镜像仓库 API 客户端模块

提供与 Harbor Registry API 交互的客户端类，支持项目、仓库、制品和标签的管理。
"""

from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

import requests

from core.config import Application
from core.http_api_client.basic_client import BasicClient
from core.logger import get_logger
from web.utils.page import PageParams

logger = get_logger(__name__)


class HarborClient(BasicClient):
    """
    Harbor API 客户端类

    提供与 Harbor 镜像仓库 API 交互的方法，包括：
    - 项目管理
    - 仓库管理
    - 制品管理
    - 标签管理
    - 统计信息

    Attributes:
        base_url: Harbor 服务的基础 URL
        base_uri: API 基础路径
        username: Harbor 用户名
        password: Harbor 密码
    """

    _logger = logger

    def __init__(
        self,
        base_url: Optional[str] = None,
        username: str = "admin",
        password: str = "Harbor@123",
    ) -> None:
        """
        初始化 Harbor 客户端

        Args:
            base_url: Harbor 服务的基础 URL
            username: Harbor 用户名
            password: Harbor 密码
        """
        super().__init__()
        self.base_url = base_url or f"https://{Application.DOMAIN}"
        self.username = username
        self.password = password
        self.base_uri = "/api/v2.0"

    def get_projects(
        self, project_id_or_name: Optional[str] = None
    ) -> Tuple[int, str, Dict[str, Any]]:
        """
        获取 Harbor 中的所有项目列表或指定项目信息

        Args:
            project_id_or_name: 项目 ID 或名称

        Returns:
            标准化响应结果 (code, message, data)
        """
        url = (
            f"{self.base_url}{self.base_uri}/projects/{project_id_or_name}"
            if project_id_or_name
            else f"{self.base_url}{self.base_uri}/projects"
        )
        response = requests.get(
            url,
            auth=(self.username, self.password),
            verify=self.verify_file
        )
        return self.api_result(response)

    def get_repositories(
        self,
        project_name: str,
        query: Optional[str] = None,
        page_params: Optional[PageParams] = None,
    ) -> Tuple[int, str, Dict[str, Any]]:
        """
        获取指定项目下的所有仓库列表

        Args:
            project_name: 项目名称
            query: 搜索关键词
            page_params: 分页参数

        Returns:
            标准化响应结果 (code, message, data)
        """
        params: Dict[str, Any] = {}
        if page_params:
            params.update(page_params.model_dump())
        if query:
            params["q"] = query

        url = f"{self.base_url}{self.base_uri}/projects/{project_name}/repositories"
        response = requests.get(
            url,
            auth=(self.username, self.password),
            verify=self.verify_file,
            params=params,
        )
        return self.api_result(response)

    def delete_repository(
        self, project_name: str, repository_name: str
    ) -> Tuple[int, str, Dict[str, Any]]:
        """
        删除指定项目下的仓库

        Args:
            project_name: 项目名称
            repository_name: 仓库名称

        Returns:
            标准化响应结果 (code, message, data)
        """
        url = f"{self.base_url}{self.base_uri}/projects/{project_name}/repositories/{quote(repository_name)}"
        response = requests.delete(
            url,
            auth=(self.username, self.password),
            verify=self.verify_file,
        )
        return response.status_code, "", {}

    def get_artifacts(
        self,
        project_name: str,
        repository_name: str,
        query: Optional[str] = None,
        page_params: Optional[PageParams] = None,
    ) -> Tuple[int, str, Dict[str, Any]]:
        """
        获取指定仓库下的所有制品列表

        Args:
            project_name: 项目名称
            repository_name: 仓库名称
            query: 搜索关键词
            page_params: 分页参数

        Returns:
            标准化响应结果 (code, message, data)
        """
        params: Dict[str, Any] = {}
        if page_params:
            params.update(page_params.model_dump())
        if query:
            params["q"] = query

        url = f"{self.base_url}{self.base_uri}/projects/{project_name}/repositories/{repository_name}/artifacts"
        response = requests.get(
            url,
            auth=(self.username, self.password),
            verify=self.verify_file,
            params=params,
        )
        return self.api_result(response)

    def get_artifact(
        self, project_name: str, repository_name: str, digest: str
    ) -> Tuple[int, str, Dict[str, Any]]:
        """
        获取指定制品详情

        Args:
            project_name: 项目名称
            repository_name: 仓库名称
            digest: 制品摘要

        Returns:
            标准化响应结果 (code, message, data)
        """
        url = f"{self.base_url}{self.base_uri}/projects/{project_name}/repositories/{repository_name}/artifacts/{digest}"
        response = requests.get(
            url,
            auth=(self.username, self.password),
            verify=self.verify_file,
        )
        return self.api_result(response)

    def delete_artifact(
        self, project_name: str, repository_name: str, digest: str
    ) -> Tuple[int, str, Dict[str, Any]]:
        """
        删除制品

        Args:
            project_name: 项目名称
            repository_name: 仓库名称
            digest: 制品摘要

        Returns:
            标准化响应结果 (code, message, data)
        """
        url = f"{self.base_url}{self.base_uri}/projects/{project_name}/repositories/{repository_name}/artifacts/{digest}"
        response = requests.delete(
            url,
            auth=(self.username, self.password),
            verify=self.verify_file,
        )
        if response.status_code == 200:
            return 200, "success", {}
        return response.status_code, f"Failed: {response.status_code} - {response.text}", {}

    def get_chart_values(
        self, project_name: str, repository_name: str, digest: str
    ) -> Tuple[int, str, str]:
        """
        获取 Chart 制品的 values.yaml 内容

        Args:
            project_name: 项目名称
            repository_name: 仓库名称
            digest: 制品摘要

        Returns:
            标准化响应结果 (code, message, data)，data 为文本内容
        """
        url = f"{self.base_url}{self.base_uri}/projects/{project_name}/repositories/{repository_name}/artifacts/{digest}/additions/values.yaml"
        response = requests.get(
            url,
            auth=(self.username, self.password),
            verify=self.verify_file,
        )
        return self.api_result_text(response)

    def get_tags(
        self,
        project_name: str,
        repository_name: str,
        digest: str,
        page_params: Optional[PageParams] = None,
    ) -> Tuple[int, str, Dict[str, Any]]:
        """
        获取制品标签列表

        Args:
            project_name: 项目名称
            repository_name: 仓库名称
            digest: 制品摘要
            page_params: 分页参数

        Returns:
            标准化响应结果 (code, message, data)
        """
        params: Dict[str, Any] = {"with_immutable_status": True}
        if page_params:
            params.update(page_params.model_dump())

        url = f"{self.base_url}{self.base_uri}/projects/{project_name}/repositories/{repository_name}/artifacts/{digest}/tags"
        response = requests.get(
            url,
            auth=(self.username, self.password),
            verify=self.verify_file,
            params=params,
        )
        return self.api_result(response)

    def add_tag(
        self, project_name: str, repository_name: str, digest: str, tag_name: str
    ) -> Tuple[int, str, Dict[str, Any]]:
        """
        为制品添加标签

        Args:
            project_name: 项目名称
            repository_name: 仓库名称
            digest: 制品摘要
            tag_name: 标签名称

        Returns:
            标准化响应结果 (code, message, data)
        """
        url = f"{self.base_url}{self.base_uri}/projects/{project_name}/repositories/{repository_name}/artifacts/{digest}/tags"
        response = requests.post(
            url,
            auth=(self.username, self.password),
            verify=self.verify_file,
            json={"name": tag_name},
        )
        if response.status_code == 201:
            return 200, "success", {}
        return response.status_code, f"Failed: {response.status_code} - {response.text}", {}

    def delete_tag(
        self, project_name: str, repository_name: str, digest: str, tag_name: str
    ) -> Tuple[int, str, Dict[str, Any]]:
        """
        删除制品标签

        Args:
            project_name: 项目名称
            repository_name: 仓库名称
            digest: 制品摘要
            tag_name: 标签名称

        Returns:
            标准化响应结果 (code, message, data)
        """
        url = f"{self.base_url}{self.base_uri}/projects/{project_name}/repositories/{repository_name}/artifacts/{digest}/tags/{tag_name}"
        response = requests.delete(
            url,
            auth=(self.username, self.password),
            verify=self.verify_file,
        )
        if response.status_code == 200:
            return 200, "success", {}
        return response.status_code, f"Failed: {response.status_code} - {response.text}", {}

    def get_statistics(self) -> Tuple[int, str, Dict[str, Any]]:
        """
        获取 Harbor 统计信息

        Returns:
            标准化响应结果 (code, message, data)
        """
        url = f"{self.base_url}{self.base_uri}/statistics"
        response = requests.get(
            url,
            auth=(self.username, self.password),
            verify=self.verify_file,
        )
        return self.api_result(response)

    def create_project(
        self, project_name: str, public: bool = True
    ) -> bool:
        """
        在 Harbor 中创建新项目

        Args:
            project_name: 要创建的项目名称
            public: 项目是否为公开项目

        Returns:
            创建成功返回 True，失败返回 False
        """
        url = f"{self.base_url}{self.base_uri}/projects"
        payload: dict[str, Any] = {
            "project_name": project_name,
            "public": public,
            "storage_limit": -1,
        }
        response = requests.post(
            url,
            json=payload,
            auth=(self.username, self.password),
            verify=self.verify_file,
        )
        if response.status_code == 201:
            return True
        self._logger.error(f"创建项目失败: {response.status_code} - {response.text}")
        return False
