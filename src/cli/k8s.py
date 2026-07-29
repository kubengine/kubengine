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
from core.command import execute_command  # noqa
from core.http_api_client.harbor_client import HarborClient  # noqa
import ipaddress  # noqa
import click  # noqa
from typing import Any, Dict, List, Optional, Set, Tuple  # noqa
from pathlib import Path  # noqa
import os  # noqa
import json  # noqa
import asyncio  # noqa
import re  # noqa
import shutil  # noqa
import yaml  # noqa


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
            ("install_chrony.py", "时间同步(Chrony)"),
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
            ("install_dashboard.py", "Dashboard"),
            ("install_cert_manager.py", "证书管理组件")
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
        # 高可用模式下优先使用控制面端点（VIP），否则用 master IP
        apiserver_endpoint = (
            Application.K8S_CONFIG.CONTROL_PLANE_ENDPOINT
            or Application.K8S_CONFIG.MASTER_IP
        )

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
    └─ K8s APIServer:         http://{apiserver_endpoint}:6443

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


@cli.command(name="scale")
@click.option(
    '--worker-ip',
    'worker_ips',
    multiple=True,
    required=True,
    help="新 Worker 节点 IP（可多次指定）"
)
@click.option(
    '--deploy-src',
    default="/root/offline-deploy",
    help="离线部署文件根目录（覆盖配置文件中的设置）"
)
@click.option(
    '--dry-run',
    is_flag=True,
    help="仅预览将要扩容的节点，不执行部署"
)
@click.option(
    '-v', '--verbose',
    count=True,
    help="日志详细级别：-v/-vv/-vvv"
)
def scale(
    worker_ips: Tuple[str, ...],
    deploy_src: str,
    dry_run: bool,
    verbose: int
) -> None:
    """
    集群扩容：向已有 K8s 集群添加 Worker 节点

    前置条件：
    1. 已通过 kubengine-k8s deploy 完成集群部署
    2. 新节点已通过 kubengine cluster configure-cluster 纳管（SSH 互信已配置）
    3. 本机为 Master 节点

    示例：
      kubengine-k8s scale --worker-ip 172.31.57.30
      kubengine-k8s scale --worker-ip 172.31.57.30 --worker-ip 172.31.57.31 --dry-run
    """
    logger.info("=============== 开始集群扩容 ===============")

    try:
        # ---- 1. 基础校验 ----
        new_workers = list(worker_ips)

        # 校验 IP 格式
        for ip in new_workers:
            try:
                ipaddress.IPv4Address(ip)
            except ValueError:
                click.echo(click.style(f"无效的 IP 地址: {ip}", fg="red"), err=True)
                exit(1)

        # 校验 master 为本机
        local_ip_list = local_ips()
        if Application.K8S_CONFIG.MASTER_IP not in local_ip_list:
            click.echo(click.style(
                "本机不是 Master 节点，扩容命令必须在 Master 上执行", fg="red"), err=True)
            exit(1)

        # 校验新节点不与已有节点重复
        existing_workers = set(Application.K8S_CONFIG.WORKER_IPS)
        existing_masters = set(Application.K8S_CONFIG.ADDITIONAL_MASTER_IPS) | {Application.K8S_CONFIG.MASTER_IP}
        dup_existing = [ip for ip in new_workers if ip in existing_workers]
        dup_master = [ip for ip in new_workers if ip in existing_masters]
        if dup_existing:
            click.echo(click.style(
                f"以下节点已在 WORKER_IPS 配置中: {dup_existing}", fg="yellow"))
        if dup_master:
            click.echo(click.style(
                f"以下节点已是 Master 节点: {dup_master}", fg="red"), err=True)
            exit(1)

        # ---- 2. 获取集群中已有的节点列表 ----
        click.echo(click.style("\n检查集群已有节点...", fg="cyan"))
        result = execute_command(
            "kubectl get nodes -o jsonpath='{.items[*].status.addresses[?(@.type==\"InternalIP\")].address}'",
            timeout=15
        )
        if result.is_failure():
            click.echo(click.style(
                f"无法获取集群节点列表（kubectl 不可用或集群未初始化）: {result.get_error_lines()}",
                fg="red"), err=True)
            exit(1)

        cluster_node_ips_raw = result.get_output().strip()
        cluster_node_ips = set(
            ip.strip() for ip in cluster_node_ips_raw.split() if ip.strip()
        )
        click.echo(f"  集群已有节点: {sorted(cluster_node_ips)}")

        # 过滤掉已加入集群的节点
        truly_new = [ip for ip in new_workers if ip not in cluster_node_ips]
        already_in_cluster = [ip for ip in new_workers if ip in cluster_node_ips]

        if already_in_cluster:
            click.echo(click.style(
                f"以下节点已在集群中，将跳过: {already_in_cluster}", fg="yellow"))

        if not truly_new:
            click.echo(click.style(
                "没有需要扩容的新节点", fg="yellow"))
            return

        # ---- 3. 展示扩容计划 ----
        click.echo(click.style(f"\n{'=' * 60}", fg="cyan", bold=True))
        click.echo(click.style("扩容计划:", fg="cyan", bold=True))
        click.echo(click.style(f"{'=' * 60}", fg="cyan", bold=True))
        click.echo(f"  Master 节点:     {Application.K8S_CONFIG.MASTER_IP}")
        click.echo(f"  待扩容节点:      {truly_new}")
        click.echo(f"  部署源目录:      {deploy_src}")
        click.echo(f"\n  将执行以下组件（仅针对新节点）:")
        scale_steps = [
            ("install_chrony.py", "时间同步(Chrony)"),
            ("install_containerd.py", "容器运行时(Containerd)"),
            ("install_cni.py", "CNI网络插件"),
            ("issue_cert.py", "证书分发"),
            ("install_kubernetes.py", "K8s核心组件安装(跳过kubeadm init)"),
            ("kubernetes_join_node.py", "Worker节点加入集群"),
        ]
        for idx, (_, desc) in enumerate(scale_steps, 1):
            click.echo(f"    {idx}. {desc}")
        click.echo(click.style(f"{'=' * 60}\n", fg="cyan", bold=True))

        if dry_run:
            click.echo(click.style("--dry-run 模式，不执行部署", fg="yellow"))
            return

        # 确认扩容
        if not click.confirm("确认执行扩容？"):
            click.echo("扩容已取消")
            return

        # ---- 4. 执行扩容 ----
        infra_path = os.path.join(Path(__file__).parent.parent, "infra")

        executor_config = InfraExecutionConfig(
            parallel=3,
            connect_timeout=30,
            verbosity=verbose,
            fail_fast=True
        )
        infra_executor = InfraFileExecutor(executor_config)

        # 构造 deploy_data（复用现有配置）
        config = K8sDeploymentConfig(deploy_src)
        deploy_data = config.deploy_data()

        # Phase 1: 不含 master 的组件（新 worker 独立执行）
        # 这些脚本通过 host.groups 判断角色，新节点全在 worker 组
        phase1_files = [
            ("install_chrony.py", "时间同步(Chrony)"),
            ("install_containerd.py", "容器运行时(Containerd)"),
            ("install_cni.py", "CNI网络插件"),
            ("issue_cert.py", "证书分发"),
            ("install_kubernetes.py", "K8s核心组件安装"),
        ]

        # Phase 1 inventory: 仅新 worker
        phase1_hosts = truly_new
        phase1_groups: Dict[str, Tuple[list[str], Dict[str, Any]]] = {
            "worker": (truly_new, {})
        }

        for filename, description in phase1_files:
            file_path = Path(infra_path) / filename
            click.echo(click.style(f"\n[{description}] 执行中...", fg="blue"))

            result = infra_executor.execute_file(
                infra_file_path=file_path,
                host_ips=phase1_hosts,
                shared_data=deploy_data,
                target_groups=phase1_groups
            )

            if not result.success:
                click.echo(click.style(f"{description} 部署失败", fg="red"))
                _show_scale_failure(result)
                exit(1)

            click.echo(click.style(f"✅ {description} 完成", fg="green"))

        # Phase 2: join（需要 master 在 inventory 中以获取 join token）
        join_file = Path(infra_path) / "kubernetes_join_node.py"
        click.echo(click.style(f"\n[Worker节点加入集群] 执行中...", fg="blue"))

        # Phase 2 inventory: master(@local) + 新 worker
        phase2_hosts = ["@local", *truly_new]
        phase2_groups: Dict[str, Tuple[list[str], Dict[str, Any]]] = {
            "master": (["@local"], {}),
            "worker": (truly_new, {})
        }

        result = infra_executor.execute_file(
            infra_file_path=join_file,
            host_ips=phase2_hosts,
            shared_data=deploy_data,
            target_groups=phase2_groups
        )

        if not result.success:
            click.echo(click.style("Worker 节点加入集群失败", fg="red"))
            _show_scale_failure(result)
            exit(1)

        click.echo(click.style("✅ Worker 节点加入集群完成", fg="green"))

        # ---- 5. 更新配置文件 ----
        click.echo(click.style("\n更新配置文件...", fg="cyan"))
        _update_worker_ips(truly_new)

        # ---- 6. 结果展示 ----
        click.echo(click.style(
            f"\n{'=' * 60}", fg="green", bold=True))
        click.echo(click.style(
            "集群扩容成功！", fg="green", bold=True))
        click.echo(click.style(
            f"{'=' * 60}", fg="green", bold=True))
        click.echo(f"  新增 Worker 节点: {truly_new}")
        updated_workers = list(Application.K8S_CONFIG.WORKER_IPS) + truly_new
        click.echo(f"  当前 Worker 总数: {len(updated_workers)}")
        click.echo(f"\n  请稍候，通过以下命令确认节点状态:")
        click.echo(f"    kubectl get nodes")
        click.echo(click.style(
            f"{'=' * 60}", fg="green", bold=True))

        logger.info("=============== 集群扩容完成 ===============")

    except K8sDeploymentError as e:
        click.echo(click.style(f"配置错误: {e}", fg="red"), err=True)
        exit(1)
    except Exception as e:
        logger.error(f"扩容过程中发生异常: {str(e)}", exc_info=True)
        click.echo(click.style(f"扩容失败: {e}", fg="red"), err=True)
        exit(1)


