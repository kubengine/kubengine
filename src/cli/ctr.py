"""
容器镜像仓库操作CLI工具

提供容器镜像仓库操作的命令行工具，包括拉取、推送、镜像仓库代理管理等功能。
"""

from __future__ import annotations

from functools import wraps
import sys
import traceback
from pathlib import Path
from typing import Any, Optional, Callable, TypeVar, ParamSpec

import click
from rich.console import Console

from core.command import execute_command
from core.containerd.certs import ContainerdCertsConfig
from core.logger import get_logger
from core.config.application import Application

# 初始化日志
logger = get_logger(__name__)
console: Console = Console()

# 泛型类型定义
P = ParamSpec('P')
T = TypeVar('T')


class CtrCLIError(Exception):
    """Ctr CLI异常"""
    pass


def handle_errors(
    exit_on_error: bool = True,
    show_traceback: bool = False
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """错误处理装饰器

    Args:
        exit_on_error: 发生错误时是否退出程序
        show_traceback: 是否显示详细错误堆栈

    Returns:
        装饰器函数
    """
    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            try:
                return func(*args, **kwargs)
            except click.BadParameter as e:
                console.print(f"[red]参数错误: {str(e)}[/red]")
                if show_traceback:
                    console.print(f"[dim]{traceback.format_exc()}[/dim]")
                if exit_on_error:
                    sys.exit(2)
                raise
            except click.UsageError as e:
                console.print(f"[red]使用错误: {str(e)}[/red]")
                if show_traceback:
                    console.print(f"[dim]{traceback.format_exc()}[/dim]")
                if exit_on_error:
                    sys.exit(2)
                raise
            except CtrCLIError as e:
                console.print(f"[red]{str(e)}[/red]")
                if show_traceback:
                    console.print(f"[dim]{traceback.format_exc()}[/dim]")
                if exit_on_error:
                    sys.exit(1)
                raise
            except FileNotFoundError as e:
                console.print(f"[red]文件不存在: {str(e)}[/red]")
                if show_traceback:
                    console.print(f"[dim]{traceback.format_exc()}[/dim]")
                if exit_on_error:
                    sys.exit(1)
                raise
            except PermissionError as e:
                console.print(f"[red]权限不足: {str(e)}[/red]")
                if show_traceback:
                    console.print(f"[dim]{traceback.format_exc()}[/dim]")
                if exit_on_error:
                    sys.exit(1)
                raise
            except Exception as e:
                error_msg = str(e)
                logger.error(f"执行失败: {error_msg}\n{traceback.format_exc()}")

                if isinstance(e, (OSError, IOError)):
                    console.print(f"[red]系统错误: {error_msg}[/red]")
                elif isinstance(e, ValueError):
                    console.print(f"[red]值错误: {error_msg}[/red]")
                elif isinstance(e, KeyError):
                    console.print(f"[red]配置错误: 缺少必要的配置项 {error_msg}[/red]")
                else:
                    console.print(f"[red]未知错误: {error_msg}[/red]")

                if show_traceback:
                    console.print(
                        f"[dim]详细错误信息:\n{traceback.format_exc()}[/dim]")

                if exit_on_error:
                    sys.exit(1)
                raise

        return wrapper
    return decorator


def cli_command(func: Callable[P, T]) -> Callable[P, T]:
    """CLI命令专用错误处理装饰器

    自动处理常见错误并退出程序
    """
    return handle_errors(exit_on_error=True, show_traceback=False)(func)


@click.group()
@click.pass_context
@cli_command
def cli(ctx: click.Context) -> None:
    """容器镜像仓库操作子命令组（ctr）

    提供容器镜像仓库操作的功能，包括拉取、推送、镜像仓库代理管理等。
    """
    ctx.ensure_object(dict)


@cli.command()
@click.option('-i', '--image', required=True, help='待拉取的镜像完整名称（含仓库/标签），例：harbor.example.com/myapp:1.0.0')
@click.option('-u', '--username', help='私有仓库用户名，公共仓库无需填写')
@click.option('-p', '--password', help='私有仓库密码/令牌，公共仓库无需填写')
@click.option('--timeout', type=int, default=300, help='拉取超时时间（秒），默认300秒')
@click.pass_context
@cli_command
def pull(
    ctx: click.Context,
    image: str,
    username: Optional[str],
    password: Optional[str],
    timeout: int
) -> None:
    """从容器仓库拉取镜像（pull）

    支持公共/私有容器仓库（Harbor/Docker Hub/Registry等），
    私有仓库需指定用户名和密码。

    示例：
        # 拉取公共仓库镜像
        kubengine image ctr pull -i nginx:1.25.3
        # 拉取私有Harbor仓库镜像
        kubengine image ctr pull -i harbor.example.com/myapp:1.0.0 -u admin -p Harbor12345
        # 拉取并设置超时时间
        kubengine image ctr pull -i redis:7.2 --timeout 600
    """
    console.print(f"[blue]📥 开始从仓库拉取镜像: {image}[/blue]")
    logger.info(
        f"执行镜像拉取操作 | 镜像: {image} | 超时: {timeout}秒 | 私有仓库: {True if username else False}")

    try:
        # 构建拉取命令
        cmd = "ctr i pull --hosts-dir /etc/containerd/certs.d/"
        if username and password:
            cmd += f" -u {username}@{password}"
        cmd += f" {image}"

        execute_command(cmd).raise_if_failed()

        console.print(f"[green]🎉 镜像拉取成功: {image}[/green]")
        logger.info(f"镜像拉取成功 | 镜像: {image}")

    except Exception as e:
        logger.error(f"镜像拉取失败 | 镜像: {image} | 错误: {str(e)}", exc_info=True)
        raise CtrCLIError(f"拉取镜像 {image} 失败: {str(e)}")


@cli.command()
@click.option('-i', '--image', required=True, help='待推送的镜像完整名称（含仓库/标签），例：harbor.example.com/myapp:1.0.0')
@click.option('-u', '--username', help='私有仓库用户名，公共仓库无需填写')
@click.option('-p', '--password', help='私有仓库密码/令牌，公共仓库无需填写')
@click.option('--timeout', type=int, default=300, help='推送超时时间（秒），默认300秒')
@click.option('--skip-exists', is_flag=True, help='若仓库已存在该镜像，跳过推送（避免覆盖）')
@click.pass_context
@cli_command
def push(
    ctx: click.Context,
    image: str,
    username: Optional[str],
    password: Optional[str],
    timeout: int,
    skip_exists: bool
) -> None:
    """将本地镜像推送到容器仓库（push）

    支持公共/私有容器仓库（Harbor/Docker Hub/Registry等），
    私有仓库需指定用户名和密码，支持跳过已存在的镜像。

    示例：
        # 推送公共仓库镜像
        kubengine image ctr push -i myapp:1.0.0
        # 推送私有Harbor仓库镜像
        kubengine image ctr push -i harbor.example.com/myapp:1.0.0 -u admin -p Harbor12345
        # 推送并跳过已存在镜像
        kubengine image ctr push -i redis:7.2 -u admin -p 123456 --skip-exists
    """
    console.print(f"[blue]📤 开始推送本地镜像到仓库: {image}[/blue]")
    logger.info(
        f"执行镜像推送操作 | 镜像: {image} | 超时: {timeout}秒 | 跳过已存在: {skip_exists}")

    try:
        # 构建推送命令
        cmd = "ctr i push --hosts-dir /etc/containerd/certs.d/"
        if username and password:
            cmd += f" -u {username}:{password}"
        cmd += f" {image}"

        execute_command(cmd).raise_if_failed()

        console.print(f"[green]🎉 镜像推送成功: {image}[/green]")
        logger.info(f"镜像推送成功 | 镜像: {image}")

    except Exception as e:
        logger.error(f"镜像推送失败 | 镜像: {image} | 错误: {str(e)}", exc_info=True)
        raise CtrCLIError(f"推送镜像 {image} 失败: {str(e)}")


@cli.command()
@click.argument('registrys', required=True, nargs=-1)
@click.option('--yes', '-y', is_flag=True, help='跳过确认')
@click.option(
    '--no-restart', is_flag=True,
    help='仅写入配置，不重启 containerd（默认写入后自动重启）'
)
@click.option(
    '--no-sync', is_flag=True,
    help='不同步到集群其他节点（默认自动同步到所有 worker/master 节点）'
)
@click.option(
    '--ssh-user', default='root', show_default=True,
    help='远程节点 SSH 用户名'
)
@click.option(
    '--ssh-password', help='远程节点 SSH 密码（与 --ssh-key 二选一）'
)
@click.option(
    '--ssh-key', default='~/.ssh/id_rsa', show_default=True,
    help='远程节点 SSH 私钥路径（默认使用互信密钥）'
)
@cli_command
def add_proxy(
    registrys: list[str],
    yes: bool,
    no_restart: bool,
    no_sync: bool,
    ssh_user: str,
    ssh_password: Optional[str],
    ssh_key: str
) -> None:
    """添加镜像仓库代理（写入 containerd hosts.toml）

    将指定上游仓库的镜像拉取请求透明转发到本地 Harbor。
    配置写入 /etc/containerd/certs.d/<registry>/hosts.toml，并重启 containerd 生效。
    默认同步到集群所有节点（worker + master）并重启各自 containerd。

    若镜像仓库代理已存在，则会覆盖当前配置。

    示例:
        kubengine image ctr add-proxy docker.io

        kubengine image ctr add-proxy quay.io registry.k8s.io

        kubengine image ctr add-proxy docker.io -y --no-restart

        # 仅本机，不同步集群
        kubengine image ctr add-proxy docker.io --no-sync
    """
    from pathlib import Path
    from rich.table import Table

    certs_d_path = Path("/etc/containerd/certs.d")
    ca_crt = Application.TLS_CONFIG.CA_CRT

    # 预览配置
    table = Table(title="[bold]将写入的仓库代理配置[/bold]", show_lines=True)
    table.add_column("目标仓库", style="cyan")
    table.add_column("代理地址", style="magenta")
    table.add_column("配置文件路径", style="dim")

    for registry in registrys:
        proxy_url = f"https://{Application.DOMAIN}/v2/{registry}"
        hosts_toml = certs_d_path / registry / "hosts.toml"
        table.add_row(registry, proxy_url, str(hosts_toml))

    console.print(table)

    if not (yes or click.confirm("确认写入以上配置并重启 containerd？", default=True)):
        console.print("[yellow]已取消[/yellow]")
        return

    # 写入 hosts.toml
    written: list[str] = []
    for registry in registrys:
        proxy_url = f"https://{Application.DOMAIN}/v2/{registry}"
        registry_dir = certs_d_path / registry
        registry_dir.mkdir(parents=True, exist_ok=True)
        hosts_toml_path = registry_dir / "hosts.toml"

        # 标准 containerd hosts.toml 格式（嵌套 [host."..."] 段）
        content = (
            f'server = "{proxy_url}"\n'
            f'\n'
            f'[host."{proxy_url}"]\n'
            f'  capabilities = ["pull", "push", "resolve"]\n'
            f'  override_path = true\n'
        )
        # CA 证书存在则附加，启用 TLS 校验
        if Path(ca_crt).exists():
            content += f'  ca = "{ca_crt}"\n'
        else:
            content += f'  skip_verify = true\n'

        hosts_toml_path.write_text(content, encoding="utf-8")
        written.append(registry)
        logger.info(f"写入 hosts.toml: {hosts_toml_path}")

    console.print(
        f"[green]✅ 已写入 {len(written)} 个仓库代理配置: {', '.join(written)}[/green]"
    )

    # 在 Harbor 中创建对应的公开项目（与 registry 同名）
    try:
        from core.http_api_client.harbor_client import HarborClient
        harbor = HarborClient()
        console.print(
            f"[blue]📋 在 Harbor 中创建对应公开项目: {', '.join(written)}[/blue]")
        for project in written:
            if harbor.create_project(project, public=True):
                console.print(f"[green]  ✅ Harbor 项目: {project}[/green]")
            else:
                console.print(
                    f"[yellow]  ⚠ Harbor 项目 '{project}' 创建失败（可能已存在或 Harbor 未就绪）[/yellow]")
    except Exception as e:
        console.print(
            f"[yellow]⚠ Harbor 项目创建跳过: {e}[/yellow]")

    # 重启本机 containerd 使配置生效
    if not no_restart:
        console.print("[blue]🔄 重启本机 containerd 使配置生效...[/blue]")
        try:
            result = execute_command("systemctl restart containerd")
            if result.is_success():
                console.print("[green]✅ 本机 containerd 重启成功[/green]")
            else:
                console.print(
                    f"[yellow]⚠ 本机 containerd 重启失败，请手动执行: systemctl restart containerd[/yellow]"
                )
                logger.warning(f"containerd 重启失败: {result.get_error_lines()}")
        except Exception as e:
            console.print(
                f"[yellow]⚠ 本机 containerd 重启异常，请手动执行: systemctl restart containerd ({e})[/yellow]"
            )
    else:
        console.print("[yellow]⚠ 已跳过本机 containerd 重启[/yellow]")

    # 同步到集群其他节点
    if no_sync:
        console.print("[yellow]⚠ 已跳过集群节点同步（--no-sync）[/yellow]")
        return

    # 收集集群节点（排除本机）
    from core.config.application import Application as App
    additional_masters = getattr(App.K8S_CONFIG, 'ADDITIONAL_MASTER_IPS', []) or []
    workers = getattr(App.K8S_CONFIG, 'WORKER_IPS', []) or []
    remote_nodes = list(additional_masters) + list(workers)

    if not remote_nodes:
        console.print("[yellow]⚠ 未配置其他集群节点（WORKER_IPS/ADDITIONAL_MASTER_IPS），跳过同步[/yellow]")
        return

    console.print(
        f"[blue]📡 同步代理配置到 {len(remote_nodes)} 个集群节点: {', '.join(remote_nodes)}[/blue]")

    # 构建 SSH 参数
    ssh_kwargs: dict = {"username": ssh_user}
    if ssh_password:
        ssh_kwargs["password"] = ssh_password
    else:
        import os
        ssh_kwargs["client_keys"] = [os.path.expanduser(ssh_key)]

    # 同步文件 + 远程重启
    import asyncio
    from core.ssh import AsyncSSHClient

    async def _sync_to_nodes() -> None:
        ssh_client = AsyncSSHClient()

        # 1. 上传 hosts.toml 和 CA 证书到每个节点
        upload_tasks = []
        ca_exists = Path(ca_crt).exists()
        for node in remote_nodes:
            for registry in written:
                local_hosts = str(certs_d_path / registry / "hosts.toml")
                remote_hosts = f"/etc/containerd/certs.d/{registry}/hosts.toml"
                # mkdir -p + 上传一条龙命令前缀
                upload_tasks.append((node, registry, local_hosts, remote_hosts))

        for node, registry, local_hosts, remote_hosts in upload_tasks:
            # 先确保远程目录存在
            mkdir_cmd = f"mkdir -p /etc/containerd/certs.d/{registry}"
            await ssh_client.execute_command(node, mkdir_cmd, **ssh_kwargs)
            # 上传 hosts.toml
            res = await ssh_client.upload_file(node, local_hosts, remote_hosts, **ssh_kwargs)
            if res.get("error"):
                console.print(
                    f"[yellow]  ⚠ {node} 上传 {registry}/hosts.toml 失败: {res['error']}[/yellow]")
            else:
                console.print(
                    f"[green]  ✅ {node} <- {registry}/hosts.toml[/green]")

        # 同步 CA 证书（hosts.toml 引用了 ca 路径）
        if ca_exists:
            for node in remote_nodes:
                # 确保远程 ca 目录存在
                remote_ca_dir = str(Path(ca_crt).parent)
                await ssh_client.execute_command(
                    node, f"mkdir -p {remote_ca_dir}", **ssh_kwargs)
                res = await ssh_client.upload_file(
                    node, ca_crt, ca_crt, **ssh_kwargs)
                if res.get("error"):
                    console.print(
                        f"[yellow]  ⚠ {node} 上传 CA 证书失败: {res['error']}[/yellow]")

        # 2. 远程重启 containerd（除非 --no-restart）
        if no_restart:
            console.print("[yellow]⚠ 已跳过远程节点 containerd 重启[/yellow]")
            return

        console.print(f"[blue]🔄 重启 {len(remote_nodes)} 个节点的 containerd...[/blue]")
        restart_cmds = [
            (node, "systemctl restart containerd")
            for node in remote_nodes
        ]
        results = await ssh_client.execute_multiple_commands(restart_cmds, **ssh_kwargs)
        for r in results:
            node = r.get("host", "?")
            if r.get("error"):
                console.print(
                    f"[yellow]  ⚠ {node} containerd 重启失败: {r['error']}[/yellow]")
            else:
                console.print(f"[green]  ✅ {node} containerd 重启成功[/green]")

    try:
        asyncio.run(_sync_to_nodes())
        console.print("[green bold]✅ 集群同步完成[/green bold]")
    except Exception as e:
        console.print(f"[yellow]⚠ 集群同步异常: {e}[/yellow]")
        logger.warning(f"集群同步异常: {e}", exc_info=True)


@cli.command()
@cli_command
def list_proxy() -> None:
    """查看当前镜像仓库代理

    示例:
        kubengine image ctr list-proxy
    """
    from rich.table import Table

    certs_config = ContainerdCertsConfig()
    all_configs = certs_config.load_hosts_configs()

    table = Table(title="[bold]当前仓库代理配置[/bold]", show_lines=True)
    table.add_column("目标仓库", style="cyan")
    table.add_column("代理仓库地址", style="magenta")
    table.add_column("代理功能")
    table.add_column("override_path")

    for key, value in all_configs.items():
        host: dict[str, dict[str, Any]] = value["host"]
        for hkey, hvalue in host.items():
            table.add_row(
                key,
                hkey,
                ",".join(hvalue.get("capabilities", [])),
                str(hvalue.get("override_path", False))
            )

    console.print(table)


if __name__ == '__main__':
    cli()
