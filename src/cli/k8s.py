"""
Kubernetes 集群部署CLI工具

核心功能：
1. 自动化部署K8s集群（包含证书生成、节点初始化、网络插件/存储/镜像仓库等组件安装）
2. 集成CNI网络、容器运行时、负载均衡、存储、镜像仓库等组件安装
3. 节点可达性检测、证书自动生成、配置参数校验
"""

import warnings  # noqa
# 必须在任何其他导入之前执行 gevent monkey patching
# 以避免 MonkeyPatchWarning
try:  # noqa
    from gevent import monkey  # noqa
    monkey.patch_all()  # noqa
except ImportError:  # noqa
    pass  # noqa

# 忽略 gevent 的 MonkeyPatchWarning
warnings.filterwarnings("ignore", category=DeprecationWarning, module="gevent")  # noqa
from core.ssh import AsyncSSHClient  # noqa
from infra.executor_wrapper import (  # noqa
    InfraExecutionResult,
    InfraFileExecutor,
    InfraExecutionConfig
)
from core.logger import get_logger, setup_cli_logging  # noqa
from core.misc.network import local_ips  # noqa
from core.misc.ca import create_cert  # noqa
from core.config import Application  # noqa
import ipaddress  # noqa
import click  # noqa
from typing import Any, Dict, List, Optional, Tuple  # noqa
from pathlib import Path  # noqa
import os  # noqa
import json  # noqa
import asyncio  # noqa


# 初始化日志
setup_cli_logging(
    level="INFO",
    log_file=f"{Application.ROOT_DIR}/logs/k8s_cli.log",
    console_output=False  # 禁用控制台输出
)
logger = get_logger(__name__)


class K8sDeploymentError(Exception):
    """K8s部署异常"""
    pass


class DeploymentState:
    """部署状态管理器"""

    def __init__(self, state_file: Optional[Path] = None):
        self.state_file = state_file or Path(
            Application.ROOT_DIR) / "config" / ".k8s_deployment_state.json"
        self.state: Dict[str, Any] = self._load_state()

    def _load_state(self) -> Dict[str, Any]:
        """加载部署状态"""
        default_state = {
            "completed_files": [],
            "failed_files": [],
            "skip_files": [],
            "file_hashes": {},
            "config_hash": None,
            "last_execution_time": None
        }
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                # 合并默认值，确保新增字段（如skip_files）存在
                default_state.update(state)
                if not isinstance(default_state.get("skip_files"), list):
                    default_state["skip_files"] = []
                return default_state
            except Exception as e:
                logger.warning(f"加载部署状态失败: {e}")

        return default_state

    def _save_state(self) -> None:
        """保存部署状态"""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存部署状态失败: {e}")

    def is_file_completed(self, file_name: str) -> bool:
        """检查文件是否已完成"""
        return file_name in self.state["completed_files"]

    def is_file_failed(self, file_name: str) -> bool:
        """检查文件是否失败过"""
        return file_name in self.state["failed_files"]

    def is_file_skipped(self, file_name: str) -> bool:
        """检查文件是否被手动配置跳过"""
        return file_name in self.state.get("skip_files", [])

    def mark_file_completed(self, file_name: str) -> None:
        """标记文件为已完成"""
        if file_name not in self.state["completed_files"]:
            self.state["completed_files"].append(file_name)
        # 如果之前失败过，从失败列表中移除
        if file_name in self.state["failed_files"]:
            self.state["failed_files"].remove(file_name)
        self._save_state()

    def mark_file_failed(self, file_name: str) -> None:
        """标记文件为失败"""
        if file_name not in self.state["failed_files"]:
            self.state["failed_files"].append(file_name)
        self._save_state()

    def set_config_hash(self, config_hash: str) -> None:
        """设置配置哈希"""
        self.state["config_hash"] = config_hash
        self.state["last_execution_time"] = os.path.getmtime(
            self.state_file) if self.state_file.exists() else None
        self._save_state()

    def get_file_hash(self, file_name: str) -> Optional[str]:
        """获取文件的已存储哈希"""
        return self.state.get("file_hashes", {}).get(file_name)

    def set_file_hash(self, file_name: str, file_hash: str) -> None:
        """设置单个文件的哈希"""
        if "file_hashes" not in self.state:
            self.state["file_hashes"] = {}
        self.state["file_hashes"][file_name] = file_hash
        self._save_state()

    def reset_state(self) -> None:
        """重置部署状态"""
        # 保留手动配置的skip_files（不属于部署产物）
        skip_files = self.state.get("skip_files", [])
        self.state = {
            "completed_files": [],
            "failed_files": [],
            "skip_files": skip_files,
            "file_hashes": {},
            "config_hash": None,
            "last_execution_time": None
        }
        self._save_state()

    def should_force_redeploy(self, config_hash: str) -> bool:
        """判断是否应该因配置变更强制重新部署"""
        return self.state.get("config_hash") != config_hash


