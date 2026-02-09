"""
Redis镜像构建器

提供Redis和Redis Sentinel的镜像构建功能。
支持源码编译、配置优化和多版本构建。

功能特性：
- 源码编译Redis
- Redis Sentinel支持
- 配置文件优化
- 编译容器复用
- 构建钩子机制
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Callable, Any, Union
from urllib.parse import urlparse

from builder.image.base_builder import BaseBuilder, BuildContext, BuilderOptions
from core.command import execute_command
from core.logger import get_logger
from core.config import ConfigDict
logger = get_logger(__name__)


class RedisBuilderError(Exception):
    """Redis构建器专用异常"""
    pass


class Builder(BaseBuilder):
    """Redis基础构建器

    提供Redis编译和安装的通用功能。
    支持编译容器复用和构建钩子机制。
    """

    # 构建器元数据
    __version__ = "1.0.0"
    __author__ = "duanzt"

    # 支持的特性
    @staticmethod
    def supported_features() -> List[str]:
        """获取构建器支持的功能特性

        Returns:
            List[str]: 支持的特性列表
        """
        return [
            "source_compilation",
            "redis_server",
            "redis_sentinel",
            "configuration_optimization",
            "build_cache",
            "compile_container_reuse"
        ]

    def __init__(
        self,
        name: str,
        config_file: Optional[Union[str, Path]] = None,
        options: Optional[BuilderOptions] = None,
        compile_container_id: Optional[str] = None,
        **kwargs: Any
    ):
        """初始化Redis基础构建器

        Args:
            name: 镜像名称
            config_file: 配置文件路径
            options: 构建器选项对象
            compile_container_id: 预编译容器ID（可选）
            **kwargs: 其他构建参数
        """
        # 调用父类初始化
        super().__init__(name, config_file, options, **kwargs)

        # 编译容器ID（支持复用）
        self.compile_container_id = compile_container_id

        logger.info(f"Redis构建器初始化完成: {name}")
        if compile_container_id:
            logger.debug(f"使用预编译容器: {compile_container_id}")

    def _compile_redis(
        self,
        config: ConfigDict,
        hook_func: Callable[[str], None]
    ) -> str:
        """编译Redis

        Args:
            config: 配置字典
            hook_func: 编译完成后的钩子函数

        Returns:
            str: 编译容器ID

        Raises:
            RedisBuilderError: 编译失败
        """
        if self.compile_container_id:
            # 使用已存在的编译容器
            logger.debug(f"复用编译容器: {self.compile_container_id}")
            hook_func(self.compile_container_id)
            return self.compile_container_id

        # 创建新的编译容器
        build_image = config.get("build_image")
        if not build_image:
            raise RedisBuilderError("Redis构建镜像未指定")

        logger.info("创建Redis编译容器")
        compile_container_id = self._create_container(build_image)

        try:
            # 下载并编译Redis
            self._download_and_compile_redis(compile_container_id, config)

            # 执行钩子函数
            hook_func(compile_container_id)

            # 缓存编译容器ID
            self.compile_container_id = compile_container_id

            return compile_container_id

        except Exception as e:
            # 清理编译容器
            self._cleanup_compile_container(compile_container_id)
            raise RedisBuilderError(f"Redis编译失败: {e}")

    def _download_and_compile_redis(
        self,
        compile_container_id: str,
        config: ConfigDict
    ) -> None:
        """下载并编译Redis

        Args:
            compile_container_id: 编译容器ID
            config: 配置字典

        Raises:
            RedisBuilderError: 编译过程失败
        """
        download_url = config.get("download_url")
        if not download_url:
            raise RedisBuilderError("Redis下载地址未指定")

        # 验证URL
        try:
            parsed_url = urlparse(download_url)
            if not all([parsed_url.scheme, parsed_url.netloc]):
                raise ValueError("无效的URL格式")
        except Exception as e:
            raise RedisBuilderError(f"无效的下载地址: {e}")

        # 提取文件名和目录名
        file_name = Path(urlparse(download_url).path).name
        clean_name = file_name.replace(".tar.gz", "").replace(".tgz", "")

        logger.info(f"下载Redis源码: {file_name}")

        # 构建编译命令
        compile_commands = f"""
        cd / && \\
        wget -q {download_url} && \\
        tar xzf {file_name} && \\
        cd {clean_name} && \\
        make && \\
        make install && \\
        mkdir -p /opt/kubengine/redis/etc && \\
        cp redis.conf /opt/kubengine/redis/etc/redis.conf && \\
        cp sentinel.conf /opt/kubengine/redis/etc/sentinel.conf
        """
        execute_command(
            f"buildah run {compile_container_id} -- bash -c '{compile_commands}'"
        ).raise_if_failed("Redis编译失败")

    def _post_compile_steps(
        self,
        container_id: str,
        postunpack_script: str
    ) -> None:
        """执行构建后的通用步骤

        Args:
            container_id: 目标容器ID
            postunpack_script: postunpack.sh脚本路径或内容

        Raises:
            RedisBuilderError: 后处理步骤失败
        """
        try:
            # 设置Redis制品仓库权限
            logger.debug("设置Redis制品仓库权限")
            execute_command(
                f"buildah run {container_id} -- bash -c 'chmod g+rwX /opt/kubengine'"
            ).raise_if_failed("设置Redis制品仓库权限失败")

            # 执行postunpack脚本
            if postunpack_script:
                logger.debug("执行postunpack脚本")
                execute_command(
                    f"buildah run {container_id} -- bash -c '{postunpack_script}'"
                ).raise_if_failed("postunpack脚本执行失败")

            logger.info("后处理步骤完成")

        except Exception as e:
            raise RedisBuilderError(f"后处理步骤失败: {e}")

    def _cleanup_compile_container(self, compile_container_id: str) -> None:
        """清理编译容器

        Args:
            compile_container_id: 编译容器ID
        """
        try:
            self._umount_container(compile_container_id)
            self._del_container(compile_container_id)
            logger.debug(f"编译容器已清理: {compile_container_id}")
        except Exception as e:
            logger.warning(f"清理编译容器失败: {e}")

    def _custom_step(self, context: BuildContext) -> None:
        """Redis基础构建器的自定义步骤

        Args:
            context: 构建上下文

        Raises:
            NotImplementedError: 子类必须实现此方法
        """
        raise NotImplementedError("Redis子类必须实现 _custom_step 方法")


class RedisBuilder(Builder):
    """Redis镜像构建器

    专门用于构建Redis服务器的镜像。
    同时生成对应的Redis Sentinel镜像。
    """

    @staticmethod
    def supported_features() -> List[str]:
        """获取构建器支持的功能特性

        Returns:
            List[str]: 支持的特性列表
        """
        parent_features = Builder.supported_features()
        redis_features = [
            "redis_server_only",
            "redis_optimization",
            "sentinel_integration",
            "dual_image_build"
        ]
        return parent_features + redis_features

    def __init__(
        self,
        name: str,
        config_file: Optional[Union[str, Path]] = None,
        options: Optional[BuilderOptions] = None,
        **kwargs: Any
    ):
        """初始化Redis构建器

        Args:
            name: 镜像名称
            config_file: 配置文件路径，默认使用config.yaml
            options: 构建器选项对象
            **kwargs: 其他构建参数
        """
        # 设置默认配置文件
        default_config = Path(__file__).parent / "config.yaml"
        config_path = config_file or default_config
        self.kwargs = kwargs
        super().__init__(name, config_path, options, **kwargs)

        logger.info(f"Redis构建器初始化完成: {name}")

    def _custom_step(self, context: BuildContext) -> None:
        """Redis自定义构建步骤

        Args:
            context: 构建上下文

        Raises:
            RedisBuilderError: 构建失败
        """
        container_id = context.container_id
        version = context.version
        config = context.config

        context.log_operation("开始Redis自定义构建步骤")

        try:
            # 加载Redis配置
            redis_config = self._load_config(version)

            # 定义Redis制品复制钩子
            def _copy_redis_artifacts(compile_container_id: str):
                """复制编译好的Redis制品到目标容器"""
                context.log_operation("复制Redis制品到目标容器")

                # 复制二进制文件
                execute_command(
                    f"buildah copy --from={compile_container_id} {container_id} "
                    "/usr/local/bin /opt/kubengine/redis/bin"
                ).raise_if_failed("复制Redis二进制文件失败")

                # 复制配置文件
                execute_command(
                    f"buildah copy --from={compile_container_id} {container_id} "
                    "/opt/kubengine/redis/etc/redis.conf /opt/kubengine/redis/etc/redis-default.conf"
                ).raise_if_failed("复制Redis配置文件失败")

                context.log_operation("Redis制品复制完成")

            # 编译Redis
            self._compile_redis(redis_config, _copy_redis_artifacts)

            # 构建Redis Sentinel镜像（如果有编译容器）
            if self.compile_container_id:
                self._build_sentinel_image(version, redis_config)

            # 执行后处理步骤
            postunpack_script = config.get(
                "postunpack_script") or "/opt/kubengine/scripts/redis/postunpack.sh"
            self._post_compile_steps(container_id, postunpack_script)

            context.log_operation("Redis自定义构建步骤完成")

        except Exception as e:
            raise RedisBuilderError(f"Redis构建失败: {e}")

    def _build_sentinel_image(self, version: str, config: ConfigDict) -> None:
        """构建Redis Sentinel镜像

        Args:
            version: Redis版本
            config: 配置字典

        Raises:
            RedisBuilderError: Sentinel构建失败
        """
        try:
            # 创建Sentinel构建器实例，复用编译容器
            sentinel_builder = SentinelBuilder(
                name="redis-sentinel",
                compile_container_id=self.compile_container_id,
                **self.kwargs
            )

            # 构建Sentinel镜像
            sentinel_version, success = sentinel_builder.build(version)

            if success:
                logger.info(f"Redis Sentinel镜像构建成功: {sentinel_version}")
            else:
                logger.error(f"Redis Sentinel镜像构建失败: {sentinel_version}")

        except Exception as e:
            logger.error(f"构建Redis Sentinel镜像时发生异常: {e}")
            raise RedisBuilderError(f"Redis Sentinel构建失败: {e}")

    def get_help_info(self) -> str:
        """获取帮助信息

        Returns:
            str: 帮助信息
        """
        return "此构建器会同时生成 redis-sentinel 镜像"


class SentinelBuilder(Builder):
    """Redis Sentinel镜像构建器

    专门用于构建Redis Sentinel的镜像。
    可以复用Redis构建器的编译容器。
    """

    @staticmethod
    def supported_features() -> List[str]:
        """获取构建器支持的功能特性

        Returns:
            List[str]: 支持的特性列表
        """
        parent_features = Builder.supported_features()
        sentinel_features = [
            "redis_sentinel_only",
            "sentinel_optimization",
            "compile_reuse",
            "high_availability"
        ]
        return parent_features + sentinel_features

    def __init__(
        self,
        name: str,
        config_file: Optional[Union[str, Path]] = None,
        options: Optional[BuilderOptions] = None,
        **kwargs: Any
    ):
        """初始化Redis Sentinel构建器

        Args:
            name: 镜像名称
            config_file: 配置文件路径，默认使用sentinel_config.yaml
            options: 构建器选项对象
            **kwargs: 其他构建参数
        """
        # 设置默认配置文件
        default_config = Path(__file__).parent / "sentinel_config.yaml"
        config_path = config_file or default_config

        super().__init__(name, config_path, options, **kwargs)

        logger.info(f"Redis Sentinel构建器初始化完成: {name}")

    def _custom_step(self, context: BuildContext) -> None:
        """Redis Sentinel自定义构建步骤

        Args:
            context: 构建上下文

        Raises:
            RedisBuilderError: 构建失败
        """
        container_id = context.container_id
        version = context.version
        config = context.config

        context.log_operation("开始Redis Sentinel自定义构建步骤")

        try:
            # 加载Sentinel配置
            sentinel_config = self._load_config(version)

            # 定义Sentinel制品复制钩子
            def _copy_sentinel_artifacts(compile_container_id: str):
                """复制编译好的Sentinel制品到目标容器"""
                context.log_operation("复制Sentinel制品到目标容器")

                # 复制二进制文件
                execute_command(
                    f"buildah copy --from={compile_container_id} {container_id} "
                    "/usr/local/bin /opt/kubengine/redis-sentinel/bin"
                ).raise_if_failed("复制Sentinel二进制文件失败")

                # 复制Sentinel配置文件
                execute_command(
                    f"buildah copy --from={compile_container_id} {container_id} "
                    "/opt/kubengine/redis/etc/sentinel.conf /opt/kubengine/redis-sentinel/etc/sentinel.conf"
                ).raise_if_failed("复制Sentinel配置文件失败")

                context.log_operation("Sentinel制品复制完成")

            # 编译（或复用编译）Redis
            self._compile_redis(sentinel_config, _copy_sentinel_artifacts)

            # 执行后处理步骤
            postunpack_script = config.get(
                "postunpack_script") or "/opt/kubengine/scripts/redis-sentinel/postunpack.sh"
            self._post_compile_steps(container_id, postunpack_script)

            context.log_operation("Redis Sentinel自定义构建步骤完成")

        except Exception as e:
            context.log_operation(f"Redis Sentinel构建步骤失败: {e}")
            raise RedisBuilderError(f"Redis Sentinel构建失败: {e}")
