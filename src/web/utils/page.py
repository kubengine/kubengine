"""
分页参数模块

提供统一的分页参数模型和依赖注入函数，用于处理 API 分页请求。
"""

from typing import Any, Optional

from fastapi import Query
from pydantic import BaseModel, Field


class PageParams(BaseModel):
    """
    分页参数模型

    用于封装分页查询参数，提供统一的分页接口。

    Attributes:
        page: 当前页码，从 1 开始
        page_size: 每页数据条数

    Example:
        >>> params = PageParams(page=1, page_size=10)
        >>> params.offset
        0
        >>> params.limit
        10
    """

    page: int = Field(default=1, ge=1, description="当前页码，从 1 开始")
    page_size: int = Field(default=10, ge=1, le=100, description="每页数据条数")

    @property
    def offset(self) -> int:
        """
        计算数据库查询的偏移量

        Returns:
            跳过的记录数

        Example:
            >>> PageParams(page=2, page_size=10).offset
            10
        """
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        """
        获取每页记录数限制

        Returns:
            每页最大记录数
        """
        return self.page_size

    def calculate_total_pages(self, total_count: int) -> int:
        """
        计算总页数

        Args:
            total_count: 总记录数

        Returns:
            总页数

        Example:
            >>> params = PageParams(page=1, page_size=10)
            >>> params.calculate_total_pages(25)
            3
        """
        return (total_count + self.page_size - 1) // self.page_size


def pagination_params(
    page: int = Query(default=1, ge=1, description="当前页码，从 1 开始"),
    page_size: int = Query(default=10, ge=1, le=100,
                           description="每页数据条数，最大 100"),
) -> PageParams:
    """
    FastAPI 分页依赖函数

    从查询参数中提取分页信息，构建 PageParams 对象。
    可直接用于 FastAPI 路由的依赖注入。

    Args:
        page: 当前页码，默认为 1，最小值为 1
        page_size: 每页条数，默认为 10，范围 1-100

    Returns:
        PageParams: 分页参数对象

    Example:
        ```python
        from fastapi import APIRouter, Depends
        from src.web.utils.page import PageParams, pagination_params

        router = APIRouter()

        @router.get("/users")
        async def list_users(params: PageParams = Depends(pagination_params)):
            # params.page = 1
            # params.page_size = 10
            # params.offset = 0
            # params.limit = 10
            pass
        ```
    """
    return PageParams(page=page, page_size=page_size)


class PaginatedResponse(BaseModel):
    """
    分页响应模型

    用于返回带有分页信息的响应数据。

    Attributes:
        items: 当前页的数据列表
        total: 总记录数
        page: 当前页码
        page_size: 每页条数
        total_pages: 总页数
        has_next: 是否有下一页
        has_prev: 是否有上一页

    Example:
        ```python
        response = PaginatedResponse(
            items=[...],
            total=100,
            page=1,
            page_size=10
        )
        # total_pages 自动计算为 10
        # has_next 自动计算为 True
        # has_prev 自动计算为 False
        ```
    """

    items: list[Any] = Field(description="当前页的数据列表")
    total: int = Field(description="总记录数")
    page: int = Field(description="当前页码")
    page_size: int = Field(description="每页条数")
    total_pages: Optional[int] = Field(default=None, description="总页数")
    has_next: Optional[bool] = Field(default=None, description="是否有下一页")
    has_prev: Optional[bool] = Field(default=None, description="是否有上一页")

    def __init__(self, **data: Any):
        """初始化分页响应，自动计算派生字段"""
        super().__init__(**data)

        # 自动计算总页数
        if self.total_pages is None:
            self.total_pages = (
                self.total + self.page_size - 1) // self.page_size

        # 自动计算是否有下一页
        if self.has_next is None:
            self.has_next = self.page < self.total_pages

        # 自动计算是否有上一页
        if self.has_prev is None:
            self.has_prev = self.page > 1


def create_paginated_response(
    items: list[Any],
    total: int,
    page_params: PageParams,
) -> PaginatedResponse:
    """
    创建分页响应

    根据查询结果和分页参数，构建标准的分页响应对象。

    Args:
        items: 当前页的数据列表
        total: 总记录数
        page_params: 分页参数对象

    Returns:
        PaginatedResponse: 分页响应对象

    Example:
        ```python
        from src.web.utils.page import PageParams, create_paginated_response

        # 查询数据
        items = query_users(offset=0, limit=10)
        total = count_users()

        # 创建分页响应
        response = create_paginated_response(
            items=items,
            total=total,
            page_params=PageParams(page=1, page_size=10)
        )
        ```
    """
    return PaginatedResponse(
        items=items,
        total=total,
        page=page_params.page,
        page_size=page_params.page_size,
    )
