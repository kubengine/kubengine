"""
统一响应模块

提供标准化的 API 响应模型和工具函数，用于构建统一的 API 响应格式。
"""

import json
from typing import Any, Generic, TypeVar, Optional

from fastapi import status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import pydantic_core


# 泛型类型变量
T = TypeVar("T")


class StandardResponse(BaseModel, Generic[T]):
    """
    标准响应模型

    用于统一 API 响应格式，支持泛型类型数据。

    Attributes:
        code: 业务状态码，200 表示成功
        message: 响应提示信息
        data: 业务数据，可选
        new_access_token: 新的访问令牌（Token 刷新时使用）
        token_type: 令牌类型，默认为 Bearer
    """

    code: int = Field(default=200, description="业务状态码")
    message: str = Field(default="操作成功", description="响应提示信息")
    data: Optional[T] = Field(default=None, description="业务数据")
    new_access_token: Optional[str] = Field(default=None, description="新的访问令牌")
    token_type: str = Field(default="Bearer", description="令牌类型")

    model_config = {
        "from_attributes": True,
        "arbitrary_types_allowed": True,
    }

    def to_json(
        self,
        indent: Optional[int] = 2,
        exclude_none: bool = True,
        ensure_ascii: bool = False,
        custom_encoder: Optional[dict[Any, Any]] = None,
        **kwargs: Any,
    ) -> str:
        """
        序列化为 JSON 字符串

        Args:
            indent: JSON 缩进，None 表示紧凑格式
            exclude_none: 是否排除值为 None 的字段
            ensure_ascii: 是否转义 ASCII 字符，False 支持中文
            custom_encoder: 自定义类型编码器
            **kwargs: json.dumps 的其他参数

        Returns:
            序列化后的 JSON 字符串
        """
        # default_encoder = {
        #     datetime: lambda v: v.strftime("%Y-%m-%d %H:%M:%S"),
        #     **(custom_encoder or {}),
        # }

        model_dict = self.model_dump(
            exclude_none=exclude_none,
            by_alias=True,
            mode="json",
        )

        return json.dumps(
            model_dict,
            indent=indent,
            ensure_ascii=ensure_ascii,
            default=lambda obj: (
                custom_encoder[type(obj)](obj)
                if custom_encoder and type(obj) in custom_encoder
                else pydantic_core.to_jsonable_python(obj)
            ),
            **kwargs,
        )

    @classmethod
    def from_json(cls, json_str: str, **kwargs: Any) -> "StandardResponse[T]":
        """
        从 JSON 字符串解析为实例

        Args:
            json_str: JSON 字符串
            **kwargs: json.loads 的其他参数

        Returns:
            StandardResponse 实例
        """
        data = json.loads(json_str, **kwargs)
        return cls(**data)


class BaseResponse(BaseModel):
    """
    基础响应模型

    所有响应模型的基类，提供基础的响应字段。

    Attributes:
        code: 业务状态码，200 表示成功
        message: 响应提示信息
    """

    code: int = Field(..., description="业务状态码")
    message: str = Field(..., description="响应提示信息")


class SuccessResponse(BaseResponse, Generic[T]):
    """
    成功响应模型

    用于返回成功的操作结果，包含业务数据。

    Attributes:
        code: 业务状态码
        message: 响应提示信息
        data: 业务数据
    """

    data: Optional[T] = Field(default=None, description="业务数据")

    model_config = {
        "from_attributes": True,
        "arbitrary_types_allowed": True,
    }


class ErrorResponse(BaseResponse):
    """
    错误响应模型

    用于返回错误信息。

    Attributes:
        code: 业务状态码
        message: 错误提示信息
        detail: 错误详情
    """

    detail: Optional[Any] = Field(default=None, description="错误详情")


class EmptySuccessResponse(BaseResponse):
    """
    无数据成功响应模型

    用于不需要返回业务数据的成功操作，如删除、更新等。
    """

    pass


def success_response(
    data: Optional[Any] = None,
    message: str = "操作成功",
    code: int = 200,
    status_code: int = status.HTTP_200_OK,
) -> JSONResponse:
    """
    创建成功响应

    Args:
        data: 业务数据
        message: 成功提示信息
        code: 业务状态码
        status_code: HTTP 状态码

    Returns:
        JSONResponse 实例
    """
    if data is None:
        response_model = EmptySuccessResponse(code=code, message=message)
    else:
        response_model = SuccessResponse(code=code, message=message, data=data)

    return JSONResponse(
        content=response_model.model_dump(exclude_none=True),
        status_code=status_code,
    )


def error_response(
    message: str = "操作失败",
    code: int = 400,
    status_code: int = status.HTTP_400_BAD_REQUEST,
    detail: Optional[Any] = None,
) -> JSONResponse:
    """
    创建错误响应

    Args:
        message: 错误提示信息
        code: 业务状态码
        status_code: HTTP 状态码
        detail: 错误详情

    Returns:
        JSONResponse 实例
    """
    response_model = ErrorResponse(code=code, message=message, detail=detail)
    return JSONResponse(
        content=response_model.model_dump(exclude_none=True),
        status_code=status_code,
    )
