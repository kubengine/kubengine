"""
基础 HTTP API 客户端模块

提供统一的 HTTP 请求响应处理方法，用于标准化 API 调用的返回结果。
"""

from typing import Any, Dict, Tuple

from core.config import Application


class BasicClient:
    """
    基础 HTTP 客户端类

    提供统一的 API 响应处理方法，将 HTTP 响应转换为标准化的三元组格式。

    Attributes:
        verify_file: SSL 证书验证的根证书文件路径
    """

    def __init__(self) -> None:
        """
        初始化基础客户端

        从配置中加载 SSL 证书路径，用于 HTTPS 请求的证书验证。
        """
        self.verify_file = Application.TLS_CONFIG.CA_CRT

    def api_result(self, response: Any) -> Tuple[int, str, Dict[str, Any]]:
        """
        处理 JSON 响应并返回标准化结果

        根据响应状态码返回不同的结果：
        - 状态码 200：返回 (200, "success", 响应JSON数据)
        - 其他状态码：返回 (状态码, 错误信息, 空字典)

        Args:
            response: HTTP 响应对象，需包含以下属性：
                - status_code: HTTP 状态码
                - json(): 返回 JSON 数据的方法
                - text: 响应文本

        Returns:
            包含三个元素的元组：
            - code: 业务状态码（成功为 200，失败为 HTTP 状态码）
            - message: 提示信息（成功为 "success"，失败为错误信息）
            - data: 响应数据（成功为 JSON 对象，失败为空字典）

        Examples:
            >>> # 成功响应
            >>> response = type('Response', (), {
            ...     'status_code': 200,
            ...     'json': lambda: {'nodes': ['node1', 'node2']},
            ...     'text': ''
            ... })()
            >>> client = BasicClient()
            >>> client.api_result(response)
            (200, 'success', {'nodes': ['node1', 'node2']})

            >>> # 失败响应
            >>> response = type('Response', (), {
            ...     'status_code': 404,
            ...     'json': lambda: {},
            ...     'text': 'Not Found'
            ... })()
            >>> client.api_result(response)
            (404, 'Failed: 404 - Not Found', {})
        """
        if response.status_code == 200:
            return 200, "success", response.json()
        return (
            response.status_code,
            f"Failed: {response.status_code} - {response.text}",
            {},
        )

    def api_result_text(
        self, response: Any
    ) -> Tuple[int, str, str]:
        """
        处理文本响应并返回标准化结果

        根据响应状态码返回不同的结果：
        - 状态码 200：返回 (200, "success", 响应文本)
        - 其他状态码：返回 (状态码, 错误信息, 空字符串)

        Args:
            response: HTTP 响应对象，需包含以下属性：
                - status_code: HTTP 状态码
                - text: 响应文本

        Returns:
            包含三个元素的元组：
            - code: 业务状态码（成功为 200，失败为 HTTP 状态码）
            - message: 提示信息（成功为 "success"，失败为错误信息）
            - data: 响应数据（成功为文本，失败为空字符串）

        Examples:
            >>> # 成功响应
            >>> response = type('Response', (), {
            ...     'status_code': 200,
            ...     'text': 'success response'
            ... })()
            >>> client = BasicClient()
            >>> client.api_result_text(response)
            (200, 'success', 'success response')

            >>> # 失败响应
            >>> response = type('Response', (), {
            ...     'status_code': 404,
            ...     'text': 'Not Found'
            ... })()
            >>> client.api_result_text(response)
            (404, 'Failed: 404 - Not Found', '')
        """
        if response.status_code == 200:
            return 200, "success", response.text
        return (
            response.status_code,
            f"Failed: {response.status_code} - {response.text}",
            "",
        )
