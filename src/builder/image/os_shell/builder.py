"""
Kylin V11 OS Shell 镜像构建器

扩展基础的KylinV11Builder，专门用于构建带有shell配置的Kylin V11镜像。
包含bash配置、shell工具和环境变量等shell相关功能。
"""
from __future__ import annotations

from builder.image.os.kylin_v11 import Builder as KylinV11Builder
from builder.image.base_builder import BuilderOptions
from typing import Any, List, Optional, Union
from pathlib import Path
from core.logger import get_logger
logger = get_logger(__name__)


class Builder(KylinV11Builder):
    """Kylin V11 OS Shell 镜像构建器

    此类扩展了基础的 builder.image.os.kylin_v11.Builder 以提供特定功能，
    用于构建带有 shell 配置的 Kylin V11 镜像。
    """

    @staticmethod
    def supported_features() -> List[str]:
        """获取构建器支持的功能特性

        Returns:
            List[str]: 支持的特性列表
        """
        # 获取父类的所有特性
        parent_features = KylinV11Builder.supported_features()

        # 添加shell特定的特性
        shell_features = [
            "bash_configuration",
            "shell_customization",
            "environment_variables",
            "shell_tools",
            "interactive_shell",
            "prompt_customization",
            "bash_completion",
            "shell_history"
        ]

        # 合并特性列表
        return parent_features + shell_features

    def __init__(
        self,
        name: str,
        config_file: Optional[Union[str, Path]] = None,
        options: Optional[BuilderOptions] = None,
        **kwargs: Any
    ):
        """初始化 Kylin V11 OS Shell 镜像构建器。

        Args:
            name: 镜像名称
            config_file: 配置文件路径，默认使用同目录下的config.yaml
            options: 构建器选项对象
            **kwargs: 其他构建参数
        """
        # 设置默认配置文件路径
        default_config = Path(__file__).parent / "config.yaml"
        config_path = config_file or default_config

        # 调用父类初始化
        super().__init__(name, config_path, options, **kwargs)

        logger.info(f"Kylin V11 OS Shell 镜像构建器初始化完成: {name}")
        logger.debug(f"配置文件: {config_path}")
        logger.debug(f"支持特性: {self.supported_features()}")
