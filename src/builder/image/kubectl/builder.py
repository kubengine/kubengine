"""
kubectl镜像构建器

专门用于构建kubectl二进制文件的镜像构建器。
基于Kylin V11基础镜像，集成kubectl工具并配置相关环境。

功能特性：
- 基于Kylin V11基础镜像
- 内置kubectl二进制文件
- 配置kubectl环境变量
- 提供多版本支持
- 优化镜像体积
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Any, Optional, Union

from builder.image.base_builder import BuilderOptions
from builder.image.os.kylin_v11 import Builder as KylinV11Builder
from core.logger import get_logger
logger = get_logger(__name__)


class Builder(KylinV11Builder):
    """kubectl镜像构建器

    此类扩展了基础的 builder.image.os.kylin_v11.Builder 以提供特定功能，
    支持多版本的kubectl。
    """

    # 构建器元数据
    __version__ = "1.0.0"
    __author__ = "duanzt"

    @staticmethod
    def supported_features() -> List[str]:
        """获取构建器支持的功能特性

        Returns:
            List[str]: 支持的特性列表
        """
        parent_features = KylinV11Builder.supported_features()
        kubectl_features = [
            "kubectl_cli",
            "multi_version_support",
            "kubernetes_tools",
            "cli_optimization"
        ]
        return parent_features + kubectl_features

    def __init__(
        self,
        name: str,
        config_file: Optional[Union[str, Path]] = None,
        options: Optional[BuilderOptions] = None,
        **kwargs: Any
    ):
        """初始化kubectl镜像构建器

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

        logger.info(f"kubectl构建器初始化完成: {name}")
        logger.debug(f"配置文件: {config_path}")
        logger.debug(f"支持特性: {self.supported_features()}")
