"""
集群管理命令行工具模块

提供集群配置相关的 CLI 命令，支持：
- 批量配置集群主机名并持久化
- 配置节点间 SSH 互信
- 节点可达性检测和验证
- 批量执行命令

使用示例：
    # 配置集群
    python cluster.py configure-cluster --hosts 172.31.65.150,localhost \\
        --hostname-map 172.31.65.150:node-1,localhost:node-2

    # 显示集群配置
    python cluster.py show-cluster-config

    # 执行命令
    python cluster.py execute-cmd --cmd "df -h"

    # 禁用防火墙
    python cluster.py disable-firewalld
"""

import asyncio
import os
from typing import Any, Dict, List, Optional, Tuple, Union

import click

from cli.models import ListType
from core.config import Application
from core.config.config_dict import ConfigDict
from core.ssh import AsyncSSHClient


# ============================ 命令行工具 ============================


@click.group()
def cli() -> None:
    """
    集群管理命令行工具主入口

    核心功能：\n
        1. 批量配置集群主机名并持久化\n
        2. 配置节点间 SSH 互信\n
        3. 节点可达性检测和验证\n
    """
    pass


# ============================ 辅助函数 ============================


def _load_cluster_config() -> Optional[Dict[str, Any]]:
    """从应用配置文件加载集群配置

    Returns:
        包含集群配置的字典，未找到则返回 None
    """
    try:
        config = ConfigDict.get_instance()

        if (hasattr(config, 'cluster') and config.cluster is not None):
            cluster_config = config.cluster

            # 提取节点和主机名映射
            nodes = getattr(cluster_config, 'nodes', [])
            hostnames = getattr(cluster_config, 'hostnames', {})

            if nodes and hostnames:
                return {
                    'hosts': nodes,
                    'hostnames': hostnames
                }

        return None

    except Exception as e:
        click.echo(
            click.style(
                f"加载集群配置失败: {str(e)}", fg="yellow")
        )
        return None


def _get_cluster_hosts(
    hosts: Optional[List[str]] = None
) -> List[str]:
    """从输入或配置文件获取集群主机列表

    Args:
        hosts: 用户提供的主机列表

    Returns:
        集群主机列表
    """
    if hosts:
        return hosts

    # 尝试从配置文件加载
    cluster_config = _load_cluster_config()
    if cluster_config and cluster_config.get('hosts'):
        click.echo(
            click.style("已从配置文件加载主机列表", fg="green")
        )
        return cluster_config['hosts']

    click.echo(
        click.style(
            "未找到主机配置。"
            "请提供 --hosts 选项或先配置集群。",
            fg="yellow"
        )
    )
    return []


def _get_cluster_hostname_map(
    hostname_map: Optional[str] = None,
    hosts: Optional[List[str]] = None
) -> Dict[str, str]:
    """从输入或配置文件获取集群主机名映射

    Args:
        hostname_map: 用户提供的主机名映射字符串
        hosts: 用于验证的主机列表

    Returns:
        主机到主机名的映射字典
    """
    if hostname_map:
        return _parse_hostname_mapping(hostname_map)

    # 尝试从配置文件加载
    cluster_config = _load_cluster_config()
    if cluster_config and cluster_config.get('hostnames'):
        click.echo(
            click.style(
                "已从配置文件加载主机名映射", fg="green")
        )
        return cluster_config['hostnames']

    click.echo(
        click.style(
            "未找到主机名映射配置。"
            "请提供 --hostname-map 选项。",
            fg="yellow"
        )
    )
    return {}


def _parse_hostname_mapping(hostname_map: str) -> Dict[str, str]:
    """解析逗号分隔的主机名映射字符串

    Args:
        hostname_map: 逗号分隔的 IP:主机名 对

    Returns:
        IP 到主机名的映射字典

    Raises:
        ValueError: 格式无效时抛出
    """
    host_hostname_dict: Dict[str, str] = {}

    try:
        for item in hostname_map.split(","):
            item = item.strip()
            if ":" not in item:
                raise ValueError(
                    f"格式无效: {item} (应为 IP:主机名)"
                )

            ip, hostname = item.split(":", 1)
            host_hostname_dict[ip.strip()] = hostname.strip()

        return host_hostname_dict

    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"解析主机名映射失败: {str(e)}")