def _show_scale_failure(result: InfraExecutionResult) -> None:
    """显示扩容失败详情"""
    click.echo(click.style("失败详情:", fg="red", bold=True))

    if result.global_error:
        click.echo(click.style(f"全局错误: {result.global_error}", fg="red"))

    failed_hosts = result.get_failed_hosts()
    if failed_hosts:
        click.echo(click.style(f"失败主机: {failed_hosts}", fg="red"))

    for hostname, host_result in result.host_results.items():
        if not host_result.success:
            click.echo(click.style(f"\n主机 {hostname}:", fg="red", bold=True))
            for op in host_result.operations:
                if not op.success:
                    click.echo(f"  操作: {op.name}")
                    if op.stderr:
                        click.echo(click.style(
                            f"  错误: {op.stderr[:500]}", fg="red"))


def _update_worker_ips(new_workers: List[str]) -> None:
    """扩容成功后将新节点追加到 application.yaml 的 worker.ips"""
    try:
        from core.config.config_dict import ConfigDict
        config = ConfigDict.get_instance()

        # 读取现有 worker ips
        existing = list(Application.K8S_CONFIG.WORKER_IPS)

        # 追加新节点
        merged = existing + [w for w in new_workers if w not in existing]

        # 更新配置对象
        if not hasattr(config, 'kubernetes') or config.kubernetes is None:
            config.kubernetes = ConfigDict({})
        if not hasattr(config.kubernetes, 'worker') or config.kubernetes.worker is None:
            config.kubernetes.worker = ConfigDict({})

        config.kubernetes.worker.ips = merged

        # 保存到文件
        config_path = os.path.join(
            Application.ROOT_DIR, "config", "application.yaml")
        config.save_to_file(config_path)

        click.echo(click.style(
            f"  配置已更新: worker.ips = {merged}", fg="green"))
    except Exception as e:
        click.echo(click.style(
            f"  配置更新失败（扩容已成功，请手动更新 application.yaml）: {e}",
            fg="yellow"))


@cli.command(name="init-harbor")
@click.option(
    '--timeout',
    default=600,
    type=int,
    help="等待 Harbor 就绪的最大时间（秒），默认 600"
)
@click.option(
    '--interval',
    default=10,
    type=int,
    help="轮询 Harbor 就绪的间隔（秒），默认 10"
)
def init_harbor(timeout: int, interval: int) -> None:
    """
    初始化 Harbor 项目

    在 Harbor 镜像仓库中创建默认公开项目（apps、charts），
    并扫描 /etc/containerd/certs.d/ 目录下所有已配置代理的 registry，
    为每个 registry 在 Harbor 中创建同名公开项目。

    会先等待 Harbor 服务就绪，再幂等地创建项目（已存在则跳过）。

    示例：
    $ python k8s.py init-harbor
    $ python k8s.py init-harbor --timeout 900 --interval 15
    """
    # 1. 默认项目
    projects: list = [("apps", True), ("charts", True)]

    # 2. 扫描 certs.d 目录，收集已配置代理的 registry
    certs_d_path = Path("/etc/containerd/certs.d")
    if certs_d_path.exists():
        existing_names = {name for name, _ in projects}
        for d in certs_d_path.iterdir():
            if d.is_dir() and (d / "hosts.toml").exists():
                if d.name not in existing_names:
                    projects.append((d.name, True))
                    existing_names.add(d.name)

    click.echo(click.style("开始初始化 Harbor 项目...", fg="blue", bold=True))
    click.echo(f"Harbor 地址: https://{Application.DOMAIN}")
    click.echo(f"待初始化项目: {', '.join(name for name, _ in projects)}")

    client = HarborClient()

    # 等待 Harbor 就绪
    click.echo(f"等待 Harbor 服务就绪（超时 {timeout}秒）...")
    if not client.wait_for_ready(timeout=timeout, interval=interval):
        click.echo(
            click.style(
                "Harbor 服务未就绪，请稍后重试，或执行 kubectl get pods -n harbor-system 检查状态",
                fg="red",
            ),
            err=True,
        )
        exit(1)
    click.echo(click.style("Harbor 服务已就绪", fg="green"))

    # 创建项目
    success_count = 0
    for project_name, public in projects:
        visibility = "公开" if public else "私有"
        if client.create_project(project_name, public):
            click.echo(
                click.style(
                    f"项目 '{project_name}' 就绪（{visibility}）", fg="green"
                )
            )
            success_count += 1
        else:
            click.echo(
                click.style(f"项目 '{project_name}' 初始化失败", fg="red"),
                err=True,
            )

    if success_count == len(projects):
        click.echo(click.style("Harbor 项目初始化完成！", fg="green", bold=True))
        exit(0)
    else:
        click.echo(
            click.style(
                f"部分项目初始化失败（{success_count}/{len(projects)}）",
                fg="red",
            ),
            err=True,
        )
        exit(1)


