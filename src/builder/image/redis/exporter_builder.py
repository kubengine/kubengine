"""
Redis Exporter镜像构建器

专门用于构建Redis Exporter监控组件的镜像构建器。
基于BaseBuilder，提供Redis监控指标的导出功能。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional, Union

from builder.image.base_builder import BaseBuilder, BuildContext, BuilderOptions
from core.logger import get_logger
logger = get_logger(__name__)


class Builder(BaseBuilder):
    """Redis Exporter镜像构建器

    专门用于构建Redis Exporter监控组件的镜像。
    用于导出Redis监控指标到Prometheus等监控系统。
    """

    @staticmethod
    def supported_features() -> List[str]:
        """获取构建器支持的功能特性

        Returns:
            List[str]: 支持的特性列表
        """
        return [
            "redis_monitoring",
            "prometheus_exporter",
            "metrics_collection",
            "redis_exporter",
            "monitoring_tools",
            "time_series_data",
            "redis_compatibility"
        ]

    def __init__(
        self,
        name: str,
        config_file: Optional[Union[str, Path]] = None,
        options: Optional[BuilderOptions] = None,
        **kwargs: Any
    ):
        """初始化Redis Exporter镜像构建器

        Args:
            name: 镜像名称
            config_file: 配置文件路径，默认使用同目录下的config.yaml
            options: 构建器选项对象
            **kwargs: 其他构建参数
        """
        # 设置默认配置文件路径
        default_config = Path(__file__).parent / "exporter_config.yaml"
        config_path = config_file or default_config

        # 调用父类初始化
        super().__init__(name, config_path, options, **kwargs)

        logger.info(f"kubectl构建器初始化完成: {name}")
        logger.debug(f"配置文件: {config_path}")
        logger.debug(f"支持特性: {self.supported_features()}")

    def _custom_step(self, context: BuildContext) -> None:
        """执行自定义构建步骤

        Args:
            context: 构建上下文
        """
        pass
