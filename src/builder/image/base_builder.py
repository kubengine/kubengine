"""
镜像构建器基类（抽象类）

提供构建应用镜像的通用接口和基础流程。
具体应用需要继承此类并实现自定义构建步骤。
"""

from __future__ import annotations
from typing import Any, Dict, List, Optional, Tuple, Union, TypeVar
from dataclasses import dataclass, field

import datetime
from abc import ABC, abstractmethod
from concurrent.futures import ProcessPoolExecutor, as_completed, Future
from pathlib import Path

from core.command import CommandResult
from core.command import execute_command
from core.config import Application, ConfigDict
from core.logger import get_logger

logger = get_logger(__name__)

# 类型别名
T = TypeVar('T')
VersionResult = Tuple[str, bool]
SupportedVersions = Tuple[List[str], str]


class BuilderError(Exception):
    """构建器异常基类"""
    pass


class VersionNotSupportedError(BuilderError):
    """版本不支持异常"""
    pass


class ConfigurationError(BuilderError):
    """配置错误异常"""
    pass


@dataclass
class BuilderOptions:
    """构建器选项配置

    统一的配置类，替代原有的 **kwargs，提供类型安全和默认值管理
    """
    # 核心构建选项
    export: bool = False
    push: bool = False
    out: Union[str, Path] = field(default_factory=lambda: Path("/opt/images"))

    # 执行选项
    timeout: Optional[int] = 300
    parallel: bool = True

    # 验证选项
    strict_validation: bool = True

    # 扩展选项（兼容性支持）
    extra: Dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        """获取选项值，兼容字典访问方式

        Args:
            key: 选项键
            default: 默认值

        Returns:
            选项值或默认值
        """
        # 优先返回标准属性
        if hasattr(self, key) and not callable(getattr(self, key)):
            return getattr(self, key)

        # 然后检查扩展选项
        return self.extra.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """设置选项值

        Args:
            key: 选项键
            value: 选项值
        """
        # 如果是标准属性，直接设置
        if hasattr(self, key) and not callable(getattr(self, key)):
            setattr(self, key, value)
        else:
            # 否则存入扩展选项
            self.extra[key] = value

    def update(self, **kwargs: Any) -> None:
        """批量更新选项

        Args:
            **kwargs: 要更新的选项
        """
        for key, value in kwargs.items():
            self.set(key, value)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式

        Returns:
            包含所有选项的字典
        """
        result: Dict[str, Any] = {}

        # 收集所有标准属性（排除方法）
        for attr_name in dir(self):
            attr = getattr(self, attr_name)
            if not attr_name.startswith('_') and not callable(attr):
                result[attr_name] = attr

        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'BuilderOptions':
        """从字典创建选项对象

        Args:
            data: 选项数据字典

        Returns:
            BuilderOptions 实例
        """
        return cls(**data)


class BuildContext:
    """构建上下文，封装构建过程中的数据和操作"""

    def __init__(
        self,
        container_id: str,
        mount_dir: str,
        version: str,
        config: ConfigDict
    ):
        self.container_id = container_id
        self.mount_dir = mount_dir
        self.version = version
        self.config = config
        self._operations_log: List[str] = []

    def log_operation(self, operation: str) -> None:
        """记录操作日志"""
        self._operations_log.append(f"[{datetime.datetime.now()}] {operation}")
        logger.info(operation)

    def get_operations_log(self) -> List[str]:
        """获取操作日志"""
        return self._operations_log.copy()


class BaseBuilder(ABC):
    """应用镜像基类（抽象类）

    提供构建应用镜像的通用接口和基础流程。
    具体应用需要继承此类并实现自定义构建步骤。
    """

    def __init__(
        self,
        name: str,
        config_file: Optional[Union[str, Path]] = None,
        options: Optional[BuilderOptions] = None,
        **kwargs: Any  # 保持向后兼容
    ):
        """初始化应用构建器

        Args:
            name: 应用名称
            config_file: 配置文件路径
            options: 构建器选项对象
            **kwargs: 兼容性参数，会合并到 options 中
        """
        self.name = name
        self.config_file = Path(config_file) if config_file else None

        # 创建或使用提供的选项对象
        if options is not None:
            self.options = options
            # 合并 kwargs（会覆盖原有值）
            if kwargs:
                self.options.update(**kwargs)
        else:
            # 直接从 kwargs 创建
            self.options = BuilderOptions.from_dict(kwargs)

        # 向后兼容的属性访问
        self.export = self.options.export
        self.push = self.options.push
        self.out = Path(self.options.out)

        # 缓存配置数据
        self._config_cache: Optional[ConfigDict] = None

        # 验证初始化
        self._validate_initialization()

        logger.debug(f"初始化构建器: {name}")
        logger.debug(f"配置文件: {self.config_file}")
        logger.debug(f"构建选项: {self.options}")

    def _validate_initialization(self) -> None:
        """验证初始化参数"""
        if not self.name:
            raise ConfigurationError("应用名称不能为空")

        if not self.config_file:
            raise ConfigurationError(f"{self.name}: 配置文件路径不能为空")

        if not self.config_file.exists():
            raise ConfigurationError(
                f"{self.name}: 配置文件不存在: {self.config_file}")

    def get_option(self, key: str, default: Any = None) -> Any:
        """获取构建选项值

        Args:
            key: 选项键
            default: 默认值

        Returns:
            选项值
        """
        return self.options.get(key, default)

    def set_option(self, key: str, value: Any) -> None:
        """设置构建选项值

        Args:
            key: 选项键
            value: 选项值
        """
        self.options.set(key, value)

    def update_options(self, **kwargs: Any) -> None:
        """批量更新构建选项

        Args:
            **kwargs: 要更新的选项
        """
        self.options.update(**kwargs)

        # 更新向后兼容的属性
        for key in ['export', 'push']:
            if key in kwargs:
                setattr(self, key, kwargs[key])
        if 'out' in kwargs:
            self.out = Path(kwargs['out'])

    @property
    def config(self) -> ConfigDict:
        """获取配置数据（懒加载）"""
        if self._config_cache is None:
            self._config_cache = self._load_config()
        return self._config_cache

    def _load_config(self, version: Optional[str] = None) -> ConfigDict:
        """加载配置数据"""
        try:
            config_data = ConfigDict.load_from_file(str(self.config_file))

            if version:
                versions_data = config_data.get("versions", {})
                if versions_data:
                    version_config = versions_data.get(version, {})
                    return config_data.merge(version_config, extend_lists=True)
            return config_data

        except Exception as e:
            raise ConfigurationError(f"{self.name}: 配置加载失败: {e}")

    def supported_versions(self) -> SupportedVersions:
        """获取支持的版本列表和帮助信息

        Returns:
            Tuple[List[str], str]: (版本列表, 帮助信息)
        """
        try:
            versions_data = self.config.get("versions", {})
            if versions_data:
                versions = list(versions_data.keys())
                help_info = self.get_help_info()
                return versions, help_info
            else:
                return [], ""
        except Exception as e:
            logger.error(f"获取支持版本失败: {e}")
            return [], ""

    def get_help_info(self) -> str:
        """获取版本选择的帮助信息

        子类可以重写此方法来提供特定的提示信息

        Returns:
            str: 帮助信息
        """
        return ""

    def build(self, version: str) -> VersionResult:
        """构建单个版本镜像（模板方法）

        Args:
            version: 要构建的版本

        Returns:
            Tuple[str, bool]: (版本号, 构建是否成功)

        Raises:
            BuilderError: 构建过程中的业务异常
        """
        try:
            # 1. 验证版本支持
            self._validate_version(version)

            # 2. 加载版本配置
            config = self._load_config(version)

            # 3. 创建构建上下文
            container_id = self._create_base_container(
                config.get("base_image"))
            mount_dir = self._mount_container(container_id)
            context = BuildContext(container_id, mount_dir, version, config)

            try:
                # 4. 执行构建步骤
                self._execute_build_steps(context)

                # 5. 后处理操作
                self._post_build_steps(context)

                logger.info(f"{self.name}:{version} 构建成功")
                return version, True

            finally:
                # 6. 清理资源
                self._cleanup_resources(context)

        except Exception as e:
            raise e

    def _validate_version(self, version: str) -> None:
        """验证版本是否支持

        Args:
            version: 版本号

        Raises:
            VersionNotSupportedError: 版本不支持
        """
        versions, _ = self.supported_versions()
        if version not in versions:
            raise VersionNotSupportedError(
                f"{self.name}: 不支持的版本 {version}，支持版本: {versions}")

    def _execute_build_steps(self, context: BuildContext) -> None:
        """执行构建步骤

        Args:
            context: 构建上下文
        """
        # 1. 准备rootfs
        self._prepare_rootfs(context)

        # 2. 应用特定构建步骤
        self._custom_step(context)

        # 3. 配置容器信息
        self._config_container_info(context)

    def _prepare_rootfs(self, context: BuildContext) -> None:
        """准备rootfs目录结构

        Args:
            context: 构建上下文
        """
        from builder.image.rootfs import ROOTFS_DIR

        rootfs_config = context.config.get("rootfs", {})
        if not rootfs_config:
            return

        context.log_operation("准备rootfs目录结构")

        for local_path, container_path in rootfs_config.items():
            src_path: Path = ROOTFS_DIR / local_path
            if not src_path.exists():
                logger.warning(f"Rootfs源文件不存在: {src_path}")
                continue

            self._copy_to_container(
                context.container_id,
                str(src_path),
                container_path
            )

    def _post_build_steps(self, context: BuildContext) -> None:
        """构建后处理步骤

        Args:
            context: 构建上下文
        """
        # 1. 提交镜像
        self._commit_container(context)

        # 2. 导出镜像（如果需要）
        if self.export:
            self._export_image(context.version)

        # 3. 推送镜像（如果需要）
        if self.push:
            self._push_to_registry(context.version)

    def build_multi(self, versions: List[str]) -> List[VersionResult]:
        """并行构建多个版本

        Args:
            versions: 版本列表

        Returns:
            List[VersionResult]: 构建结果列表
        """
        if not versions:
            logger.warning("未指定构建版本")
            return []

        logger.info(f"开始并行构建 {len(versions)} 个版本: {versions}")

        # 确保输出目录存在
        self.out.mkdir(parents=True, exist_ok=True)

        with ProcessPoolExecutor(len(versions)) as executor:
            # 提交所有构建任务
            future_to_version: Dict[Future[VersionResult], str] = {
                executor.submit(self.build, version): version
                for version in versions
            }

            # 收集结果
            results: List[VersionResult] = []
            completed_count = 0

            for future in as_completed(future_to_version):
                version = future_to_version[future]
                try:
                    result = future.result()
                    results.append(result)
                    completed_count += 1

                    status = "成功" if result[1] else "失败"
                    logger.info(
                        f"构建进度 {completed_count}/{len(versions)}: {version} {status}")

                except Exception as e:
                    logger.error(f"构建 {version} 时发生异常: {e}")
                    results.append((version, False))

        # 统计结果
        successful = sum(1 for _, success in results if success)
        logger.info(f"构建完成: {successful}/{len(versions)} 成功")

        return results

    def build_sequential(self, versions: List[str]) -> List[VersionResult]:
        """顺序构建多个版本

        Args:
            versions: 版本列表

        Returns:
            List[VersionResult]: 构建结果列表
        """
        if not versions:
            logger.warning("未指定构建版本")
            return []

        logger.info(f"开始顺序构建 {len(versions)} 个版本: {versions}")

        results: List[VersionResult] = []

        for i, version in enumerate(versions, 1):
            logger.info(f"构建进度 {i}/{len(versions)}: {version}")
            result = self.build(version)
            results.append(result)

            if not result[1]:
                logger.warning(f"版本 {version} 构建失败，继续构建其他版本")

        successful = sum(1 for _, success in results if success)
        logger.info(f"顺序构建完成: {successful}/{len(versions)} 成功")

        return results

    # 容器操作方法
    def _create_base_container(self, base_image: Optional[str] = None) -> str:
        """创建基础容器

        Args:
            base_image: 基础镜像名称

        Returns:
            str: 容器ID
        """
        base_image = base_image or "scratch"
        # context.log_operation(f"创建基础容器: {base_image}")
        return self._create_container(base_image)

    def _create_container(self, base_image: str = "scratch") -> str:
        """创建容器

        Args:
            base_image: 基础镜像名称

        Returns:
            str: 容器ID
        """
        cmd = ["buildah", "from", base_image]
        result = self._execute_command(" ".join(cmd))

        container_id = result.stdout.strip()
        logger.debug(f"创建容器: {container_id}")
        return container_id

    def _mount_container(self, container_id: str) -> str:
        """挂载容器文件系统

        Args:
            container_id: 容器ID

        Returns:
            str: 挂载目录路径
        """
        cmd = ["buildah", "mount", container_id]
        result = self._execute_command(" ".join(cmd))

        mount_dir = result.stdout.strip()
        logger.debug(f"容器 {container_id} 挂载到: {mount_dir}")
        return mount_dir

    def _umount_container(self, container_id: str) -> None:
        """卸载容器文件系统

        Args:
            container_id: 容器ID
        """
        cmd = ["buildah", "umount", container_id]
        self._execute_command(" ".join(cmd), ignore_error=True)

    def _del_container(self, container_id: str) -> None:
        """删除容器

        Args:
            container_id: 容器ID
        """
        cmd = ["buildah", "rm", container_id]
        self._execute_command(" ".join(cmd), ignore_error=True)

    def _cleanup_resources(self, context: BuildContext) -> None:
        """清理构建资源

        Args:
            context: 构建上下文
        """
        try:
            self._umount_container(context.container_id)
        except Exception as e:
            logger.warning(f"卸载容器失败: {e}")

        try:
            self._del_container(context.container_id)
        except Exception as e:
            logger.warning(f"删除容器失败: {e}")

    def _config_container_info(self, context: BuildContext) -> None:
        """配置容器的基础信息

        Args:
            context: 构建上下文
        """
        config = context.config
        container_id = context.container_id

        # 配置各种容器属性
        configs: list[Tuple[str, Any]] = [
            ("cmd", config.get("cmd")),
            ("entrypoint", config.get("entrypoint")),
            ("port", config.get("ports", [])),
            ("volume", config.get("volumes", [])),
            ("workingdir", config.get("workingdir")),
            ("user", config.get("user")),
            ("env", config.get("envs", {})),
        ]

        for config_type, config_value in configs:
            if config_value:
                self._config_container(container_id, config_type, config_value)

        # 配置标签
        self._config_labels(container_id, config.get("labels", {}) or {})

        # 配置作者
        author = config.get("author", "default")
        if author:
            self._config_container(container_id, "author", author)

    def _config_labels(self, container_id: str, base_labels: dict) -> None:
        """配置容器标签

        Args:
            container_id: 容器ID
            base_labels: 基础标签字典
        """
        # 添加构建时间标签
        labels = base_labels.copy()
        labels.update({
            "org.opencontainers.image.base.created": datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat(),
            "org.opencontainers.image.created.by": f"{self.name} builder"
        })

        self._config_container(container_id, "label", labels)

    def _config_container(
        self,
        container_id: str,
        config_type: str,
        config_value: Union[str, List[str], Dict[str, str]]
    ) -> None:
        """配置容器属性

        Args:
            container_id: 容器ID
            config_type: 配置类型
            config_value: 配置值
        """
        cmd = ["buildah", "config"]

        if isinstance(config_value, list):
            for value in config_value:
                cmd.extend([f"--{config_type}", f"'{value}'"])
        elif isinstance(config_value, dict):
            for key, value in config_value.items():
                cmd.extend([f"--{config_type}", f"{key}='{value}'"])
        elif config_value:
            cmd.extend([f"--{config_type}", f"'{config_value}'"])
        else:
            return

        cmd.append(container_id)
        self._execute_command(" ".join(cmd))

    def _commit_container(self, context: BuildContext) -> None:
        """提交容器为镜像

        Args:
            context: 构建上下文
        """
        image_name = f"{self.name}:{context.version}"
        cmd = ["buildah", "commit", context.container_id, image_name]

        context.log_operation(f"提交镜像: {image_name}")
        self._execute_command(" ".join(cmd))

    def _export_image(self, version: str) -> None:
        """导出镜像为tar文件

        Args:
            version: 镜像版本
        """
        # 确保输出目录存在
        self.out.mkdir(parents=True, exist_ok=True)

        image_name = f"{self.name}:{version}"
        output_path = self.out / f"{self.name}-{version}.image.tar"

        cmd = [
            "buildah", "push", "--format", "oci", "--compression-level", "9",
            f"localhost/{image_name}",
            f"oci-archive:{output_path}:{Application.DOMAIN}/apps/{image_name}"
        ]

        logger.info(f"导出镜像到: {output_path}")
        self._execute_command(" ".join(cmd))

    def _push_to_registry(self, version: str) -> None:
        """推送到镜像仓库

        Args:
            version: 镜像版本
        """
        image_name = f"{self.name}:{version}"
        target_image = f"{Application.DOMAIN}/apps/{image_name}"

        # 从配置获取认证信息
        cmd = [
            "buildah", "push", "--creds", f"{Application.REGISTRY.USERNAME}:{Application.REGISTRY.PASSWORD}",
            f"localhost/{image_name}",
            target_image
        ]

        logger.info(f"推送镜像到仓库: {target_image}")
        self._execute_command(" ".join(cmd))

    def _copy_to_container(self, container_id: str, src_path: str, dest_path: str) -> None:
        """复制文件到容器

        Args:
            container_id: 容器ID
            src_path: 源路径
            dest_path: 目标路径
        """
        cmd = ["buildah", "copy", container_id, src_path, dest_path]
        self._execute_command(" ".join(cmd))

    def _execute_command(
        self,
        command: str,
        ignore_error: bool = False,
    ) -> CommandResult:
        """执行命令的统一接口

        Args:
            command: 要执行的命令
            ignore_error: 是否忽略错误
            timeout: 超时时间（秒）

        Returns:
            CommandResult: 命令执行结果

        Raises:
            BuilderError: 命令执行失败且ignore_error=False时
        """
        try:
            return execute_command(command).raise_if_failed()
        except Exception as e:
            if not ignore_error:
                raise BuilderError(f"命令执行异常: {command}\n异常: {e}")
            logger.warning(f"命令执行失败但忽略: {command}, 错误: {e}")
            return CommandResult(1, stdout="", stderr=str(e))

    @abstractmethod
    def _custom_step(self, context: BuildContext) -> None:
        """应用特定的构建步骤

        子类必须实现此方法来定义具体的应用构建逻辑

        Args:
            context: 构建上下文，包含容器ID、挂载目录、版本和配置信息
        """
        logger.info("执行基础构建步骤")