@cli.command(name="push-images")
@click.option(
    '--dry-run',
    is_flag=True,
    help="仅展示将要推送的镜像，不实际推送"
)
@click.option(
    '--image-timeout',
    default=600,
    type=int,
    help="单个镜像推送超时（秒），默认 600"
)
def push_images(dry_run: bool, image_timeout: int) -> None:
    """推送集群所有节点的镜像到 Harbor 镜像仓库

    遍历集群全部节点（Master/Worker），收集 containerd 中已存在的镜像，
    去重后统一推送至 Harbor。后续集群扩容时，新节点可直接从 Harbor 拉取，
    无需再通过离线 tar 包加载镜像。

    \b
    示例：
      # 推送所有节点镜像到 Harbor
      $ python k8s.py push-images

      # 仅预览将推送的镜像清单
      $ python k8s.py push-images --dry-run
    """
    domain = Application.DOMAIN
    registry_user = Application.REGISTRY.USERNAME
    registry_pass = Application.REGISTRY.PASSWORD

    # 解析所有节点（@local 表示本机 Master）
    hosts = ["@local", *Application.K8S_CONFIG.ADDITIONAL_MASTER_IPS,
             *Application.K8S_CONFIG.WORKER_IPS]

    click.echo(click.style("=" * 70, fg="blue"))
    click.echo(click.style("推送集群节点镜像到 Harbor", fg="blue", bold=True))
    click.echo(click.style("=" * 70, fg="blue"))
    click.echo(f"  Harbor 地址:  https://{domain}")
    click.echo(f"  集群节点:     {', '.join(h.replace('@local', '本机') for h in hosts)}")
    click.echo(f"  dry-run:      {'是' if dry_run else '否'}")
    click.echo("")

    # ---- 1. 并行收集各节点镜像清单 ----
    click.echo("正在收集各节点镜像清单...")

    list_cmd = "ctr -n k8s.io i ls -q"

    async def _collect_images() -> Dict[str, List[str]]:
        ssh_client = AsyncSSHClient()
        node_images: Dict[str, List[str]] = {}
        try:
            # 本机
            local_res = execute_command(list_cmd, timeout=120)
            local_images = [
                line.strip() for line in local_res.get_output_lines()
                if line.strip()
            ] if not local_res.is_failure() else []
            node_images["@local"] = local_images

            # 远程节点并行收集
            remote_hosts = [h for h in hosts if h != "@local"]
            if remote_hosts:
                results = await ssh_client.execute_multiple_commands(
                    [(h, list_cmd) for h in remote_hosts],
                    connect_timeout=30
                )
                for item in results:
                    host = item.get("host", "")
                    stdout = str(item.get("stdout", "") or "")
                    if item.get("exit_status") == 0 and stdout:
                        node_images[host] = [
                            line.strip() for line in stdout.splitlines()
                            if line.strip()
                        ]
                    else:
                        node_images[host] = []
                        click.echo(click.style(
                            f"  ⚠ {host}: 镜像列表获取失败",
                            fg="yellow"))
        finally:
            await ssh_client.close_all_connections()
        return node_images

    node_images = asyncio.run(_collect_images())

    for host, imgs in node_images.items():
        label = "本机" if host == "@local" else host
        click.echo(f"  {label}: {len(imgs)} 个镜像")

    # ---- 2. 汇总去重，过滤已在 Harbor 的镜像 ----
    image_hosts: Dict[str, List[str]] = {}
    for host, imgs in node_images.items():
        for ref in imgs:
            # 跳过无标签的 sha256 digest 引用
            if ref.startswith("sha256:"):
                continue
            # 跳过已在 Harbor（以 domain 为前缀）的镜像
            if ref.startswith(f"{domain}/"):
                continue
            image_hosts.setdefault(ref, []).append(host)

    all_images = sorted(image_hosts.keys())

    click.echo(click.style(
        f"\n去重后待推送镜像: {len(all_images)} 个", fg="cyan"))

    if not all_images:
        click.echo(click.style("没有需要推送的镜像", fg="green"))
        exit(0)

    if dry_run:
        for ref in all_images:
            click.echo(f"  [dry-run] {ref}")
        exit(0)

    # ---- 3. 等待 Harbor 就绪 ----
    harbor_client = HarborClient()
    click.echo("等待 Harbor 服务就绪...")
    if not harbor_client.wait_for_ready(timeout=300, interval=10):
        click.echo(click.style(
            "Harbor 未就绪，请先执行 kubectl get pods -n harbor-system 检查",
            fg="red"), err=True)
        exit(1)
    click.echo(click.style("Harbor 已就绪", fg="green"))

    # 确保所有 registry 对应的 Harbor 项目存在
    needed_registries: set = set()
    for ref in all_images:
        registry, _ = _split_image_ref(ref)
        needed_registries.add(registry)
    for project in sorted(needed_registries):
        harbor_client.create_project(project, public=True)

    # ---- 4. 逐个推送（单事件循环，复用 SSH 连接池）----
    click.echo(click.style("\n开始推送镜像...", fg="cyan", bold=True))

    async def _push_all() -> Tuple[int, int, List[str]]:
        ssh_client = AsyncSSHClient()
        ok_count, fail_count = 0, 0
        failed_images: List[str] = []
        try:
            for idx, ref in enumerate(all_images, 1):
                # 优先从本机推送（更快），本机没有则从首个拥有该镜像的远程节点推送
                candidate_hosts = image_hosts[ref]
                push_host = "@local" if "@local" in candidate_hosts else candidate_hosts[0]
                label = "本机" if push_host == "@local" else push_host
                click.echo(f"  [{idx}/{len(all_images)}] {ref}  (from {label})")

                push_cmd = (
                    f"ctr -n k8s.io i push --hosts-dir /etc/containerd/certs.d/ "
                    f"-u {registry_user}:{registry_pass} {ref}"
                )

                if push_host == "@local":
                    res = execute_command(push_cmd, timeout=image_timeout)
                    failed = res.is_failure()
                    err_msg = res.get_error_lines()
                else:
                    result = await ssh_client.execute_command(
                        push_host, push_cmd, connect_timeout=30)
                    failed = result.get("exit_status") != 0
                    err_msg = str(result.get("stderr", "") or result.get("error", ""))

                if failed:
                    click.echo(click.style(
                        f"    ❌ 推送失败: {err_msg}", fg="red"))
                    fail_count += 1
                    failed_images.append(ref)
                else:
                    click.echo(click.style("    ✅ 成功", fg="green"))
                    ok_count += 1
        finally:
            await ssh_client.close_all_connections()
        return ok_count, fail_count, failed_images

    ok_count, fail_count, failed_images = asyncio.run(_push_all())

    # ---- 汇总 ----
    click.echo(click.style("\n" + "=" * 70, fg="blue"))
    click.echo(click.style("推送完成", fg="blue", bold=True))
    click.echo(click.style("=" * 70, fg="blue"))
    click.echo(f"  成功: {ok_count}")
    click.echo(f"  失败: {fail_count}")
    if needed_registries:
        click.echo(f"  涉及 Harbor 项目: {', '.join(sorted(needed_registries))}")
    if failed_images:
        click.echo(click.style("\n失败镜像:", fg="red"))
        for ref in failed_images:
            click.echo(f"    {ref}")

    exit(0 if fail_count == 0 else 1)


