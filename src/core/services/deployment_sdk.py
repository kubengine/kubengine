"""SDK 原始 JSON 传输，兼容 34–37，保留未来字段并限制请求时长。"""

import json
from contextlib import suppress

from kubernetes.client.exceptions import ApiException


def sdk_json(view, api, method, *, content_type="application/json", **kwargs):
    sdk = getattr(view, api)
    serializer = getattr(sdk, "_" + method + "_serialize", None)
    if serializer is not None:
        arguments = dict(
            kwargs,
            pretty=None,
            _request_auth=None,
            _content_type=content_type,
            _headers={"Accept": "application/json"},
            _host_index=0,
        )
        # 使用安装版本的 SDK 参数默认值，避免给不同方法传入不支持的参数。
        from inspect import Parameter, signature

        for key, param in signature(serializer).parameters.items():
            if key not in arguments and param.default is Parameter.empty:
                arguments[key] = None
        response = view.api_client.call_api(
            *serializer(**arguments), _request_timeout=(5, 20)
        )
    else:
        response = getattr(sdk, method)(
            **kwargs, _preload_content=False, _request_timeout=(5, 20)
        )
    try:
        raw = response.data
        if raw is None:
            raw = response.read()
        if response.status >= 400:
            # 不把 API 错误正文写入操作或日志，可能包含注入的敏感文本。
            raise ApiException(status=response.status, reason="集群请求被拒绝")
        return json.loads(raw)
    finally:
        underlying = getattr(response, "response", response)
        with suppress(Exception):
            underlying.close()
        with suppress(Exception):
            underlying.release_conn()
