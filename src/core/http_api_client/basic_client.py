"""基础 HTTP API 客户端模块

提供统一的 HTTP 请求响应处理方法，用于标准化 API 调用的返回结果。
"""

from typing import Any, Dict, Tuple

from core.config import Application


class BasicClient:
    """基础 HTTP 客户端类

    提供统一的 API 响应处理方法，将 HTTP 响应转换为标准化的三元组格式。

    Attributes:
        verify_file: SSL 证书验证的根证书文件路径
    """

    def __init__(self) -> None:
        """初始化基础客户端

        从配置中加载 SSL 证书路径，用于 HTTPS 请求的证书验证。
        """
        self.verify_file = Application.TLS_CONFIG.CA_CRT

    def api_result(self, response: Any) -> Tuple[int, str, Dict[str, Any]]:
        """处理 JSON 响应并返回标准化结果

        根据响应状态码返回不同的结果：
        - 状态码 2xx：保留状态码与 JSON；空响应返回空字典
        - 其他状态码：返回 (状态码, 错误信息, 空字典)

        Args:
            response: HTTP 响应对象，需包含以下属性：
                - status_code: HTTP 状态码
                - json(): 返回 JSON 数据的方法
                - text: 响应文本

        Returns:
            包含三个元素的元组：
            - code: 状态码（保留上游的 2xx 成功及 4xx/5xx 失败状态）
            - message: 提示信息（成功为 "success"，失败为错误信息）
            - data: 响应数据（成功为 JSON 对象，失败为空字典）

        Examples:
            >>> # 成功响应
            >>> response = type('Response', (), {
            ...     'status_code': 200, ...     'json': lambda:
            {'nodes': ['node1', 'node2']}, ...     'text': '' ... })()
            >>> client = BasicClient()
            >>> client.api_result(response)
            (200, 'success', {'nodes': ['node1', 'node2']})

            >>> # 失败响应
            >>> response = type('Response', (), {
            ...     'status_code': 404, ...     'json': lambda: {}, ...
            'text': 'Not Found' ... })()
            >>> client.api_result(response)
            (404, '上游服务请求失败（HTTP 404）', {})
        """
        if 200 <= response.status_code < 300:
            try:
                data = response.json()
            except ValueError:
                if not response.text.strip():
                    data = {}
                else:
                    return 502, "上游服务返回了无效的 JSON 响应", {}
            return response.status_code, "success", data
        message = f"上游服务请求失败（HTTP {response.status_code}）"
        try:
            payload = response.json()
            if isinstance(payload, dict):
                supplied = payload.get("message") or payload.get("detail")
                if isinstance(supplied, str):
                    message = supplied[:2000]
                elif isinstance(payload.get("errors"), list):
                    messages = [
                        str(item.get("message", ""))
                        for item in payload["errors"]
                        if isinstance(item, dict)
                    ]
                    message = (
                        "；".join(filter(None, messages))[:2000] or message
                    )
        except (ValueError, TypeError):
            pass
        return response.status_code, message, {}

    def api_result_text(self, response: Any) -> Tuple[int, str, str]:
        """处理文本响应并返回标准化结果

        根据响应状态码返回不同的结果：
        - 状态码 2xx：保留状态码与响应文本
        - 其他状态码：返回 (状态码, 错误信息, 空字符串)

        Args:
            response: HTTP 响应对象，需包含以下属性：
                - status_code: HTTP 状态码
                - text: 响应文本

        Returns:
            包含三个元素的元组：
            - code: 状态码（保留上游的 2xx 成功及 4xx/5xx 失败状态）
            - message: 提示信息（成功为 "success"，失败为错误信息）
            - data: 响应数据（成功为文本，失败为空字符串）

        Examples:
            >>> # 成功响应
            >>> response = type('Response', (), {
            ...     'status_code': 200, ...     'text': 'success
            response' ... })()
            >>> client = BasicClient()
            >>> client.api_result_text(response)
            (200, 'success', 'success response')

            >>> # 失败响应
            >>> response = type('Response', (), {
            ...     'status_code': 404, ...     'text': 'Not Found' ...
            })()
            >>> client.api_result_text(response)
            (404, '上游服务请求失败（HTTP 404）', '')
        """
        if 200 <= response.status_code < 300:
            return response.status_code, "success", response.text
        return (
            response.status_code,
            f"上游服务请求失败（HTTP {response.status_code}）",
            "",
        )
