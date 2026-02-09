"""Containerd certificates and hosts configuration utilities."""

from pathlib import Path
from typing import List, Dict, Any, Optional
from core.config.config_dict import ConfigDict
from core.logger import get_logger

logger = get_logger(__name__)


class ContainerdCertsConfig:
    """Containerd证书和hosts配置管理器"""

    DEFAULT_CERTS_D_PATH = Path("/etc/containerd/certs.d")

    def __init__(self, certs_d_path: Optional[Path] = None):
        """初始化证书配置管理器

        Args:
            certs_d_path: 证书目录路径，默认为 /etc/containerd/certs.d
        """
        self.certs_d_path = certs_d_path or self.DEFAULT_CERTS_D_PATH
        self._valid_hosts_configs: Optional[Dict[str, Dict[str, Any]]] = None

    def collect_valid_hosts_toml_paths(self) -> List[Path]:
        """收集所有有效的hosts.toml文件路径

        Returns:
            List[Path]: 有效hosts.toml文件的完整路径列表
        """
        valid_hosts_toml_paths: List[Path] = []

        # 检查证书目录是否存在
        if not self.certs_d_path.exists():
            logger.warning(f"证书目录不存在: {self.certs_d_path}")
            return valid_hosts_toml_paths

        if not self.certs_d_path.is_dir():
            logger.error(f"证书路径不是目录: {self.certs_d_path}")
            return valid_hosts_toml_paths

        logger.debug(f"扫描证书目录: {self.certs_d_path}")

        # 遍历证书目录下的所有子目录
        for subdir in self.certs_d_path.iterdir():
            if not subdir.is_dir():
                # 跳过非目录文件
                logger.debug(f"跳过非目录项: {subdir}")
                continue

            hosts_toml_path = subdir / "hosts.toml"

            if not hosts_toml_path.exists():
                logger.debug(f"目录 {subdir.name} 下没有 hosts.toml 文件，忽略")
                continue

            # 验证hosts.toml文件是否有效
            if self._is_valid_hosts_toml(hosts_toml_path):
                valid_hosts_toml_paths.append(hosts_toml_path)
                logger.debug(f"发现有效配置: {hosts_toml_path}")
            else:
                logger.warning(f"无效的hosts.toml文件: {hosts_toml_path}")

        logger.debug(f"共找到 {len(valid_hosts_toml_paths)} 个有效的hosts.toml文件")
        return valid_hosts_toml_paths

    def _is_valid_hosts_toml(self, hosts_toml_path: Path) -> bool:
        """验证hosts.toml文件是否有效

        Args:
            hosts_toml_path: hosts.toml文件路径

        Returns:
            bool: 文件是否有效
        """
        try:
            # 检查文件是否可读
            if not hosts_toml_path.is_file():
                logger.debug(f"不是文件: {hosts_toml_path}")
                return False

            # 检查文件大小
            if hosts_toml_path.stat().st_size == 0:
                logger.debug(f"文件为空: {hosts_toml_path}")
                return False

            # 尝试解析TOML格式
            config = ConfigDict.load_from_file(str(hosts_toml_path))

            # 检查是否包含必要的字段
            if self._has_valid_structure(config):
                logger.debug(f"hosts.toml结构有效: {hosts_toml_path}")
                return True
            else:
                logger.debug(f"hosts.toml结构无效: {hosts_toml_path}")
                return False
        except Exception as e:
            logger.error(f"验证hosts.toml文件时发生错误: {hosts_toml_path}, 错误: {e}")
            return False

    def _has_valid_structure(self, config: Dict[str, Any]) -> bool:
        """检查TOML配置结构是否有效

        Args:
            config: 解析后的TOML配置字典

        Returns:
            bool: 结构是否有效
        """
        # 基本验证：至少包含一个有效字段
        valid_fields = ['server', 'host', 'ca', 'cert',
                        'key', 'capabilities', 'skip_verify']
        has_valid_field = any(key in config for key in valid_fields)
        if not has_valid_field:
            logger.debug("hosts.toml不包含任何有效字段")
            return False

        # 验证server字段（如果存在）
        if 'server' in config:
            server = config['server']
            if not isinstance(server, str) or not server.strip():
                logger.debug("server字段格式无效")
                return False

        # 验证host字段（如果存在）
        # if 'host' in config:
        #     host = config['host']
        #     if not isinstance(host, str) or not host.strip():
        #         logger.debug("host字段格式无效")
        #         return False

        return True

    def load_hosts_configs(self) -> Dict[str, Dict[str, Any]]:
        """加载所有有效的hosts配置

        Returns:
            Dict[str, Dict[str, Any]]: 仓库名到配置的映射
        """
        if self._valid_hosts_configs is not None:
            return self._valid_hosts_configs

        self._valid_hosts_configs = {}
        valid_paths = self.collect_valid_hosts_toml_paths()

        for hosts_toml_path in valid_paths:
            try:
                config = ConfigDict.load_from_file(str(hosts_toml_path))
                repo_name = config["server"]
                self._valid_hosts_configs[repo_name] = config
                logger.debug(f"加载配置: {repo_name} -> {config}")

            except Exception as e:
                logger.error(f"加载hosts.toml失败: {hosts_toml_path}, 错误: {e}")

        return self._valid_hosts_configs

    def get_registry_servers(self) -> List[str]:
        """获取所有配置的镜像仓库服务器地址

        Returns:
            List[str]: 服务器地址列表
        """
        configs = self.load_hosts_configs()
        servers: List[str] = []

        for repo_name, config in configs.items():
            # 优先使用server字段
            if 'server' in config:
                server = config['server']
                if isinstance(server, str) and server.strip():
                    servers.append(server.strip())

            # 最后使用仓库名本身
            else:
                servers.append(repo_name)

        # 去重
        return list(set(servers))

    def find_config_for_registry(self, registry: str) -> Optional[Dict[str, Any]]:
        """查找指定镜像仓库的配置

        Args:
            registry: 镜像仓库地址

        Returns:
            Optional[Dict[str, Any]]: 配置字典，如果没找到则返回None
        """
        configs = self.load_hosts_configs()

        # 精确匹配
        if registry in configs:
            return configs[registry]

        # 模糊匹配（去除协议前缀）
        clean_registry = registry.replace(
            'https://', '').replace('http://', '')
        if clean_registry in configs:
            return configs[clean_registry]

        # 检查server字段匹配
        for repo_name, config in configs.items():
            server = config.get('server', '')
            if isinstance(server, str) and server.strip():
                clean_server = server.replace(
                    'https://', '').replace('http://', '')
                if clean_server == clean_registry:
                    return config

        return None

    def validate_certificates(self) -> Dict[str, bool]:
        """验证所有配置的证书文件是否存在

        Returns:
            Dict[str, bool]: 证书路径到存在性的映射
        """
        configs = self.load_hosts_configs()
        cert_validation: Dict[str, bool] = {}

        for repo_name, config in configs.items():
            # 检查CA证书
            if 'ca' in config:
                ca_path = Path(config['ca'])
                cert_validation[f"{repo_name}.ca"] = ca_path.exists()

            # 检查客户端证书
            if 'cert' in config:
                cert_path = Path(config['cert'])
                cert_validation[f"{repo_name}.cert"] = cert_path.exists()

            # 检查客户端密钥
            if 'key' in config:
                key_path = Path(config['key'])
                cert_validation[f"{repo_name}.key"] = key_path.exists()

        return cert_validation

    def create_hosts_toml(
        self,
        registry: str,
        server: str,
        ca_file: Optional[str] = None,
        cert_file: Optional[str] = None,
        key_file: Optional[str] = None,
        skip_verify: bool = False,
        capabilities: Optional[List[str]] = None
    ) -> bool:
        """创建hosts.toml配置文件

        Args:
            registry: 镜像仓库名称
            server: 镜像仓库服务器地址
            ca_file: CA证书文件路径
            cert_file: 客户端证书文件路径
            key_file: 客户端密钥文件路径
            skip_verify: 是否跳过TLS验证
            capabilities: 能力列表

        Returns:
            bool: 创建是否成功
        """
        try:
            # 创建目录
            registry_dir = self.certs_d_path / registry
            registry_dir.mkdir(parents=True, exist_ok=True)

            # 构建配置
            config: ConfigDict = ConfigDict({
                'server': server
            })

            if ca_file:
                config['ca'] = ca_file

            if cert_file:
                config['cert'] = cert_file

            if key_file:
                config['key'] = key_file

            if skip_verify:
                config['skip_verify'] = True

            if capabilities:
                config['capabilities'] = capabilities

            # 写入文件
            hosts_toml_path = registry_dir / "hosts.toml"
            config.save_to_file(str(hosts_toml_path))

            logger.debug(f"创建hosts.toml成功: {hosts_toml_path}")

            # 清除缓存
            self._valid_hosts_configs = None

            return True

        except Exception as e:
            logger.error(f"创建hosts.toml失败: {registry}, 错误: {e}")
            return False

    def list_certificates_info(self) -> List[Dict[str, Any]]:
        """列出所有证书配置信息

        Returns:
            List[Dict[str, Any]]: 证书配置信息列表
        """
        configs = self.load_hosts_configs()
        cert_info: List[Dict[str, Any]] = []

        for repo_name, config in configs.items():
            info: Dict[str, Any] = {
                'registry': repo_name,
                'server': config.get('host', 'N/A'),
                'has_ca': 'ca' in config,
                'has_cert': 'cert' in config,
                'has_key': 'key' in config,
                'skip_verify': config.get('skip_verify', False),
                'capabilities': config.get('capabilities', [])
            }
            cert_info.append(info)

        return cert_info


# 示例使用
if __name__ == "__main__":
    # 示例用法
    certs_config = ContainerdCertsConfig()

    # 收集有效配置
    valid_paths = certs_config.collect_valid_hosts_toml_paths()
    print(f"有效hosts.toml路径: {valid_paths}")

    # 加载所有配置
    all_configs = certs_config.load_hosts_configs()
    print(f"所有配置: {all_configs}")

    # 获取服务器列表
    servers = certs_config.get_registry_servers()
    print(f"服务器列表: {servers}")

    # 查找特定仓库配置
    harbor_config = certs_config.find_config_for_registry("harbor.company.com")
    print(f"Harbor配置: {harbor_config}")

    # 验证证书
    cert_validation = certs_config.validate_certificates()
    print(f"证书验证结果: {cert_validation}")

    # 列出证书信息
    cert_info = certs_config.list_certificates_info()
    for info in cert_info:
        print(f"仓库: {info['registry']}, 服务器: {info['server']}")