class K8sDeploymentConfigValidator:
    """K8s部署配置验证器"""

    @staticmethod
    def validate_ip_address(ip: str, field_name: str) -> None:
        """验证IP地址格式

        Args:
            ip: IP地址字符串
            field_name: 字段名称（用于错误信息）

        Raises:
            K8sDeploymentError: IP地址格式无效
        """
        try:
            ipaddress.IPv4Address(ip)
        except ValueError as e:
            raise K8sDeploymentError(f"{field_name} '{ip}' 不是有效的IPv4地址: {e}")

    @staticmethod
    def validate_cidr(cidr: str, field_name: str) -> None:
        """验证CIDR格式

        Args:
            cidr: CIDR字符串
            field_name: 字段名称

        Raises:
            K8sDeploymentError: CIDR格式无效
        """
        try:
            ipaddress.ip_network(cidr, strict=False)
        except ValueError as e:
            raise K8sDeploymentError(f"{field_name} '{cidr}' 不是有效的CIDR格式: {e}")

    @staticmethod
    def validate_loadbalancer_ippool(ippool: List[str]) -> None:
        """验证负载均衡IP池

        Args:
            ippool: IP池列表

        Raises:
            K8sDeploymentError: IP池格式无效
        """
        if not ippool:
            raise K8sDeploymentError("负载均衡IP池不能为空")

        for ip_config in ippool:
            try:
                if "-" in ip_config:
                    # IP范围格式：192.168.1.100-192.168.1.200
                    start_ip, end_ip = ip_config.split("-")
                    K8sDeploymentConfigValidator.validate_ip_address(
                        start_ip.strip(), "IP范围起始地址")
                    K8sDeploymentConfigValidator.validate_ip_address(
                        end_ip.strip(), "IP范围结束地址")
                elif "/" in ip_config:
                    # CIDR格式：192.168.1.0/24
                    K8sDeploymentConfigValidator.validate_cidr(
                        ip_config, "负载均衡CIDR")
                else:
                    # 单个IP格式：192.168.1.100
                    K8sDeploymentConfigValidator.validate_ip_address(
                        ip_config, "负载均衡IP")
            except Exception as e:
                if isinstance(e, K8sDeploymentError):
                    raise
                raise K8sDeploymentError(f"无效的负载均衡IP配置 '{ip_config}': {e}")

    @staticmethod
    def validate_nameservers(nameservers: List[str]) -> None:
        """验证DNS服务器列表

        Args:
            nameservers: DNS服务器IP列表

        Raises:
            K8sDeploymentError: DNS服务器IP无效
        """
        if not nameservers:
            raise K8sDeploymentError("DNS服务器列表不能为空")

        for nameserver in nameservers:
            K8sDeploymentConfigValidator.validate_ip_address(
                nameserver, "DNS服务器")

    @staticmethod
    def validate_master_node(master_ip: str) -> None:
        """验证Master节点配置

        Args:
            master_ip: Master节点IP

        Raises:
            K8sDeploymentError: Master节点配置无效
        """
        K8sDeploymentConfigValidator.validate_ip_address(
            master_ip, "Master节点IP")

        # 检查是否为本机IP
        local_ips_list = local_ips()
        if master_ip not in local_ips_list:
            raise K8sDeploymentError(
                f"Master节点 {master_ip} 不在本机IP列表{local_ips_list}中，请确认Master节点是否为本机"
            )


