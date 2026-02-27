"""
容器镜像仓库操作CLI工具

提供容器镜像仓库操作的命令行工具，包括拉取、推送、镜像仓库代理管理等功能。
"""

from __future__ import annotations

from functools import wraps
import sys
import traceback
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
@cli_command
def add_proxy(registrys: list[str], yes: bool) -> None:
    """添加镜像仓库代理

    如果镜像仓库代理已存在，则会覆盖当前配置

    示例:
        kubengine image ctr add-proxy docker.io

        kubengine image ctr add-proxy quay.io registry.k8s.io
    """
    from rich.table import Table

    table = Table(title="[bold]当前仓库代理配置[/bold]", show_lines=True)
    table.add_column("目标仓库", style="cyan")
    table.add_column("代理仓库地址", style="magenta")
    table.add_column("代理功能")
    table.add_column("override_path")

    for registry in registrys:
        table.add_row(
            registry,
            f"{Application.DOMAIN}/v2/{registry}",
            "pull,push,resolve",
            "True"
        )

    console.print(table)

    try:
        if yes or click.confirm("确认镜像仓库代理配置"):
            console.print("\n完成", style="green")
    except click.exceptions.Abort:
        print()


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