def _validate_host_hostname_mapping(
    hosts: List[str],
    host_hostname_map: Dict[str, str]
) -> None:
    """验证所有主机都有对应的主机名映射

    Args:
        hosts: 主机 IP 列表
        host_hostname_map: 主机名映射字典

    Raises:
        ValueError: 任何主机缺少主机名映射时抛出
    """
    for host in hosts:
        if host not in host_hostname_map:
            raise ValueError(f"主机 {host} 缺少主机名映射")


def _build_ssh_params(
    username: str,
    password: str,
    key_file: str
) -> Dict[str, Any]:
    """构建 SSH 连接参数

    Args:
        username: SSH 用户名
        password: SSH 密码
        key_file: SSH 私钥文件路径

    Returns:
        SSH 连接参数字典
    """
    ssh_kwargs: Dict[str, Union[str, List[str]]] = {"username": username}

    if password:
        ssh_kwargs["password"] = password
    else:
        ssh_kwargs["client_keys"] = [os.path.expanduser(key_file)]

    return ssh_kwargs


# ============================ 工作流函数 ============================


async def configure_cluster_workflow(
    hosts: List[str],
    verify_ssh: bool,
    host_hostname_map: Dict[str, str],
    **ssh_kwargs: Any
) -> None:
    """执行集群配置工作流

    Args:
        hosts: 目标集群节点 IP 列表
        verify_ssh: 是否验证 SSH 互信结果
        host_hostname_map: IP 到主机名的映射字典
        **ssh_kwargs: SSH 连接参数
    """
    ssh_client = AsyncSSHClient()

    try:
        # 步骤 1: 检查节点可达性
        click.echo(click.style(
            "\n=== 检查节点可达性 ===", fg="blue"))
        reachable_hosts, unreachable_hosts = await ssh_client.is_reachable(
            hosts, **ssh_kwargs
        )

        if unreachable_hosts:
            click.echo(
                click.style(
                    f"警告: 不可达节点: {', '.join(unreachable_hosts)}",
                    fg="yellow"
                )
            )

            if not click.confirm(
                "是否继续仅配置可达节点？",
                default=False
            ):
                click.echo("用户取消配置")
                return

            # 过滤为仅可达节点
            hosts = reachable_hosts
            host_hostname_map = {
                host: host_hostname_map[host]
                for host in reachable_hosts
                if host in host_hostname_map
            }

        # 步骤 2: 设置主机名
        click.echo(click.style(
            "\n=== 设置集群主机名 ===", fg="blue"))
        hostname_results = await ssh_client.set_hostnames(
            host_hostname_map, **ssh_kwargs
        )

        for result in hostname_results:
            if result.get('error'):
                click.echo(
                    click.style(
                        f"主机 {result['host']} 主机名设置失败: {result['error']}",
                        fg="red"
                    )
                )
            else:
                hostname = host_hostname_map.get(
                    str(result['host']), 'unknown')
                click.echo(
                    click.style(
                        f"主机 {result['host']} 主机名设置为 {hostname}",
                        fg="green"
                    )
                )

        # 步骤 3: 配置 SSH 互信
        click.echo(click.style("\n=== 配置 SSH 互信 ===", fg="blue"))
        trust_result = await ssh_client.setup_ssh_mutual_trust(
            hosts, **ssh_kwargs
        )

        if trust_result.get('error'):
            click.echo(
                click.style(
                    f"SSH 互信配置失败: {trust_result['error']}",
                    fg="red"
                )
            )
        else:
            click.echo(
                click.style(
                    "SSH 互信配置成功！",
                    fg="green"
                )
            )

        # 步骤 4: 验证 SSH 互信（可选）
        if verify_ssh and not trust_result.get('error'):
            await _verify_ssh_trust(ssh_client, hosts, **ssh_kwargs)

        # 步骤 5: 保存集群配置
        await _save_cluster_config(hosts, host_hostname_map)

    except Exception as e:
        click.echo(
            click.style(f"配置工作流失败: {str(e)}", fg="red"),
            err=True
        )
    finally:
        await ssh_client.close_all_connections()
        click.echo(click.style(
            "\n=== 所有 SSH 连接已关闭 ===", fg="blue"))