class K8sDeploymentConfig:
    """K8s部署配置类"""

    def __init__(self, deploy_src: Optional[str] = None):
        """初始化K8s部署配置

        Args:
            deploy_src: 离线部署文件根目录
        """
        self.deploy_src = deploy_src or Application.K8S_CONFIG.DEPLOY_SRC
        self._validate_config()

    def _validate_config(self) -> None:
        """验证配置参数"""
        logger.info("开始验证K8s部署配置")

        # 验证Master节点
        K8sDeploymentConfigValidator.validate_master_node(
            Application.K8S_CONFIG.MASTER_IP)

        # 验证Worker节点
        for worker in Application.K8S_CONFIG.WORKER_IPS:
            K8sDeploymentConfigValidator.validate_ip_address(
                worker, "Worker节点IP")

        # 验证附加Master节点（HA模式）
        for master in Application.K8S_CONFIG.ADDITIONAL_MASTER_IPS:
            K8sDeploymentConfigValidator.validate_ip_address(
                master, "附加Master节点IP")

        # 验证网络配置
        K8sDeploymentConfigValidator.validate_cidr(
            Application.K8S_CONFIG.SERVICE_CIDR, "Service网段")
        K8sDeploymentConfigValidator.validate_cidr(
            Application.K8S_CONFIG.POD_CIDR, "Pod网段")

        # 验证负载均衡配置
        K8sDeploymentConfigValidator.validate_loadbalancer_ippool(
            Application.K8S_CONFIG.LOADBALANCER_IP_POOLS)

        # 验证DNS配置
        K8sDeploymentConfigValidator.validate_nameservers(
            Application.K8S_CONFIG.NAMESERVER)

        # 验证部署源目录
        if not self.deploy_src:
            raise K8sDeploymentError("部署源目录不能为空")

        deploy_path = Path(self.deploy_src)
        if not deploy_path.exists():
            raise K8sDeploymentError(f"部署源目录不存在: {self.deploy_src}")

        logger.info("K8s部署配置验证通过")

    def get_config_hash(self) -> str:
        """获取配置哈希值（用于检测配置变更）"""
        import hashlib
        config_str = json.dumps({
            "master_ip": Application.K8S_CONFIG.MASTER_IP,
            "additional_master_ips": sorted(Application.K8S_CONFIG.ADDITIONAL_MASTER_IPS),
            "control_plane_endpoint": Application.K8S_CONFIG.CONTROL_PLANE_ENDPOINT,
            "worker_ips": sorted(Application.K8S_CONFIG.WORKER_IPS),
            "service_cidr": Application.K8S_CONFIG.SERVICE_CIDR,
            "pod_cidr": Application.K8S_CONFIG.POD_CIDR,
            "loadbalancer_ippools": Application.K8S_CONFIG.LOADBALANCER_IP_POOLS,
            "nameserver": sorted(Application.K8S_CONFIG.NAMESERVER),
            "deploy_src": self.deploy_src
        }, sort_keys=True)
        return hashlib.md5(config_str.encode()).hexdigest()

    @property
    def all_hosts(self) -> List[str]:
        """获取所有节点IP列表"""
        return ["@local", *Application.K8S_CONFIG.ADDITIONAL_MASTER_IPS, *Application.K8S_CONFIG.WORKER_IPS]

    @property
    def host_groups(self) -> Optional[Dict[str, Tuple[list[str], Dict[str, Any]]]]:
        """获取节点分组"""
        non_data: dict[str, Any] = {}
        groups: Dict[str, Tuple[list[str], Dict[str, Any]]] = {
            "master": (["@local"], non_data),
            "worker": (Application.K8S_CONFIG.WORKER_IPS, non_data)
        }
        if Application.K8S_CONFIG.ADDITIONAL_MASTER_IPS:
            groups["additional_master"] = (
                Application.K8S_CONFIG.ADDITIONAL_MASTER_IPS, non_data)
        return groups

    def get_loadbalancer_ip(self) -> str:
        """从负载均衡IP池中提取首个可用IP"""
        if not Application.K8S_CONFIG.LOADBALANCER_IP_POOLS:
            raise ValueError("LoadBalancer IP池不能为空")

        ip_config = Application.K8S_CONFIG.LOADBALANCER_IP_POOLS[0]

        if "-" in ip_config:
            first_ip = ip_config.split("-")[0].strip()
            ipaddress.IPv4Address(first_ip)
            return first_ip
        elif "/" in ip_config:
            network = ipaddress.ip_network(ip_config, strict=False)
            return str(network.network_address + 1)
        else:
            ipaddress.IPv4Address(ip_config)
            return ip_config

    def deploy_data(self) -> Dict[str, Any]:
        """转换为字典格式（用于部署脚本）"""
        return {
            "master_ip": Application.K8S_CONFIG.MASTER_IP,
            "additional_master_ips": Application.K8S_CONFIG.ADDITIONAL_MASTER_IPS,
            "control_plane_endpoint": Application.K8S_CONFIG.CONTROL_PLANE_ENDPOINT,
            "master_interface": Application.K8S_CONFIG.MASTER_INTERFACE,
            "worker_ips": Application.K8S_CONFIG.WORKER_IPS,
            "service_cidr": Application.K8S_CONFIG.SERVICE_CIDR,
            "pod_cidr": Application.K8S_CONFIG.POD_CIDR,
            "loadbalancer_ippools": Application.K8S_CONFIG.LOADBALANCER_IP_POOLS,
            "loadbalancer_ip": self.get_loadbalancer_ip(),
            "nameserver": Application.K8S_CONFIG.NAMESERVER,
            "deploy_src": self.deploy_src,
            "manifest_dir": os.path.join(Application.ROOT_DIR, "config", "manifest"),
            "master_schedule": Application.K8S_CONFIG.MASTER_SCHEDULABLE,
            "root_dir": Application.ROOT_DIR,
            "domain": Application.DOMAIN,
            "ca_crt_file": Application.TLS_CONFIG.CA_CRT
        }

    def show_config(self) -> None:
        """显示当前配置"""
        click.echo(click.style("当前K8s部署配置:", fg="blue", bold=True))
        click.echo(f"  Master节点: {Application.K8S_CONFIG.MASTER_IP}")
        click.echo(
            f"  附加Master节点: {Application.K8S_CONFIG.ADDITIONAL_MASTER_IPS}")
        if Application.K8S_CONFIG.CONTROL_PLANE_ENDPOINT:
            click.echo(
                f"  控制面端点(VIP): {Application.K8S_CONFIG.CONTROL_PLANE_ENDPOINT}")
        click.echo(f"  Worker节点: {Application.K8S_CONFIG.WORKER_IPS}")
        click.echo(f"  Service网段: {Application.K8S_CONFIG.SERVICE_CIDR}")
        click.echo(f"  Pod网段: {Application.K8S_CONFIG.POD_CIDR}")
        click.echo(
            f"  负载均衡IP池: {Application.K8S_CONFIG.LOADBALANCER_IP_POOLS}")
        click.echo(f"  DNS服务器: {Application.K8S_CONFIG.NAMESERVER}")
        click.echo(f"  部署源目录: {self.deploy_src}")


