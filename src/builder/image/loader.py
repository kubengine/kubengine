"""
镜像构建器懒加载器（基于 Entry Points）

支持通过 Entry Points 动态加载构建器实现，提供类型安全、缓存机制和插件化架构。
"""

from __future__ import annotations

import inspect
from typing import Dict, Mapping, Type, List, Optional, Any, TypeVar, Union, Callable
from pathlib import Path
from dataclasses import dataclass

from importlib.metadata import entry_points, EntryPoint

from core.logger import get_logger
from builder.image.base_builder import BaseBuilder, BuilderOptions

logger = get_logger(__name__)

T = TypeVar('T', bound=Mapping[str, Any])


@dataclass
class BuilderMetadata:
    """构建器元数据"""
    name: str
    class_type: Type[BaseBuilder]
    entry_point: EntryPoint
    description: Optional[str] = None
    version: Optional[str] = None
    author: Optional[str] = None
    supported_features: Optional[List[str]] = None

    def __post_init__(self):
        if self.supported_features is None:
            self.supported_features = []


class BuilderValidationError(Exception):
    """构建器验证异常"""
    pass


class BuilderLoadError(Exception):
    """构建器加载异常"""
    pass


class LazyBuilderLoader:
    """镜像构建器懒加载器

    支持通过 Entry Points 动态加载构建器实现，提供缓存、验证和元数据管理功能。
    """

    def __init__(
        self,
        entry_point_group: str = "image_builders",
        auto_load: bool = True,
        strict_validation: bool = True
    ):
        """初始化加载器

        Args:
            entry_point_group: Entry Points 组名
            auto_load: 是否自动加载所有构建器
            strict_validation: 是否严格验证构建器
        """
        self._cache: Dict[str, BuilderMetadata] = {}
        self._entry_point_group = entry_point_group
        self._auto_load = auto_load
        self._strict_validation = strict_validation
        self._loaded = False

        # 验证器注册
        self._validators: List[Callable[[Type[BaseBuilder]], None]] = [
            self._validate_builder_inheritance,
            self._validate_required_methods,
            self._validate_class_signature,
        ]

        if auto_load:
            self._load_all_builders()

    @property
    def is_loaded(self) -> bool:
        """检查是否已加载所有构建器"""
        return self._loaded

    def get_all_builders(self) -> Dict[str, BuilderMetadata]:
        """获取所有可用的构建器及其元数据

        Returns:
            Dict[str, BuilderMetadata]: 构建器名称到元数据的映射
        """
        if not self._loaded:
            self._load_all_builders()
        return self._cache.copy()

    def get_builder_names(self) -> List[str]:
        """获取所有可用构建器的名称列表

        Returns:
            List[str]: 构建器名称列表
        """
        return list(self.get_all_builders().keys())

    def load_builder(self, builder_name: str) -> Type[BaseBuilder]:
        """根据名称加载指定的构建器

        Args:
            builder_name: 构建器名称

        Returns:
            Type[BaseBuilder]: 构建器类

        Raises:
            BuilderValidationError: 构建器验证失败
            BuilderLoadError: 构建器加载失败
        """
        # 缓存命中
        if builder_name in self._cache:
            return self._cache[builder_name].class_type

        # 动态加载
        try:
            metadata = self._load_builder_metadata(builder_name)
            self._cache[builder_name] = metadata
            return metadata.class_type

        except Exception as e:
            raise BuilderLoadError(f"加载构建器 '{builder_name}' 失败: {e}")

    def create_builder(
        self,
        builder_name: str,
        name: str,
        config_file: Optional[Union[str, Path]] = None,
        options: Optional[BuilderOptions] = None,
        **kwargs: Any
    ) -> BaseBuilder:
        """创建构建器实例

        Args:
            builder_name: 构建器类型名称
            name: 应用名称
            config_file: 配置文件路径
            **kwargs: 其他构建参数

        Returns:
            BaseBuilder: 构建器实例

        Raises:
            BuilderValidationError: 构建器验证失败
            BuilderLoadError: 构建器加载失败
        """
        try:
            builder_class = self.load_builder(builder_name)
            return builder_class(name, config_file, options, **kwargs)

        except Exception as e:
            logger.error(f"创建构建器实例失败: {e}")
            raise BuilderLoadError(f"创建构建器 '{builder_name}' 实例失败: {e}")

    def get_builder_metadata(self, builder_name: str) -> Optional[BuilderMetadata]:
        """获取构建器元数据

        Args:
            builder_name: 构建器名称

        Returns:
            Optional[BuilderMetadata]: 构建器元数据，如果不存在则返回None
        """
        if builder_name not in self._cache:
            try:
                self.load_builder(builder_name)
            except Exception:
                return None

        return self._cache.get(builder_name)

    def validate_builder(self, builder_class: Type[BaseBuilder]) -> None:
        """验证构建器类

        Args:
            builder_class: 构建器类

        Raises:
            BuilderValidationError: 验证失败
        """
        for validator in self._validators:
            try:
                validator(builder_class)
            except Exception as e:
                raise BuilderValidationError(f"构建器验证失败: {e}")

    def add_validator(self, validator: Callable[[Type[BaseBuilder]], None]) -> None:
        """添加自定义验证器

        Args:
            validator: 验证器函数
        """
        self._validators.append(validator)

    def reload_builders(self) -> None:
        """重新加载所有构建器"""
        self._cache.clear()
        self._loaded = False
        self._load_all_builders()

    def _load_all_builders(self) -> None:
        """加载所有通过 Entry Points 注册的构建器"""
        logger.info(f"开始加载构建器组: {self._entry_point_group}")

        try:
            eps = entry_points(group=self._entry_point_group)
            loaded_count = 0

            for ep in eps:
                try:
                    metadata = self._load_builder_metadata(ep.name)
                    self._cache[ep.name] = metadata
                    loaded_count += 1

                    logger.debug(f"成功加载构建器: {ep.name}")

                except Exception as e:
                    logger.warning(f"加载构建器 {ep.name} 失败: {e}")
                    if self._strict_validation:
                        raise BuilderLoadError(f"严格模式下无法加载构建器 {ep.name}: {e}")

            self._loaded = True
            logger.info(f"构建器加载完成: {loaded_count}/{len(list(eps))}")

        except Exception as e:
            logger.error(f"加载构建器组失败: {e}")
            raise BuilderLoadError(
                f"无法加载构建器组 '{self._entry_point_group}': {e}")

    def _load_builder_metadata(self, builder_name: str) -> BuilderMetadata:
        """加载单个构建器的元数据

        Args:
            builder_name: 构建器名称

        Returns:
            BuilderMetadata: 构建器元数据

        Raises:
            ValueError: 构建器不存在
            ImportError: 构建器加载失败
            BuilderValidationError: 构建器验证失败
        """
        try:
            eps = entry_points(group=self._entry_point_group)
            ep = next(ep for ep in eps if ep.name == builder_name)
        except StopIteration:
            raise ValueError(f"构建器 '{builder_name}' 不存在")

        # 加载构建器类
        try:
            builder_class = ep.load()
        except Exception as e:
            raise ImportError(f"加载构建器类失败: {e}")

        # 验证构建器
        self.validate_builder(builder_class)

        # 提取元数据
        metadata = self._extract_metadata(ep, builder_class)

        return metadata

    def _extract_metadata(self, entry_point: EntryPoint, builder_class: Type[BaseBuilder]) -> BuilderMetadata:
        """从Entry Point和类中提取元数据

        Args:
            entry_point: Entry Point对象
            builder_class: 构建器类

        Returns:
            BuilderMetadata: 构建器元数据
        """
        # 从类属性中提取元数据
        description = getattr(builder_class, "__doc__", None)
        version = getattr(builder_class, "__version__", None)
        author = getattr(builder_class, "__author__", None)

        # 检查支持的特性
        supported_features: list[str] = []
        if hasattr(builder_class, "supported_features"):
            supported_features = getattr(builder_class, "supported_features")()

        return BuilderMetadata(
            name=entry_point.name,
            class_type=builder_class,
            entry_point=entry_point,
            description=description,
            version=version,
            author=author,
            supported_features=supported_features
        )

    def _validate_builder_inheritance(self, builder_class: Any) -> None:
        """验证构建器继承关系

        Args:
            builder_class: 构建器类

        Raises:
            BuilderValidationError: 继承关系验证失败
        """
        if not issubclass(builder_class, BaseBuilder):
            raise BuilderValidationError("构建器必须继承自 BaseBuilder")

    def _validate_required_methods(self, builder_class: Type[BaseBuilder]) -> None:
        """验证必需的抽象方法是否实现

        Args:
            builder_class: 构建器类

        Raises:
            BuilderValidationError: 必需方法验证失败
        """
        required_methods = ['_custom_step']

        for method_name in required_methods:
            if not hasattr(builder_class, method_name):
                raise BuilderValidationError(f"构建器缺少必需方法: {method_name}")

            method = getattr(builder_class, method_name)
            if not callable(method):
                raise BuilderValidationError(f"构建器方法 {method_name} 不可调用")

    def _validate_class_signature(self, builder_class: Type[BaseBuilder]) -> None:
        """验证类签名

        Args:
            builder_class: 构建器类

        Raises:
            BuilderValidationError: 类签名验证失败
        """
        try:
            sig = inspect.signature(builder_class.__init__)

            # 检查必需参数
            required_params = ['name']
            for param_name in required_params:
                if param_name not in sig.parameters:
                    raise BuilderValidationError(f"构建器缺少必需参数: {param_name}")

        except Exception as e:
            if isinstance(e, BuilderValidationError):
                raise
            raise BuilderValidationError(f"构建器类签名验证失败: {e}")

    def get_builder_info(self, builder_name: str) -> Dict[str, Any]:
        """获取构建器详细信息

        Args:
            builder_name: 构建器名称

        Returns:
            Dict[str, Any]: 构建器详细信息
        """
        metadata = self.get_builder_metadata(builder_name)
        if not metadata:
            return {}

        return {
            "name": metadata.name,
            "description": metadata.description,
            "version": metadata.version,
            "author": metadata.author,
            "supported_features": metadata.supported_features,
            "module": metadata.class_type.__module__,
            "class_name": metadata.class_type.__name__,
        }

    def list_builders_info(self) -> List[Dict[str, Any]]:
        """列出所有构建器的详细信息

        Returns:
            List[Dict[str, Any]]: 构建器信息列表
        """
        return [
            self.get_builder_info(name)
            for name in self.get_builder_names()
        ]