# ======================== Bitnami 同步辅助函数 ========================

def _bitnami_extract_images(chart_yaml_path: Path) -> List[str]:
    """从 Chart.yaml 的 annotations.images 提取完整镜像清单

    bitnami chart 的 Chart.yaml 包含 annotations.images 字段（block scalar），
    列出该 chart 依赖的全部镜像（含完整 registry 和 tag），比解析 values.yaml 更可靠。

    Args:
        chart_yaml_path: Chart.yaml 文件路径

    Returns:
        镜像引用列表，如 ["docker.io/bitnami/redis:8.2.1-debian-12-r0", ...]
    """
    with open(chart_yaml_path, "r", encoding="utf-8") as f:
        chart = yaml.safe_load(f)
    images_block = (chart.get("annotations") or {}).get("images", "")
    if not images_block:
        return []
    image_items = yaml.safe_load(images_block) or []
    return [item["image"] for item in image_items if isinstance(item, dict) and "image" in item]


def _split_image_ref(ref: str) -> Tuple[str, str]:
    """拆分镜像引用为 (registry, repository_with_tag)

    判断首段是否为 registry 地址（含 . 或 :），否则视为 docker.io。

    Examples:
        docker.io/bitnami/redis:8.2.1 -> ("docker.io", "bitnami/redis:8.2.1")
        quay.io/jetstack/cert-manager:v1.0 -> ("quay.io", "jetstack/cert-manager:v1.0")
        nginx:1.25 -> ("docker.io", "library/nginx:1.25")
    """
    parts = ref.split("/", 1)
    if len(parts) == 2 and ("." in parts[0] or ":" in parts[0]):
        return parts[0], parts[1]
    return "docker.io", f"library/{ref}" if "/" not in ref else ref


def _parse_image_ref(ref: str) -> Dict[str, str]:
    """将镜像引用解析为各组成部分，用于镜像源地址模板渲染

    解析 docker.io/bitnami/kubectl:1.33.4-debian-12-r0 得到：
      {
        "original": "docker.io/bitnami/kubectl:1.33.4-debian-12-r0",
        "registry": "docker.io",
        "repo":     "bitnami/kubectl",
        "name":     "kubectl",
        "tag":      "1.33.4-debian-12-r0"
      }
    """
    registry, repo_tag = _split_image_ref(ref)
    if ":" in repo_tag:
        repo, tag = repo_tag.rsplit(":", 1)
    else:
        repo, tag = repo_tag, "latest"
    name = repo.rsplit("/", 1)[-1] if "/" in repo else repo
    return {
        "original": ref,
        "registry": registry,
        "repo": repo,
        "name": name,
        "tag": tag,
    }


def _render_template(template: str, parts: Dict[str, str]) -> str:
    """用镜像组成部分渲染模板，支持 {original}/{registry}/{repo}/{name}/{tag}"""
    out = template
    for key, val in parts.items():
        out = out.replace("{" + key + "}", val)
    return out


# ======================== sync-bitnami 命令组 ========================

@cli.group(name="sync-bitnami")
def sync_bitnami() -> None:
    """同步 bitnami charts 到 Harbor（离线三步：export + pull-images + import）

    \b
    流程（适配互联网环境与纯内网环境）：
      1. export       —— 打包 chart + 依赖 + 镜像清单
      2. pull-images  —— 【互联网】经国内镜像源逐个下载镜像并导出 tar
      3. import       —— 【内网】将 chart 与镜像 tar 导入 Harbor

    \b
    示例：
      # 1. 打包 redis（chart + 依赖 + 镜像清单）
      $ python k8s.py sync-bitnami export redis

      # 2. 互联网机器：经镜像源下载镜像（支持模板 + 逐个覆盖）
      $ python k8s.py sync-bitnami pull-images /tmp/bitnami-bundles/redis-images.txt \\
          --template 'swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/bitnamilegacy/{name}:{tag}'

      # 3. 内网机器：导入到 Harbor
      $ python k8s.py sync-bitnami import /tmp/bitnami-bundles/redis-21.0.1.tgz \\
          --images-tar /tmp/bitnami-bundles/redis-images.images.tar
    """
    pass


# -------------------- export 子命令（互联网环境） --------------------