class K8sDeployer:
    """K8s集群部署器"""

    def __init__(self, config: K8sDeploymentConfig, verbosity: int = 1):
        self.config = config
        self.verbosity = verbosity
        self.ssh_client = AsyncSSHClient()

        # 部署状态管理器
        self.deployment_state = DeploymentState()

        # 配置基础设施执行器（fail-fast模式）
        executor_config = InfraExecutionConfig(
            parallel=3,  # 适中的并发数
            connect_timeout=30,
            verbosity=verbosity,
            fail_fast=True
        )
        self.infra_executor = InfraFileExecutor(executor_config)

        # 基础设施文件路径
        self.infra_path = os.path.join(Path(__file__).parent.parent, "infra")
        self.deployment_files = self._get_deployment_files()

        # 生成每个部署文件的哈希（用于检测单文件变更）
        self.file_hashes = self._generate_file_hashes()

    def _generate_file_hashes(self) -> Dict[str, str]:
        """生成每个部署文件的哈希（用于检测单文件变更）"""
        import hashlib

        file_hashes: Dict[str, str] = {}
        for file_path, _ in self.deployment_files:
            if file_path.exists():
                mtime = file_path.stat().st_mtime
                file_hashes[file_path.name] = hashlib.md5(
                    f"{file_path}:{mtime}".encode()).hexdigest()

        return file_hashes

    def _filter_pending_files(self) -> List[Tuple[Path, str]]:
        """过滤出需要执行的文件（防幂等）"""
        # 过滤掉手动配置跳过的infra文件
        active_files = [
            (fp, desc) for fp, desc in self.deployment_files
            if not self.deployment_state.is_file_skipped(fp.name)
        ]
        skipped_count = len(self.deployment_files) - len(active_files)
        if skipped_count > 0:
            skipped_names = [
                fp.name for fp, _ in self.deployment_files
                if self.deployment_state.is_file_skipped(fp.name)
            ]
            click.echo(
                f"检测到 {skipped_count} 个组件被配置跳过（skip_files）: {skipped_names}")

        config_hash = self.config.get_config_hash()

        # 配置变更时，重置状态并执行所有文件
        if self.deployment_state.should_force_redeploy(config_hash):
            click.echo("检测到配置变更，重新执行所有组件")
            self.deployment_state.reset_state()
            self.deployment_state.set_config_hash(config_hash)
            for file_name, file_hash in self.file_hashes.items():
                self.deployment_state.set_file_hash(file_name, file_hash)
            return active_files

        pending_files: List[Tuple[Path, str]] = []
        completed_count = 0

        for file_path, description in active_files:
            file_name = file_path.name
            current_hash = self.file_hashes.get(file_name)
            stored_hash = self.deployment_state.get_file_hash(file_name)

            # 文件已完成且内容未变 → 跳过；否则需要（重新）执行
            if (self.deployment_state.is_file_completed(file_name)
                    and current_hash == stored_hash):
                completed_count += 1
                click.echo(f"{description} ({file_name}) - 已完成，跳过")
            else:
                pending_files.append((file_path, description))

        if completed_count > 0:
            click.echo(
                f"检测到 {completed_count} 个组件已完成，{len(pending_files)} 个组件待部署")

        return pending_files

    async def validate_environment(self) -> bool:
        """验证部署环境"""
        click.echo("验证部署环境...")

        # 1. 验证Master节点是否为本机（已在配置验证中完成）
        # 2. 检测所有节点SSH可达性
        click.echo("🔗 检测节点SSH连通性...")
        _, not_reachable_hosts = await self.ssh_client.is_reachable(
            [Application.K8S_CONFIG.MASTER_IP] + [x
                                                  for x in self.config.all_hosts if x != "@local"]
        )

        if not_reachable_hosts:
            self._error(f"以下节点SSH不可达: {not_reachable_hosts}")
            return False

        logger.info("所有节点SSH可达")
        click.echo("所有节点SSH可达")
        return True

    def prepare_certificates(self) -> bool:
        """生成K8s集群证书"""
        click.echo("生成K8s集群证书...")
        try:
            create_cert()
            logger.info("K8s集群证书生成完成")
            click.echo("K8s集群证书生成完成")
            return True
        except Exception as e:
            self._error(f"证书生成失败: {str(e)}")
            return False

    def _get_deployment_files(self) -> List[Tuple[Path, str]]:
        """获取部署文件列表（按执行顺序）"""
        filenames = [
            ("install_cni.py", "CNI网络插件"),
            ("install_containerd.py", "容器运行时组件"),
        ]

        # HA模式：在 kubeadm init 前部署 keepalived 提供 VIP
        if Application.K8S_CONFIG.ADDITIONAL_MASTER_IPS:
            filenames.append(
                ("install_keepalived.py", "Keepalived VIP（高可用）"))

        filenames += [
            ("install_kubernetes.py", "K8s核心组件"),
        ]

        # HA模式：附加 master 节点通过 --control-plane join
        if Application.K8S_CONFIG.ADDITIONAL_MASTER_IPS:
            filenames.append(
                ("kubernetes_join_control_plane.py", "附加Master节点加入控制面"))

        filenames += [
            ("kubernetes_join_node.py", "Worker节点加入"),
            ("install_calico.py", "Calico网络组件"),
            ("install_helm.py", "Helm包管理器"),
            ("install_metallb.py", "MetalLB负载均衡"),
            ("install_ingress_nginx.py", "Ingress控制器"),
            ("issue_cert.py", "证书分发"),
            ("install_longhorn.py", "分布式存储"),
            ("install_harbor.py", "镜像仓库"),
            ("install_metrics_server.py", "监控组件"),
            ("install_dashboard.py", "Dashboard")
        ]

        # 返回完整的文件路径（Path对象）
        return [
            (Path(self.infra_path) / filename, description)
            for filename, description in filenames
        ]

    def execute_deployment(self) -> bool:
        """执行K8s集群部署"""
        click.echo("开始执行K8s集群部署...")
        # 过滤待执行的文件
        pending_files = self._filter_pending_files()

        if not pending_files:
            click.echo("所有组件均已部署完成！")
            self._show_deployment_results()
            return True

        click.echo(f"开始部署剩余 {len(pending_files)} 个组件...")

        # 执行待部署的文件
        for file_path, description in pending_files:
            click.echo(f"\n部署组件: {description} ({file_path.name})")

            try:
                result = self.infra_executor.execute_file(
                    infra_file_path=file_path,
                    host_ips=self.config.all_hosts,
                    shared_data=self.config.deploy_data(),
                    target_groups=self.config.host_groups
                )

                if result.success:
                    click.echo(f"{description} 部署成功")
                    # 标记为已完成并保存当前文件哈希
                    self.deployment_state.mark_file_completed(file_path.name)
                    self.deployment_state.set_file_hash(
                        file_path.name, self.file_hashes.get(file_path.name, ""))
                else:
                    click.echo(f"{description} 部署失败")
                    # 标记为失败
                    self.deployment_state.mark_file_failed(file_path.name)
                    self._show_failure_details(result)
                    return False

            except Exception as e:
                click.echo(f"{description} 部署异常: {str(e)}")
                self.deployment_state.mark_file_failed(file_path.name)
                return False

        # 所有组件部署完成
        click.echo("所有组件部署完成！")
        self._show_deployment_results()
        return True

    def _show_deployment_results(self) -> None:
        """显示部署成功结果"""
        loadbalancer_ip = self.config.get_loadbalancer_ip()

        click.echo(click.style("=" * 80, fg="green", bold=True))
        click.echo(click.style("Kubernetes 集群部署成功！", fg="green", bold=True))
        click.echo(click.style("=" * 80, fg="green", bold=True))

        # 域名映射信息
        domain_mappings = f"""
    域名映射配置（需添加到 /etc/hosts）：
    {loadbalancer_ip:<15} {Application.DOMAIN}              # Harbor镜像仓库
    {loadbalancer_ip:<15} longhorn.{Application.DOMAIN}     # Longhorn存储管理
    {loadbalancer_ip:<15} dashboard.{Application.DOMAIN}    # K8s Dashboard
    """

        success_msg = f"""
    集群核心信息：
    ├─ Master节点IP:          {Application.K8S_CONFIG.MASTER_IP}
    ├─ Worker节点数量:        {len(Application.K8S_CONFIG.WORKER_IPS)}
    ├─ Service网段:           {Application.K8S_CONFIG.SERVICE_CIDR}
    ├─ Pod网段:               {Application.K8S_CONFIG.POD_CIDR}
    ├─ 负载均衡IP池:           {Application.K8S_CONFIG.LOADBALANCER_IP_POOLS}
    ├─ 负载均衡VIP:           {loadbalancer_ip}
    └─ 离线部署目录:          {self.config.deploy_src}

    证书相关：
    ├─ CA证书路径:       {Application.TLS_CONFIG.CA_CRT}
    ├─ 集群证书目录:     {Application.TLS_CONFIG.ROOT_DIR}
    └─ KubeConfig文件:  /root/.kube/config

    {domain_mappings.strip()}

    组件访问地址：
    ├─ Harbor镜像仓库:        https://{Application.DOMAIN}
    ├─ Longhorn管理面板:      https://longhorn.{Application.DOMAIN}
    ├─ K8s Dashboard:         https://dashboard.{Application.DOMAIN}
    └─ K8s APIServer:         http://{Application.K8S_CONFIG.MASTER_IP}:6443

    默认账号密码（请及时修改！）：
    ├─ Harbor默认账号:        {getattr(Application.REGISTRY, 'USERNAME', 'admin')}
    ├─ Harbor默认密码:        {getattr(Application.REGISTRY, 'PASSWORD', 'Harbor@123')}
    ├─ Longhorn无默认密码     （基于K8s RBAC认证）
    └─ Dashboard令牌获取:     kubectl -n kubernetes-dashboard create token admin-user

    常用操作提示：
    ├─ 查看节点状态:          kubectl get nodes
    ├─ 查看集群组件:          kubectl get pods -A
    ├─ 查看Longhorn状态:      kubectl get pods -n longhorn-system
    └─ 查看Harbor状态:        kubectl get pods -n harbor-system

    重要提醒：
    1. 请确保所有节点已配置上述域名映射（/etc/hosts）
    2. 首次登录Harbor请立即修改默认密码
    3. 建议备份 {Application.TLS_CONFIG.ROOT_DIR} 证书目录
    4. 如访问面板异常，请检查节点防火墙/SELinux配置
    5. 请耐心等待10分钟左右，通过 kubectl get pods -A 检查所有Pod状态正常后即可使用
    """

        click.echo(click.style(success_msg, fg="green"))
        click.echo(click.style("=" * 80, fg="green", bold=True))

    def _show_failure_details(self, result: InfraExecutionResult) -> None:
        """显示部署失败详情"""
        click.echo(click.style("部署失败详情:", fg="red", bold=True))

        if result.global_error:
            click.echo(click.style(f"全局错误: {result.global_error}", fg="red"))

        failed_hosts = result.get_failed_hosts()
        if failed_hosts:
            click.echo(click.style(f"失败主机: {failed_hosts}", fg="red"))

        for hostname, host_result in result.host_results.items():
            if not host_result.success:
                click.echo(click.style(
                    f"\n主机 {hostname}:", fg="red", bold=True))

                if host_result.error:
                    click.echo(click.style(
                        f"  错误: {host_result.error}", fg="red"))

                failed_ops = [op_name for op_name, op_result in host_result.operations.items()
                              if not op_result.success]
                if failed_ops:
                    click.echo(click.style(
                        f"  失败操作: {failed_ops}", fg="yellow"))

    def _success(self, message: str) -> None:
        """输出成功消息"""
        click.echo(click.style(f"{message}", fg="green", bold=True))

    def _error(self, message: str) -> None:
        """输出错误消息"""
        click.echo(click.style(f"{message}", fg="red", bold=True), err=True)

    async def deploy(self) -> bool:
        """执行完整的部署流程"""
        try:
            # 环境验证
            if not await self.validate_environment():
                return False

            # 证书准备
            if not self.prepare_certificates():
                return False

            # 执行部署
            return self.execute_deployment()

        except Exception as e:
            logger.error(f"部署过程中发生异常: {str(e)}", exc_info=True)
            self._error(f"部署过程中发生异常: {str(e)}")
            return False