async def _verify_ssh_trust(
    ssh_client: AsyncSSHClient,
    hosts: List[str],
    **ssh_kwargs: Any
) -> None:
    """验证集群节点间的 SSH 互信

    Args:
        ssh_client: SSH 客户端实例
        hosts: 要验证的主机列表
        **ssh_kwargs: SSH 连接参数
    """
    click.echo(click.style("\n=== 验证 SSH 互信 ===", fg="blue"))

    # 构建验证任务
    verify_tasks: List[Tuple[str, str]] = []
    for src_host in hosts:
        for dest_host in hosts:
            if src_host != dest_host:
                cmd = (
                    f'ssh -o StrictHostKeyChecking=no '
                    f'-o ConnectTimeout=5 {dest_host} "echo success"'
                )
                verify_tasks.append((src_host, cmd))

    # 执行验证任务
    verify_results = await ssh_client.execute_multiple_commands(
        verify_tasks, **ssh_kwargs
    )

    # 处理验证结果
    for result in verify_results:
        command_parts = str(result['command']).split()
        dest_host = command_parts[3] if len(command_parts) > 3 else 'unknown'

        if (result.get('exit_status') == 0 and 'success' in str(result.get('stdout', ''))):
            click.echo(
                click.style(
                    f"SSH 互信验证成功: {result['host']} → {dest_host}",
                    fg="green"
                )
            )
        else:
            error_info = result.get('error') or result.get(
                'stderr', '未知错误')
            click.echo(
                click.style(
                    f"SSH 互信验证失败: {result['host']} → {dest_host}: {error_info}",
                    fg="red"
                )
            )


async def _save_cluster_config(
    hosts: List[str],
    host_hostname_map: Dict[str, str]
) -> None:
    """保存集群配置到全局配置文件

    Args:
        hosts: 集群节点 IP 列表
        host_hostname_map: 主机名映射
    """
    try:
        config = ConfigDict.get_instance()

        # 初始化集群配置（如果不存在）
        if not hasattr(config, 'cluster') or config.cluster is None:
            config.cluster = ConfigDict({})

        # 保存集群信息
        config.cluster.nodes = hosts
        config.cluster.hostnames = host_hostname_map

        # 保存到文件
        config_path = os.path.join(
            Application.ROOT_DIR, "config", "application.yaml"
        )
        config.save_to_file(config_path)

        click.echo(
            click.style(
                f"\n集群配置已保存到 {config_path}",
                fg="green"
            )
        )

    except Exception as e:
        click.echo(
            click.style(f"保存集群配置失败: {str(e)}", fg="red"),
            err=True
        )


async def execute_command_workflow(
    hosts: List[str],
    command: str,
    **ssh_kwargs: Any
) -> None:
    """在集群节点上执行命令

    Args:
        hosts: 目标集群节点 IP 列表
        command: 要执行的命令
        **ssh_kwargs: SSH 连接参数
    """
    ssh_client = AsyncSSHClient()

    try:
        # 检查节点可达性
        click.echo(click.style(
            "\n=== 检查节点可达性 ===", fg="blue"))
        reachable_hosts, unreachable_hosts = await ssh_client.is_reachable(
            hosts, **ssh_kwargs
        )

        if unreachable_hosts:
            click.echo(
                click.style(
                    f"跳过不可达节点: {', '.join(unreachable_hosts)}",
                    fg="yellow"
                )
            )

        if not reachable_hosts:
            click.echo(
                click.style("没有可达节点！", fg="red"),
                err=True
            )
            return

        # 构建命令执行任务
        click.echo(
            click.style(
                f"\n=== 在 {len(reachable_hosts)} 个节点上执行命令: {command} ===",
                fg="blue"
            )
        )
        cmd_tasks = [(host, command) for host in reachable_hosts]

        # 执行命令
        results = await ssh_client.execute_multiple_commands(
            cmd_tasks, **ssh_kwargs
        )

        # 显示结果
        click.echo(click.style(
            "\n=== 命令执行结果 ===", fg="blue"))
        for result in results:
            _display_command_result(result)

    except Exception as e:
        click.echo(
            click.style(f"命令执行失败: {str(e)}", fg="red"),
            err=True
        )
    finally:
        await ssh_client.close_all_connections()
        click.echo(click.style(
            "\n=== 所有 SSH 连接已关闭 ===", fg="blue"))