@sync_bitnami.command(name="export")
@click.argument('charts', nargs=-1)
@click.option(
    '--charts-dir',
    default='/opt/charts/bitnami',
    show_default=True,
    help="bitnami charts 根目录"
)
@click.option(
    '-o', '--output-dir',
    default='/tmp/bitnami-bundles',
    show_default=True,
    help="离线产物输出目录"
)
@click.option(
    '--dry-run',
    is_flag=True,
    help="仅展示将执行的操作，不实际执行"
)
def sync_export(
    charts: tuple,
    charts_dir: str,
    output_dir: str,
    dry_run: bool
) -> None:
    """【阶段一·互联网环境】打包 bitnami chart + 依赖 + 镜像清单

    将目标 chart 源码拷贝到工作目录，从本地 charts-dir 拷贝依赖项到
    charts/ 子目录，然后 helm package 打包（目标 chart 和依赖项分别打包）。
    同时从 Chart.yaml annotations.images 提取镜像清单写入文件。

    \b
    产物结构（直接输出到 output_dir，不打包 bundle）：
      {chart}-{ver}.tgz       目标 chart 包
      {dep}-{ver}.tgz         依赖 chart 包（如 common）
      {chart}-images.txt      镜像清单（每行一个镜像引用）

    示例：
      $ python k8s.py sync-bitnami export redis
      $ python k8s.py sync-bitnami export redis postgresql -o /data/bundles
    """
    import tempfile

    charts_root = Path(charts_dir)
    if not charts_root.exists():
        click.echo(click.style(f"charts 目录不存在: {charts_dir}", fg="red"), err=True)
        exit(1)

    # 确定待导出的 chart 列表
    if charts:
        chart_names = list(charts)
    else:
        chart_names = sorted([
            d.name for d in charts_root.iterdir()
            if d.is_dir() and (d / "Chart.yaml").exists()
        ])

    if not chart_names:
        click.echo(click.style("未找到可导出的 chart", fg="red"), err=True)
        exit(1)

    click.echo(click.style("=" * 70, fg="blue"))
    click.echo(click.style("[阶段一] 打包 bitnami chart + 依赖 + 镜像清单", fg="blue", bold=True))
    click.echo(click.style("=" * 70, fg="blue"))
    click.echo(f"  charts 目录:     {charts_dir}")
    click.echo(f"  输出目录:        {output_dir}")
    click.echo(f"  待导出 chart:    {', '.join(chart_names)}")
    click.echo(f"  dry-run:         {'是' if dry_run else '否'}")
    click.echo("")

    if not dry_run:
        Path(output_dir).mkdir(parents=True, exist_ok=True)

    ok_count, fail_count = 0, 0

    for chart_name in chart_names:
        chart_dir = charts_root / chart_name
        chart_yaml = chart_dir / "Chart.yaml"

        if not chart_yaml.exists():
            click.echo(click.style(f"[{chart_name}] Chart.yaml 不存在，跳过", fg="yellow"))
            fail_count += 1
            continue

        # 读取 chart 版本和依赖
        try:
            with open(chart_yaml, "r", encoding="utf-8") as f:
                chart_meta = yaml.safe_load(f)
            chart_version = chart_meta.get("version", "")
            dependencies = chart_meta.get("dependencies", []) or []
        except Exception as e:
            click.echo(click.style(f"[{chart_name}] 读取 Chart.yaml 失败: {e}", fg="red"), err=True)
            fail_count += 1
            continue

        dep_names = [d.get("name") for d in dependencies if d.get("name")]

        click.echo(click.style(f"\n[{chart_name}] (v{chart_version})", fg="cyan", bold=True))
        if dep_names:
            click.echo(f"  依赖项: {', '.join(dep_names)}")

        # 提取镜像清单
        images = _bitnami_extract_images(chart_yaml)
        click.echo(f"  镜像数: {len(images)}")

        if dry_run:
            click.echo(f"  [dry-run] 拷贝 {chart_name} -> 工作目录")
            for dn in dep_names:
                dep_src = charts_root / dn
                if dep_src.exists():
                    click.echo(f"  [dry-run] 拷贝依赖 {dn} -> charts/{dn}")
                    click.echo(f"  [dry-run] helm package {dn} -> {output_dir}/")
                else:
                    click.echo(click.style(
                        f"  [dry-run] 依赖 {dn} 在 {charts_dir} 未找到", fg="yellow"))
            click.echo(f"  [dry-run] helm package {chart_name} -> {output_dir}/")
            click.echo(f"  [dry-run] 镜像清单 -> {output_dir}/{chart_name}-images.txt")
            ok_count += 1
            continue

        # 使用临时工作目录
        work_dir = Path(tempfile.mkdtemp(prefix=f"sync_{chart_name}_"))
        try:
            # ---- 1. 拷贝目标 chart 到工作目录 ----
            work_chart_dir = work_dir / chart_name
            shutil.copytree(chart_dir, work_chart_dir)
            click.echo(f"  拷贝 chart -> {work_chart_dir}")

            # ---- 2. 拷贝依赖项到 charts/ 子目录 ----
            charts_subdir = work_chart_dir / "charts"
            charts_subdir.mkdir(exist_ok=True)
            dep_packed: List[str] = []

            for dep_name in dep_names:
                dep_src = charts_root / dep_name
                if not dep_src.exists():
                    click.echo(click.style(
                        f"  依赖 {dep_name} 在 {charts_dir} 未找到，跳过", fg="yellow"))
                    continue

                dep_dest = charts_subdir / dep_name
                shutil.copytree(dep_src, dep_dest)
                click.echo(f"  拷贝依赖 {dep_name} -> charts/{dep_name}")

                # ---- 3. 依赖项也 helm package ----
                dep_pkg_result = execute_command(
                    f"helm package {dep_dest} -d {output_dir}",
                    timeout=60
                )
                if dep_pkg_result.is_failure():
                    click.echo(click.style(
                        f"  helm package 依赖 {dep_name} 失败: "
                        f"{dep_pkg_result.get_error_lines()}", fg="yellow"))
                else:
                    # 获取依赖版本用于日志
                    try:
                        dep_yaml = dep_src / "Chart.yaml"
                        with open(dep_yaml, "r", encoding="utf-8") as f:
                            dep_meta = yaml.safe_load(f)
                        dep_ver = dep_meta.get("version", "?")
                    except Exception:
                        dep_ver = "?"
                    click.echo(click.style(
                        f"  ✅ 依赖打包: {dep_name}-{dep_ver}.tgz", fg="green"))
                    dep_packed.append(dep_name)

            # ---- 4. helm package 目标 chart（依赖已就位） ----
            pkg_result = execute_command(
                f"helm package {work_chart_dir} -d {output_dir}",
                timeout=60
            )
            if pkg_result.is_failure():
                click.echo(click.style(
                    f"  helm package 失败: {pkg_result.get_error_lines()}", fg="red"), err=True)
                fail_count += 1
                continue

            tgz_name = f"{chart_name}-{chart_version}.tgz"
            click.echo(click.style(f"  ✅ chart 打包: {tgz_name}", fg="green"))

            # ---- 5. 生成镜像清单文件 ----
            images_file = Path(output_dir) / f"{chart_name}-images.txt"
            with open(images_file, "w", encoding="utf-8") as f:
                for img in images:
                    f.write(img + "\n")
            click.echo(click.style(
                f"  ✅ 镜像清单: {images_file.name} ({len(images)} 个)", fg="green"))

            ok_count += 1

        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    # ---- 汇总 ----
    click.echo(click.style("\n" + "=" * 70, fg="blue"))
    click.echo(click.style("导出完成", fg="blue", bold=True))
    click.echo(click.style("=" * 70, fg="blue"))
    click.echo(f"  成功 {ok_count}，失败 {fail_count}")
    if ok_count > 0:
        click.echo(click.style(f"  产物目录: {output_dir}", fg="cyan"))
        click.echo(click.style(
            "  下一步: 将 .tgz 和 *-images.txt 拷贝到内网，执行 sync-bitnami import",
            fg="cyan"))

    exit(0 if fail_count == 0 else 1)