# CLI命令定义
@click.group()
def cli():
    """Kubernetes 集群部署命令行工具"""
    pass


@cli.command(name="deploy")
@click.option(
    '--deploy-src',
    default="/root/offline-deploy",
    required=True,
    help="离线部署文件根目录（覆盖配置文件中的设置）"
)
@click.option(
    '-v', '--verbose',
    count=True,
    help="日志详细级别：-v/-vv/-vvv"
)
@click.option(
    '--show-config',
    is_flag=True,
    help="显示当前配置（不执行部署）"
)
def deploy(deploy_src: str, verbose: int, show_config: bool) -> None:
    """
    K8s集群部署命令

    部署参数从Application.K8S_DEPLOYMENT配置中读取，包括：
    - MASTER: Master节点IP
    - WORKERS: Worker节点IP列表
    - SERVICE_CIDR: Service网段
    - POD_CIDR: Pod网段
    - LOADBALANCER_IPPOOL: 负载均衡IP池
    - NAMESERVER: DNS服务器列表
    - DEPLOY_SRC: 离线部署目录

    示例：
    $ python k8s.py deploy --show-config
    $ python k8s.py deploy -vvv
    """
    logger.info("=============== 开始部署Kubernetes集群 ===============")

    try:
        # 创建部署配置（从Application配置加载）
        config = K8sDeploymentConfig(deploy_src)

        # 显示配置
        if show_config:
            config.show_config()
            return

        # 显示将要使用的配置
        click.echo(click.style("将要使用的K8s部署配置:", fg="blue", bold=True))
        config.show_config()

        # 确认部署
        if not click.confirm("\n是否继续部署？"):
            click.echo("部署已取消")
            return

        # 创建部署器并执行部署
        deployer = K8sDeployer(config, verbose)

        # 启动异步部署
        success = asyncio.run(deployer.deploy())

        # 根据结果设置退出码
        exit(0 if success else 1)

    except K8sDeploymentError as e:
        click.echo(click.style(f"配置错误: {e}", fg="red"), err=True)
        exit(1)
    except Exception as e:
        logger.error(f"部署过程中发生异常: {str(e)}", exc_info=True)
        click.echo(click.style(f"部署失败: {e}", fg="red"), err=True)
        exit(1)