def _display_command_result(result: Dict[str, Any]) -> None:
    """以格式化方式显示命令执行结果

    Args:
        result: 命令执行结果字典
    """
    host = result['host']
    click.echo(click.style(f"\n【节点 {host}】", fg="cyan", bold=True))

    if result.get('error'):
        click.echo(click.style("  状态: 失败", fg="red"))
        click.echo(click.style(f"  错误: {result['error']}", fg="red"))
    else:
        exit_status = result.get('exit_status', -1)
        status = "成功" if exit_status == 0 else f"退出码: {exit_status}"
        status_color = "green" if exit_status == 0 else "yellow"

        click.echo(click.style(f"  状态: {status}", fg=status_color))

        stdout = result.get('stdout', '').strip()
        if stdout:
            click.echo(click.style("  标准输出:", fg="blue"))
            click.echo(f"    {stdout}")

        stderr = result.get('stderr', '').strip()
        if stderr:
            click.echo(click.style("  标准错误:", fg="red"))
            click.echo(f"    {stderr}")


# ============================ CLI 命令 ============================


@cli.command(name="configure-cluster")
@click.option(
    "--hosts",
    type=ListType,
    help="集群节点 IP 列表，逗号分隔 (如: 172.31.65.150,localhost)"
)
@click.option(
    "--hostname-map",
    help="IP 到主机名的映射，逗号分隔 (如: 172.31.65.150:node-1,localhost:node-2)"
)
@click.option(
    "--username",
    default="root",
    help="SSH 用户名 (默认: root)"
)
@click.option(
    "--password",
    help="SSH 密码（密码认证优先于密钥认证）"
)
@click.option(
    "--key-file",
    default="~/.ssh/id_rsa",
    help="SSH 私钥文件路径"
)
@click.option(
    "--skip-verify",
    is_flag=True,
    default=False,
    help="跳过 SSH 互信验证"
)
def configure_cluster_command(
    hosts: Optional[List[str]],
    hostname_map: Optional[str],
    username: str,
    password: str,
    key_file: str,
    skip_verify: bool
) -> None:
    """配置集群主机名和 SSH 互信

    如果未提供 --hosts 和 --hostname-map，命令将尝试从应用配置文件加载集群配置。

    示例：
        $ python cluster.py configure-cluster \\
            --hosts 172.31.65.150,localhost \\
            --hostname-map 172.31.65.150:node-1,localhost:node-2 \\
            --username root \\
            --key-file ~/.ssh/id_rsa

        # 从配置文件加载：
        $ python cluster.py configure-cluster --username root --key-file ~/.ssh/id_rsa
    """
    try:
        # 从输入或配置获取主机列表
        loaded_hosts = _get_cluster_hosts(hosts)
        if not loaded_hosts:
            return

        # 从输入或配置获取主机名映射
        loaded_hostname_map = _get_cluster_hostname_map(
            hostname_map, loaded_hosts)
        if not loaded_hostname_map:
            return

        # 验证所有主机都有主机名映射
        _validate_host_hostname_mapping(loaded_hosts, loaded_hostname_map)

        # 构建 SSH 连接参数
        ssh_kwargs = _build_ssh_params(username, password, key_file)

        # 执行配置工作流
        asyncio.run(
            configure_cluster_workflow(
                hosts=loaded_hosts,
                verify_ssh=not skip_verify,
                host_hostname_map=loaded_hostname_map,
                **ssh_kwargs
            )
        )

    except ValueError as e:
        click.echo(
            click.style(f"配置错误: {str(e)}", fg="red"), err=True
        )