# -------------------- pull-images 子命令（互联网环境） --------------------

@sync_bitnami.command(name="pull-images")
@click.argument('images_file', type=click.Path(exists=True))
@click.option(
    '-o', '--output',
    help='导出的镜像 tar 路径（默认: 与输入同目录的 {stem}.images.tar）'
)
@click.option(
    '-t', '--template',
    help='镜像源地址模板，支持 {original}/{registry}/{repo}/{name}/{tag}。'
         '例: swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/bitnamilegacy/{name}:{tag}'
)
@click.option(
    '-n', '--namespace',
    default='k8s.io',
    show_default=True,
    help='containerd namespace'
)
@click.option(
    '--timeout',
    type=int,
    default=600,
    show_default=True,
    help='单个镜像 pull 超时时间（秒）'
)
def sync_pull_images(
    images_file: str,
    output: Optional[str],
    template: Optional[str],
    namespace: str,
    timeout: int
) -> None:
    """【互联网环境】经国内镜像源批量下载镜像并导出 tar

    读取 export 生成的镜像清单文件，逐个镜像交互式确认镜像源地址，
    pull 后 tag 回原引用，最终批量导出为单个 tar，供拷贝到内网 import。

    \b
    支持模板占位符（--template），适配路径中段变化，例如 bitnami->bitnamilegacy：
      原引用: docker.io/bitnami/kubectl:1.33.4-debian-12-r0
      模板:   swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/bitnamilegacy/{name}:{tag}
      渲染后: swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/bitnamilegacy/kubectl:1.33.4-debian-12-r0

    \b
    每个镜像交互操作：
      - 回车: 使用模板渲染的地址
      - 输入完整地址: 本次使用该地址（模板渲染不符时覆盖）
      - 输入 s: 跳过该镜像（镜像源未同步时）
      - 输入 r: 直接用原引用从 docker.io 拉取（可直连场景）
    """
    img_path = Path(images_file)
    with open(img_path, "r", encoding="utf-8") as f:
        images = [
            line.strip() for line in f
            if line.strip() and not line.strip().startswith("#")
        ]

    if not images:
        click.echo(click.style("镜像清单为空", fg="red"), err=True)
        exit(1)

    if not output:
        output = str(img_path.parent / f"{img_path.stem}.images.tar")

    click.echo(click.style("=" * 70, fg="blue"))
    click.echo(click.style("[镜像下载] 经国内镜像源批量下载并导出 tar", fg="blue", bold=True))
    click.echo(click.style("=" * 70, fg="blue"))
    click.echo(f"  镜像清单:    {images_file} ({len(images)} 个)")
    click.echo(f"  模板:        {template or '无（需逐个输入）'}")
    click.echo(f"  输出 tar:    {output}")
    click.echo(f"  namespace:   {namespace}")
    click.echo(f"  超时:        {timeout}s/个")
    click.echo("")

    success_refs: List[str] = []
    total = len(images)

    for idx, original_ref in enumerate(images, 1):
        parts = _parse_image_ref(original_ref)
        default_ref = _render_template(template, parts) if template else ""

        click.echo(click.style(
            f"\n[{idx}/{total}] {original_ref}", fg="cyan", bold=True))

        # 交互获取镜像源地址（用 click.prompt 确保 stdout 刷新，避免卡死）
        mirror_ref = ""
        skip = False
        try:
            raw = click.prompt(
                f"  镜像源地址 [回车={'默认' if default_ref else '跳过'} s=跳过 r=原引用]",
                default=default_ref,
                show_default=False,
                prompt_suffix=": "
            ).strip()
        except (click.exceptions.Abort, KeyboardInterrupt, EOFError, SystemExit):
            click.echo("")
            click.echo(click.style("已中止", fg="yellow"))
            skip = True

        if not skip:
            low = raw.lower()
            if low in ("s", "skip"):
                skip = True
            elif low in ("r", "raw"):
                mirror_ref = original_ref
            elif not raw and not default_ref:
                skip = True
            else:
                mirror_ref = raw

        if skip:
            click.echo(click.style("  ⏭ 跳过", fg="yellow"))
            continue

        click.echo(f"  镜像源: {mirror_ref}")

        # ---- 1. 从镜像源 pull ----
        pull_res = execute_command(
            f"ctr -n {namespace} i pull {mirror_ref}",
            timeout=timeout
        )
        if pull_res.is_failure():
            click.echo(click.style(f"  ❌ pull 失败: {mirror_ref}", fg="red"))
            err = pull_res.get_error_lines()
            if err:
                click.echo(f"  {err}")
            continue
        click.echo(click.style("  ✅ pull 成功", fg="green"))

        # ---- 2. tag 回原引用 ----
        if mirror_ref != original_ref:
            tag_res = execute_command(
                f"ctr -n {namespace} i tag {mirror_ref} {original_ref}"
            )
            if tag_res.is_failure():
                # 目标引用可能已存在（重跑），删除后重试
                execute_command(
                    f"ctr -n {namespace} i rm {original_ref}",
                    timeout=30
                )
                tag_res = execute_command(
                    f"ctr -n {namespace} i tag {mirror_ref} {original_ref}"
                )
            if tag_res.is_failure():
                click.echo(click.style(
                    f"  ❌ tag 失败: {mirror_ref} -> {original_ref}", fg="red"))
                continue
            click.echo(click.style(f"  ✅ tag -> {original_ref}", fg="green"))

        success_refs.append(original_ref)

    # ---- 3. 批量导出 tar ----
    if not success_refs:
        click.echo(click.style("\n无成功镜像，跳过导出", fg="yellow"))
        exit(1)

    click.echo(click.style(
        f"\n导出 {len(success_refs)} 个镜像到 tar...", fg="blue"))
    export_cmd = (
        f"ctr -n {namespace} i export {output} "
        + " ".join(success_refs)
    )
    export_res = execute_command(
        export_cmd,
        timeout=max(300, timeout * len(success_refs))
    )
    if export_res.is_failure():
        click.echo(click.style(
            f"❌ 导出失败: {export_res.get_error_lines()}", fg="red"), err=True)
        exit(1)

    size_mb = Path(output).stat().st_size / (1024 * 1024)
    click.echo(click.style("=" * 70, fg="green"))
    click.echo(click.style("完成", fg="green", bold=True))
    click.echo(click.style("=" * 70, fg="green"))
    click.echo(f"  成功: {len(success_refs)}/{total}")
    click.echo(f"  产物: {output} ({size_mb:.1f} MB)")
    click.echo(click.style(
        "  下一步: 将 .tgz 与此 tar 拷贝到内网，执行 sync-bitnami import",
        fg="cyan"))


# -------------------- import 子命令（纯内网环境） --------------------