# 全局加载器实例
_default_loader: Optional[LazyBuilderLoader] = None


def get_default_loader() -> LazyBuilderLoader:
    """获取默认加载器实例

    Returns:
        LazyBuilderLoader: 默认加载器实例
    """
    global _default_loader
    if _default_loader is None:
        _default_loader = LazyBuilderLoader()
    return _default_loader


def load_builder(builder_name: str) -> Type[BaseBuilder]:
    """便捷函数：加载构建器

    Args:
        builder_name: 构建器名称

    Returns:
        Type[BaseBuilder]: 构建器类
    """
    return get_default_loader().load_builder(builder_name)


def create_builder(
    builder_name: str,
    name: str,
    config_file: Optional[Union[str, Path]] = None,
    options: Optional[BuilderOptions] = None,
    **kwargs: Any
) -> BaseBuilder:
    """便捷函数：创建构建器实例

    Args:
        builder_name: 构建器名称
        name: 应用名称
        config_file: 配置文件路径
        **kwargs: 其他构建参数

    Returns:
        BaseBuilder: 构建器实例
    """
    return get_default_loader().create_builder(builder_name, name, config_file, options, **kwargs)


def list_available_builders() -> List[str]:
    """便捷函数：列出所有可用的构建器

    Returns:
        List[str]: 构建器名称列表
    """
    return get_default_loader().get_builder_names()


def get_builder_info(builder_name: str) -> Dict[str, Any]:
    """便捷函数：获取构建器信息

    Args:
        builder_name: 构建器名称

    Returns:
        Dict[str, Any]: 构建器信息
    """
    return get_default_loader().get_builder_info(builder_name)
