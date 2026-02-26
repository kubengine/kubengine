"""
Kylin v11基础镜像构建器

该构建器专门用于构建Kylin v11操作系统的基础镜像。
通过优化的软件包安装和系统配置，生成安全、轻量的基础镜像。

功能特性：
- 基于dnf的软件包管理
- 自动清理缓存和文档文件
- 时区和安全配置优化
- 镜像体积最小化
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Union

from builder.image.base_builder import BaseBuilder, BuildContext, BuilderError, BuilderOptions
from core.command import CommandResult
from core.command import execute_command
from core.logger import get_logger
from core.config import ConfigDict

logger = get_logger(__name__)


class KylinV11BuilderError(Exception):
    """Kylin V11构建器专用异常"""
    pass


class Builder(BaseBuilder):
    """Kylin v11基础镜像构建器

    专门用于构建Kylin v11操作系统的基础镜像。
    继承自BaseBuilder，实现自定义构建逻辑。
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
            "dnf_package_management",
            "timezone_configuration",
            "security_optimization",
            "image_size_optimization",
            "minimal_base_image"
        ]

    def __init__(
        self,
        name: str,
        config_file: Optional[Union[str, Path]] = None,
        options: Optional[BuilderOptions] = None,
        **kwargs: Any
    ):
        """初始化Kylin v11镜像构建器

        Args:
            name: 镜像名称
            config_file: 配置文件路径，默认使用同目录下的config.yaml
            options: 构建器选项对象
            **kwargs: 其他构建参数

        Raises:
            ConfigurationError: 配置文件不存在或无效
            BuilderError: 初始化失败
        """
        # 设置默认配置文件路径
        default_config = Path(__file__).parent / "config.yaml"
        config_path = config_file or default_config

        # 调用父类初始化
        super().__init__(name, config_path, options, **kwargs)

        # 注意：环境验证已移至实际构建时执行，避免初始化时卡住
        # （例如：dnf 命令可能因为锁文件或网络问题而卡住）

        logger.info(f"Kylin v11构建器初始化完成: {name}")
        logger.debug(f"配置文件: {config_path}")
        logger.debug(f"支持特性: {self.supported_features()}")

    def _validate_build_environment(self) -> None:
        """验证构建环境

        确保必要工具可用，配置文件有效。

        Raises:
            BuilderError: 环境验证失败
        """
        execute_command(
            "dnf --version").raise_if_failed("dnf 命令不可用，请确保系统支持dnf包管理器")
        logger.debug("构建环境验证通过")

    def _custom_step(self, context: BuildContext) -> None:
        """执行自定义构建步骤

        包含以下主要步骤：
        1. 验证构建环境
        2. 安装指定的软件包
        3. 清理缓存和文档文件
        4. 配置系统时区
        5. 安全优化设置

        Args:
            context: 构建上下文

        Raises:
            BuilderError: 构建步骤执行失败
        """
        # 首先验证构建环境
        self._validate_build_environment()

        # container_id = context.container_id
        # mount_dir = context.mount_dir
        # version = context.version
        config = context.config

        context.log_operation("开始Kylin v11自定义构建步骤")

        try:
            # 1. 安装软件包
            self._install_packages(context, config)

            # 2. 优化镜像体积
            self._optimize_image_size(context)

            # 3. 配置系统设置
            self._configure_system(context, config)

            # 4. 安全加固
            self._security_hardening(context)

            context.log_operation("Kylin v11自定义构建步骤完成")

        except Exception as e:
            raise BuilderError(f"Kylin v11构建步骤执行失败: {e}")

    def _install_packages(self, context: BuildContext, config: ConfigDict) -> None:
        """安装软件包

        Args:
            context: 构建上下文
            config: 配置数据

        Raises:
            BuilderError: 软件包安装失败
        """
        mount_dir = context.mount_dir

        # 获取要安装的软件包列表
        packages: List[str] = config.get("install_packages") or []
        if not packages:
            context.log_operation("未指定软件包，跳过安装步骤")
            return

        context.log_operation(
            f"安装 {len(packages)} 个软件包: {', '.join(packages)}")

        # 构建dnf安装命令
        base_cmd: List[str] = [
            "dnf",
            "install",
            f"--installroot={mount_dir}",
            "--releasever=/",
            "--setopt=tsflags=nodocs",
            "--setopt=install_weak_deps=false",
            "--setopt=keepcache=false",
            "-y"
        ]

        # 添加额外的setopt配置
        setopts: List[str] = config.get("setopts") or []
        for setopt in setopts:
            base_cmd.extend([f"--setopt={setopt}"])

        # 添加软件包
        base_cmd.extend(packages)
        execute_command(" ".join(base_cmd)).raise_if_failed("软件包安装失败")
        context.log_operation("软件包安装完成")

    def _optimize_image_size(self, context: BuildContext) -> None:
        """优化镜像体积

        清理不必要的文件以减小镜像体积。

        Args:
            context: 构建上下文

        Raises:
            BuilderError: 优化步骤失败
        """
        mount_dir = context.mount_dir
        # container_id = context.container_id

        context.log_operation("开始优化镜像体积")

        # 定义要清理的目录和文件
        cleanup_operations: List[Tuple[str, str]] = [
            # 清理包管理器缓存
            (f"yum clean all --installroot={mount_dir} --releasever=/", "清理yum缓存"),
            # 删除文档文件
            (f"rm -rf {mount_dir}/usr/share/man/*", "删除手册文件"),
            (f"rm -rf {mount_dir}/usr/share/doc/*", "删除文档文件"),
            (f"rm -rf {mount_dir}/usr/share/info/*", "删除info文件"),
            # 删除udev相关文件
            (f"rm -rf {mount_dir}/etc/udev/rules.d/*", "删除udev规则"),
            (f"rm -rf {mount_dir}/lib/udev/*", "删除udev库文件"),
            # 删除systemd文件
            (f"rm -rf {mount_dir}/usr/lib/systemd/*", "删除systemd文件"),
            # 删除locale文件（保留基础en_US和zh_CN）
            (f"find {mount_dir}/usr/share/locale -type d ! -name 'en_US' ! -name 'zh_CN' -exec rm -rf {{}} +", "清理locale文件"),
            # 删除内核模块（基础镜像不需要）
            (f"rm -rf {mount_dir}/lib/modules/*", "删除内核模块"),
        ]

        # 执行清理操作
        for command, description in cleanup_operations:
            context.log_operation(f"执行: {description}")
            execute_command(command)

        context.log_operation("镜像体积优化完成")

    def _configure_system(self, context: BuildContext, config: ConfigDict) -> None:
        """配置系统设置

        Args:
            context: 构建上下文
            config: 配置数据

        Raises:
            BuilderError: 配置失败
        """

        context.log_operation("开始配置系统设置")

        try:
            # 配置时区
            self._configure_timezone(context)

            # 配置DNS
            self._configure_dns(context, config)

            # 配置网络
            self._configure_network(context, config)

            # 创建必要的目录
            self._create_essential_directories(context, config)

            context.log_operation("系统配置完成")

        except Exception as e:
            raise BuilderError(f"系统配置失败: {e}")

    def _configure_timezone(self, context: BuildContext) -> None:
        """配置系统时区

        Args:
            context: 构建上下文
        """
        mount_dir = context.mount_dir

        context.log_operation("配置系统时区为Asia/Shanghai")

        # 创建时区链接
        timezone_operations: List[Tuple[str, str]] = [
            (f"ln -sf /usr/share/zoneinfo/Asia/Shanghai {mount_dir}/etc/localtime", "设置时区链接"),
            (f"echo 'Asia/Shanghai' > {mount_dir}/etc/timezone", "写入时区配置"),
        ]

        for command, _ in timezone_operations:
            execute_command(command).raise_if_failed("时区配置失败")

    def _configure_dns(self, context: BuildContext, config: ConfigDict) -> None:
        """配置DNS

        Args:
            context: 构建上下文
            config: 配置数据
        """
        mount_dir = context.mount_dir

        # 获取DNS配置
        dns_servers: List[str] = config.get("dns_servers") or [
            "8.8.8.8", "114.114.114.114"]

        context.log_operation(f"配置DNS服务器: {', '.join(dns_servers)}")

        # 写入resolv.conf
        resolv_content = "\n".join(
            f"nameserver {server}" for server in dns_servers)
        resolv_file = Path(mount_dir) / "etc" / "resolv.conf"

        try:
            with open(resolv_file, 'w') as f:
                f.write(resolv_content + "\n")
                f.write("# Generated by Kylin V11 Builder\n")

        except Exception as e:
            raise BuilderError(f"写入DNS配置失败: {e}")

    def _configure_network(self, context: BuildContext, config: ConfigDict) -> None:
        """配置网络

        Args:
            context: 构建上下文
            config: 配置数据
        """
        mount_dir = context.mount_dir

        # 配置hosts文件
        hostname = config.get("hostname", "kylin-v11")

        hosts_content = f"""# Generated by Kylin V11 Builder
127.0.0.1   localhost localhost.localdomain
127.0.0.1   {hostname}
::1         localhost localhost.localdomain
"""

        hosts_file = Path(mount_dir) / "etc" / "hosts"
        try:
            with open(hosts_file, 'w') as f:
                f.write(hosts_content)

            context.log_operation(f"配置主机名为: {hostname}")

        except Exception as e:
            raise BuilderError(f"写入hosts配置失败: {e}")

    def _create_essential_directories(self, context: BuildContext, config: ConfigDict) -> None:
        """创建必要的目录

        Args:
            context: 构建上下文
            config: 配置数据
        """
        mount_dir = context.mount_dir

        # 基础目录列表
        essential_dirs: List[str] = [
            "tmp",
            "var/tmp",
            "var/log",
            "var/run",
            "opt",
            "srv"
        ]

        # 额外目录（从配置中获取）
        extra_dirs: List[str] = config.get("create_directories") or []

        all_dirs = essential_dirs + extra_dirs

        context.log_operation(f"创建必要目录: {', '.join(all_dirs)}")

        for dir_path in all_dirs:
            full_path = Path(mount_dir) / dir_path
            try:
                full_path.mkdir(parents=True, exist_ok=True)
                # 设置适当的权限
                if dir_path.startswith("tmp"):
                    os.chmod(full_path, 0o1777)  # 粘滞位

            except Exception as e:
                logger.warning(f"创建目录失败 {dir_path}: {e}")

    def _security_hardening(self, context: BuildContext) -> None:
        """安全加固

        Args:
            context: 构建上下文
        """
        container_id = context.container_id

        context.log_operation("开始安全加固")

        try:
            # 移除setuid位以增强安全性
            security_commands: List[str] = [
                f"buildah run {container_id} -- bash -c 'find / -perm /6000 -type f -exec chmod a-s {{}} \\; || true'",
                f"buildah run {container_id} -- bash -c 'find / -perm /2000 -type f -exec chmod g-s {{}} \\; || true'",
            ]

            for command in security_commands:
                result: CommandResult = execute_command(command)
                if result.is_failure():
                    logger.warning(f"安全加固命令执行失败: {command}")

            context.log_operation("安全加固完成")

        except Exception as e:
            logger.warning(f"安全加固过程中发生异常: {e}")

    def get_build_info(self, version: str) -> Dict[str, Any]:
        """获取构建信息

        Args:
            version: 镜像版本

        Returns:
            Dict[str, Any]: 构建信息字典
        """
        return {
            "builder_name": "KylinV11Builder",
            "version": version,
            "builder_version": self.__version__,
            "author": self.__author__,
            "supported_features": self.supported_features(),
            "description": "Kylin V11基础镜像构建器",
            "base_os": "Kylin Linux Advanced Server V11",
            "package_manager": "dnf"
        }