def _do_sync_import(
    tgzs: Tuple[str, ...],
    images_tar: Optional[str],
    images_file: Optional[str],
    dry_run: bool,
    image_timeout: int,
    harbor_client: Optional[HarborClient],
) -> Tuple[int, int, int, int, Set[str]]:
    """执行单个 bundle 的导入逻辑（chart push + 镜像 import+push）

    Returns:
        (chart_ok, chart_fail, image_ok, image_fail, needed_projects)
    """
    domain = Application.DOMAIN
    registry_user = Application.REGISTRY.USERNAME
    registry_pass = Application.REGISTRY.PASSWORD

    needed_projects: Set[str] = set()
    chart_ok, chart_fail = 0, 0
    image_ok, image_fail = 0, 0

    # 探测 images-file：未指定时根据 images-tar 名字推导
    resolved_images: List[str] = []
    if images_tar:
        if not Path(images_tar).exists():
            click.echo(click.style(
                f"  镜像 tar 不存在: {images_tar}", fg="red"), err=True)
            return chart_ok, chart_fail, 0, 0, needed_projects
        if not images_file:
            derived = images_tar.replace(".images.tar", ".txt")
            if not Path(derived).exists():
                derived = images_tar.replace(".tar", ".txt")
            images_file = derived if Path(derived).exists() else None

        if images_file and Path(images_file).exists():
            with open(images_file, "r", encoding="utf-8") as f:
                resolved_images = [
                    line.strip() for line in f
                    if line.strip() and not line.strip().startswith("#")
                ]

    # ---- 1. helm push 每个 chart 包 ----
    for tgz_str in tgzs:
        tgz_path = Path(tgz_str)
        if not tgz_path.exists():
            click.echo(click.style(
                f"  chart 包不存在: {tgz_path}", fg="red"), err=True)
            chart_fail += 1
            continue

        click.echo(click.style(f"\n  chart: {tgz_path.name}", fg="cyan"))

        if dry_run:
            click.echo(f"    [dry-run] helm push -> oci://{domain}/charts")
            chart_ok += 1
            continue

        push_res = execute_command(
            " ".join([
                "helm", "push", str(tgz_path),
                f"oci://{domain}/charts",
                f"--username {registry_user}",
                f"--password {registry_pass}",
            ]),
            timeout=120
        )
        if push_res.is_failure():
            click.echo(click.style(
                f"    ❌ helm push 失败: {push_res.get_error_lines()}",
                fg="red"), err=True)
            chart_fail += 1
        else:
            click.echo(click.style("    ✅ chart 推送成功", fg="green"))
            chart_ok += 1

    # ---- 2. 导入镜像 tar + push ----
    if images_tar:
        click.echo(click.style(
            f"\n  镜像导入: {Path(images_tar).name}", fg="cyan"))

        if dry_run:
            for img_ref in resolved_images:
                registry, repo_tag = _split_image_ref(img_ref)
                needed_projects.add(registry)
                target = f"{domain}/{registry}/{repo_tag}"
                click.echo(f"    [dry-run] {img_ref} -> {target}")
            image_ok = len(resolved_images)
        else:
            import_res = execute_command(
                f"ctr -n k8s.io i import {images_tar}",
                timeout=600
            )
            if import_res.is_failure():
                click.echo(click.style(
                    f"    ❌ images.tar 导入失败: {import_res.get_error_lines()}",
                    fg="red"), err=True)
                image_fail = len(resolved_images)
            elif not resolved_images:
                click.echo(click.style(
                    "    镜像已导入本地，但无清单文件无法 push", fg="yellow"))
            else:
                click.echo(click.style("    ✅ 镜像导入本地", fg="green"))
                for img_ref in resolved_images:
                    registry, repo_tag = _split_image_ref(img_ref)
                    needed_projects.add(registry)

                    if harbor_client and not harbor_client.create_project(
                            registry, public=True):
                        click.echo(click.style(
                            f"    Harbor 项目 '{registry}' 创建失败，跳过",
                            fg="yellow"))
                        image_fail += 1
                        continue

                    push_res = execute_command(
                        f"ctr -n k8s.io i push --hosts-dir /etc/containerd/certs.d/ "
                        f"-u {registry_user}:{registry_pass} {img_ref}",
                        timeout=image_timeout
                    )
                    if push_res.is_failure():
                        click.echo(click.style(
                            f"    ❌ push 失败: {img_ref}", fg="red"))
                        image_fail += 1
                    else:
                        click.echo(click.style(f"    ✅ {img_ref}", fg="green"))
                        image_ok += 1

    return chart_ok, chart_fail, image_ok, image_fail, needed_projects


@sync_bitnami.command(name="import")
@click.argument('tgzs', nargs=-1, required=True)
@click.option(
    '--images-tar',
    help='镜像 tar 路径（pull-images 产物），不提供则只导入 chart'
)
@click.option(
    '--images-file',
    help='镜像清单文件（export 产物），用于 tag+push；未指定时自动探测'
)
@click.option(
    '--dry-run',
    is_flag=True,
    help="仅展示将执行的操作，不实际执行"
)
@click.option(
    '--image-timeout',
    type=int,
    default=600,
    show_default=True,
    help="单个镜像 push 超时时间（秒）"
)
def sync_import(
    tgzs: tuple,
    images_tar: Optional[str],
    images_file: Optional[str],
    dry_run: bool,
    image_timeout: int
) -> None:
    """【阶段二·纯内网环境】将 chart 包与镜像 tar 导入 Harbor

    helm push 每个 .tgz 到 oci://{DOMAIN}/charts；
    若提供 --images-tar，则 ctr i import 后按清单 tag+push 到 Harbor。

    \b
    示例：
      # 仅导入 chart（含依赖）
      $ python k8s.py sync-bitnami import common-2.31.10.tgz redis-21.0.1.tgz

      # chart + 镜像一起导入
      $ python k8s.py sync-bitnami import redis-21.0.1.tgz \\
          --images-tar redis-images.images.tar --images-file redis-images.txt
    """
    domain = Application.DOMAIN
    registry_user = Application.REGISTRY.USERNAME
    registry_pass = Application.REGISTRY.PASSWORD

    click.echo(click.style("=" * 70, fg="blue"))
    click.echo(click.style("[阶段二] 导入 bitnami 离线包到 Harbor（内网环境）", fg="blue", bold=True))
    click.echo(click.style("=" * 70, fg="blue"))
    click.echo(f"  Harbor 地址:     https://{domain}")
    click.echo(f"  chart 包:        {len(tgzs)} 个")
    click.echo(f"  镜像 tar:        {images_tar or '无'}")
    click.echo(f"  dry-run:         {'是' if dry_run else '否'}")
    click.echo("")

    # 等待 Harbor 就绪
    harbor_client: Optional[HarborClient] = None
    if not dry_run:
        harbor_client = HarborClient()
        click.echo("等待 Harbor 服务就绪...")
        if not harbor_client.wait_for_ready(timeout=300, interval=10):
            click.echo(click.style(
                "Harbor 未就绪，请先执行 kubectl get pods -n harbor-system 检查",
                fg="red"), err=True)
            exit(1)
        click.echo(click.style("Harbor 已就绪", fg="green"))
    click.echo("")

    chart_ok, chart_fail, image_ok, image_fail, needed_projects = _do_sync_import(
        tgzs, images_tar, images_file, dry_run, image_timeout, harbor_client
    )

    # ---- 汇总 ----
    click.echo(click.style("\n" + "=" * 70, fg="blue"))
    click.echo(click.style("导入完成", fg="blue", bold=True))
    click.echo(click.style("=" * 70, fg="blue"))
    click.echo(f"  Chart:  成功 {chart_ok}，失败 {chart_fail}")
    if images_tar:
        click.echo(f"  镜像:   成功 {image_ok}，失败 {image_fail}")
    if needed_projects:
        click.echo(f"  涉及 Harbor 项目: {', '.join(sorted(needed_projects))}")
        click.echo(click.style(
            f"  提示: 执行 `python ctr.py add-proxy "
            f"{' '.join(sorted(needed_projects))}` 配置透明转发",
            fg="cyan"))

    exit(0 if (chart_fail == 0 and image_fail == 0) else 1)