@cli.command(name="config")
@click.option(
    '--validate',
    is_flag=True,
    help="验证配置"
)
@click.option(
    '--show',
    is_flag=True,
    help="显示配置"
)
def config(validate: bool, show: bool) -> None:
    """K8s部署配置管理命令"""

    if not validate and not show:
        click.echo("请指定 --validate 或 --show 选项")
        return

    try:
        # 加载配置
        deployment_config = K8sDeploymentConfig()

        if show:
            deployment_config.show_config()

        if validate:
            click.echo(click.style("配置验证通过", fg="green"))

    except K8sDeploymentError as e:
        click.echo(click.style(f"配置错误: {e}", fg="red"), err=True)
        exit(1)


@cli.command(name="reset-state")
@click.option(
    '--force',
    is_flag=True,
    help="强制重置状态"
)
def reset_state(force: bool) -> None:
    """重置部署状态"""

    if not force:
        if not click.confirm("确定要重置部署状态吗？这将重新部署所有组件"):
            click.echo("操作已取消")
            return

    try:
        deployment_state = DeploymentState()
        deployment_state.reset_state()
        click.echo(click.style("部署状态已重置", fg="green"))
    except Exception as e:
        click.echo(click.style(f"重置失败: {e}", fg="red"), err=True)
        exit(1)


if __name__ == '__main__':
    cli()