@cli.command(name="show-cluster-config")
def show_cluster_config() -> None:
    """显示当前集群配置"""
    cluster_config = _load_cluster_config()

    if not cluster_config:
        click.echo(
            click.style("未找到集群配置", fg="yellow")
        )
        return

    click.echo(click.style("=== 集群配置 ===", fg="blue", bold=True))
    click.echo()

    hosts = cluster_config.get('hosts', [])
    if hosts:
        click.echo(click.style("节点:", fg="cyan"))
        for host in hosts:
            click.echo(f"  • {host}")

    hostnames = cluster_config.get('hostnames', {})
    if hostnames:
        click.echo(click.style("\n主机名映射:", fg="cyan"))
        for ip, hostname in hostnames.items():
            click.echo(f"  • {ip} → {hostname}")

    click.echo()


@cli.command(name="execute-cmd")
@click.option(
    "--hosts",
    type=ListType,
    help="集群节点 IP 列表，逗号分隔（未提供则从配置加载）"
)
@click.option(
    "--cmd",
    required=True,
    help="要执行的命令 (如: 'df -h', 'systemctl status docker')"
)
@click.option(
    "--username",
    default="root",
    help="SSH 用户名 (默认: root)"
)
@click.option(
    "--password",
    help="SSH 密码（优先于密钥认证）"
)
@click.option(
    "--key-file",
    default="~/.ssh/id_rsa",
    help="SSH 私钥文件路径"
)
def execute_command(
    hosts: Optional[List[str]],
    cmd: str,
    username: str,
    password: str,
    key_file: str
) -> None:
    """在集群节点上执行命令

    如果未提供 --hosts，命令将尝试从应用配置文件加载集群配置。

    示例：
        1. 检查已配置节点的磁盘使用情况：
            $ python cluster.py execute-cmd --cmd "df -h" --username root

        2. 在指定节点上执行：
            $ python cluster.py execute-cmd \\
                --hosts 172.31.65.150,localhost \\
                --cmd "df -h" \\
                --username root \\
                --key-file ~/.ssh/id_rsa

        3. 重启 Docker 服务：
            $ python cluster.py execute-cmd \\
                --cmd "systemctl restart docker" \\
                --username root \\
                --password your-password
    """
    try:
        # 从输入或配置获取主机列表
        loaded_hosts = _get_cluster_hosts(hosts)
        if not loaded_hosts:
            return

        ssh_kwargs = _build_ssh_params(username, password, key_file)

        asyncio.run(
            execute_command_workflow(
                hosts=loaded_hosts,
                command=cmd,
                **ssh_kwargs
            )
        )

    except ValueError as e:
        click.echo(
            click.style(f"配置错误: {str(e)}", fg="red"), err=True
        )


@cli.command(name="disable-firewalld")
@click.option(
    "--hosts",
    type=ListType,
    help="集群节点 IP 列表，逗号分隔（未提供则从配置加载）"
)
@click.option(
    "--username",
    default="root",
    help="SSH 用户名 (默认: root)"
)
@click.option(
    "--password",
    help="SSH 密码"
)
@click.option(
    "--key-file",
    default="~/.ssh/id_rsa",
    help="SSH 私钥文件路径"
)
def disable_firewalld(
    hosts: Optional[List[str]],
    username: str,
    password: str,
    key_file: str
) -> None:
    """禁用并停止集群节点上的防火墙服务

    如果未提供 --hosts，命令将尝试从应用配置文件加载集群配置。

    示例：
        $ python cluster.py disable-firewalld --username root

        # 或在指定节点上：
        $ python cluster.py disable-firewalld \\
            --hosts 172.31.65.150,localhost \\
            --username root
    """
    try:
        # 从输入或配置获取主机列表
        loaded_hosts = _get_cluster_hosts(hosts)
        if not loaded_hosts:
            return

        ssh_kwargs = _build_ssh_params(username, password, key_file)
        cmd = "systemctl stop firewalld && systemctl disable firewalld"

        asyncio.run(
            execute_command_workflow(
                hosts=loaded_hosts,
                command=cmd,
                **ssh_kwargs
            )
        )

    except ValueError as e:
        click.echo(
            click.style(f"配置错误: {str(e)}", fg="red"), err=True
        )


# ============================ 主程序入口 ============================


if __name__ == '__main__':
    """CLI 命令行入口"""
    cli()