@sync_bitnami.command(name="import-dir")
@click.argument('dir_path')
@click.option(
    '--dry-run',
    is_flag=True,
    help="仅展示将执行的操作，不实际执行"
)
@click.option(
    '--image-timeout',
    type=int,
    default=600,
    show_default=True,
    help="单个镜像 push 超时时间（秒）"
)
def sync_import_dir(
    dir_path: str,
    dry_run: bool,
    image_timeout: int
) -> None:
    """【阶段二·批量】扫描目录下所有 bitnami 离线包，批量导入 Harbor

    自动扫描 DIR_PATH 下的 *-bundles 子目录（若 DIR_PATH 本身即为单个
    bundle 目录也可），依次将每个 bundle 的 chart 包（含 common 依赖、
    子 chart 如 kibana）和镜像 tar 导入 Harbor。

    \b
    示例：
      # 批量导入 bitnami-bundles 下所有应用
      $ kubengine-k8s sync-bitnami import-dir /root/offline-deploy/bitnami-bundles

      # 仅导入单个应用
      $ kubengine-k8s sync-bitnami import-dir /root/offline-deploy/bitnami-bundles/redis-bitnami-bundles

      # 预览
      $ kubengine-k8s sync-bitnami import-dir /root/offline-deploy/bitnami-bundles --dry-run
    """
    base_dir = Path(dir_path)
    if not base_dir.is_dir():
        click.echo(click.style(f"目录不存在: {dir_path}", fg="red"), err=True)
        exit(1)

    def _is_bundle_dir(d: Path) -> bool:
        return any(d.glob("*.tgz"))

    # 判断是父目录（含多个 bundle 子目录）还是单个 bundle 目录
    if _is_bundle_dir(base_dir):
        bundle_dirs = [base_dir]
    else:
        bundle_dirs = sorted([
            d for d in base_dir.iterdir()
            if d.is_dir() and _is_bundle_dir(d)
        ])

    if not bundle_dirs:
        click.echo(click.style(
            f"未找到包含 chart 包的 bundle 目录: {dir_path}", fg="red"), err=True)
        exit(1)

    # 收集每个 bundle 的文件
    bundles: List[Tuple[str, List[str], Optional[str], Optional[str]]] = []
    for bdir in bundle_dirs:
        tgzs = sorted(str(f) for f in bdir.glob("*.tgz"))
        tar_files = sorted(bdir.glob("*.images.tar"))
        images_tar = str(tar_files[0]) if tar_files else None
        txt_files = sorted(bdir.glob("*-images.txt"))
        images_file = str(txt_files[0]) if txt_files else None
        bundles.append((bdir.name, tgzs, images_tar, images_file))

    # ---- 展示扫描结果 ----
    click.echo(click.style("=" * 70, fg="blue"))
    click.echo(click.style(
        "[阶段二·批量] 导入 bitnami 离线包到 Harbor", fg="blue", bold=True))
    click.echo(click.style("=" * 70, fg="blue"))
    click.echo(f"  扫描目录:  {dir_path}")
    click.echo(f"  Bundle 数: {len(bundles)}")
    click.echo(f"  dry-run:   {'是' if dry_run else '否'}")
    click.echo("")

    for idx, (name, tgzs, images_tar, images_file) in enumerate(bundles, 1):
        tgz_names = [Path(t).name for t in tgzs]
        click.echo(f"  [{idx}/{len(bundles)}] {name}")
        click.echo(f"        charts: {tgz_names}")
        click.echo(f"        images: {Path(images_tar).name if images_tar else '无'}"
                   f" ({images_file or '无清单'})")
    click.echo("")

    # ---- 等待 Harbor 就绪（仅一次）----
    harbor_client: Optional[HarborClient] = None
    if not dry_run:
        harbor_client = HarborClient()
        click.echo("等待 Harbor 服务就绪...")
        if not harbor_client.wait_for_ready(timeout=300, interval=10):
            click.echo(click.style(
                "Harbor 未就绪，请先执行 kubectl get pods -n harbor-system 检查",
                fg="red"), err=True)
            exit(1)
        click.echo(click.style("Harbor 已就绪", fg="green"))
    click.echo("")

    # ---- 逐个 bundle 导入 ----
    total_chart_ok, total_chart_fail = 0, 0
    total_image_ok, total_image_fail = 0, 0
    all_projects: Set[str] = set()

    for idx, (name, tgzs, images_tar, images_file) in enumerate(bundles, 1):
        click.echo(click.style(
            f"\n{'─' * 70}", fg="blue"))
        click.echo(click.style(
            f"[{idx}/{len(bundles)}] {name}", fg="blue", bold=True))
        click.echo(click.style(
            f"{'─' * 70}", fg="blue"))

        chart_ok, chart_fail, image_ok, image_fail, projects = _do_sync_import(
            tuple(tgzs), images_tar, images_file,
            dry_run, image_timeout, harbor_client
        )

        total_chart_ok += chart_ok
        total_chart_fail += chart_fail
        total_image_ok += image_ok
        total_image_fail += image_fail
        all_projects.update(projects)

    # ---- 汇总 ----
    click.echo(click.style("\n" + "=" * 70, fg="blue"))
    click.echo(click.style("批量导入完成", fg="blue", bold=True))
    click.echo(click.style("=" * 70, fg="blue"))
    click.echo(f"  Bundle 数:       {len(bundles)}")
    click.echo(f"  Chart:           成功 {total_chart_ok}，失败 {total_chart_fail}")
    click.echo(f"  镜像:            成功 {total_image_ok}，失败 {total_image_fail}")
    if all_projects:
        click.echo(f"  涉及 Harbor 项目: {', '.join(sorted(all_projects))}")
    click.echo("")

    exit(0 if (total_chart_fail == 0 and total_image_fail == 0) else 1)


if __name__ == '__main__':
    cli()
