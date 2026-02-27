"""
镜像构建CLI工具

提供镜像构建、管理、查询等功能，支持单个版本、多版本和全量构建。
"""
from __future__ import annotations
from cli.ctr import cli as ctr_cli

from functools import wraps
import sys
import traceback
import shutil
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any, Callable, TypeVar, ParamSpec

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from cli.models import LIST
from builder.image.loader import LazyBuilderLoader, create_builder
from builder.image.base_builder import BuilderOptions, BaseBuilder
from core.config.application import Application
from core.logger import get_logger, setup_cli_logging


logger = get_logger(__name__)
# 初始化Rich控制台
console: Console = Console()

# 泛型类型定义
P = ParamSpec('P')
T = TypeVar('T')


class ImageCLIError(Exception):
    """Image CLI异常"""
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
            # except click.ClickInterrupt:  # type: ignore
            #     # 用户中断，静默退出
            #     console.print("\n[yellow]⚠️  操作被用户中断[/yellow]")
            #     if exit_on_error:
            #         sys.exit(130)
            #     raise
            except click.BadParameter as e:
                # Click 参数错误
                console.print(f"[red]参数错误: {str(e)}[/red]")
                if show_traceback:
                    console.print(f"[dim]{traceback.format_exc()}[/dim]")
                if exit_on_error:
                    sys.exit(2)
                raise
            except click.UsageError as e:
                # Click 使用错误
                console.print(f"[red]使用错误: {str(e)}[/red]")
                if show_traceback:
                    console.print(f"[dim]{traceback.format_exc()}[/dim]")
                if exit_on_error:
                    sys.exit(2)
                raise
            except ImageCLIError as e:
                # 自定义CLI错误
                console.print(f"[red]{str(e)}[/red]")
                if show_traceback:
                    console.print(f"[dim]{traceback.format_exc()}[/dim]")
                if exit_on_error:
                    sys.exit(1)
                raise
            except FileNotFoundError as e:
                # 文件不存在错误
                console.print(f"[red]文件不存在: {str(e)}[/red]")
                if show_traceback:
                    console.print(f"[dim]{traceback.format_exc()}[/dim]")
                if exit_on_error:
                    sys.exit(1)
                raise
            except PermissionError as e:
                # 权限错误
                console.print(f"[red]权限不足: {str(e)}[/red]")
                if show_traceback:
                    console.print(f"[dim]{traceback.format_exc()}[/dim]")
                if exit_on_error:
                    sys.exit(1)
                raise
            except Exception as e:
                # 通用异常处理
                error_msg = str(e)
                logger.error(f"执行失败: {error_msg}\n{traceback.format_exc()}")

                # 根据错误类型显示不同的消息
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


# 专用错误处理装饰器
def cli_command(func: Callable[P, T]) -> Callable[P, T]:
    """CLI命令专用错误处理装饰器

    自动处理常见错误并退出程序
    """
    return handle_errors(exit_on_error=True, show_traceback=False)(func)


# 非退出错误处理装饰器
def safe_execution(func: Callable[P, T]) -> Callable[P, T]:
    """安全执行装饰器

    不退出程序，只记录错误
    """
    return handle_errors(exit_on_error=False, show_traceback=True)(func)


@click.group()
@click.option('--quiet', '-q', is_flag=True, help='静默模式')
@click.option('--debug', is_flag=True, help='启用调试模式（显示所有日志，优先级高于--quiet）')
@click.pass_context
@cli_command
def cli(ctx: click.Context, quiet: bool, debug: bool) -> None:
    """镜像构建工具

    用于构建和管理Docker镜像的命令行工具。\n
    支持单个版本构建、批量构建、应用管理等功能。
    """
    ctx.ensure_object(dict)
    ctx.obj['quiet'] = quiet
    ctx.obj['debug'] = debug

    # 配置日志级别【调整：移除verbose分支，按debug→quiet→默认逻辑配置】
    log_file = f"{Application.ROOT_DIR}/logs/images_cli.log"
    # 日志级别优先级：debug（最高）> quiet > 默认（WARNING）
    if debug:
        log_level = "DEBUG"
        ctx.obj['show_traceback'] = True
    elif quiet:
        log_level = "ERROR"  # 静默模式：仅显示错误日志
        ctx.obj['show_traceback'] = False
    else:
        log_level = "INFO"
        ctx.obj['show_traceback'] = False

    # 核心修改：调用setup_cli_logging时，传入全局rich console实例，实现日志/进度条输出统一
    setup_cli_logging(log_level, log_file,
                      console_output=False, rich_console=console)
    # 同步设置当前logger级别
    logger.setLevel(log_level)


def create_loader() -> LazyBuilderLoader:
    """创建构建器加载器"""
    try:
        return LazyBuilderLoader()
    except Exception as e:
        raise ImageCLIError(f"初始化构建器加载器失败: {e}")


def create_builder_options(
    out: Optional[str] = None,
    export: bool = False,
    push: bool = False,
    timeout: Optional[int] = None,
    parallel: bool = True,
    **kwargs: Any
) -> BuilderOptions:
    """创建构建器选项"""
    return BuilderOptions(
        export=export,
        push=push,
        out=out or "/opt/images",
        timeout=timeout,
        parallel=parallel,
        **kwargs
    )


def format_build_results(app_name: str, results: List[Tuple[str, bool]]) -> None:
    """格式化构建结果输出"""
    successful: List[str] = [
        version for version, success in results if success]
    failed: List[str] = [version for version,
                         success in results if not success]

    # 创建结果表格
    table = Table(title=f"[bold]{app_name}[/bold] 构建结果")
    table.add_column("版本", style="cyan")
    table.add_column("状态", style="magenta")
    table.add_column("结果", style="green")

    for version, success in results:
        status = "成功" if success else "失败"
        result_style = "green" if success else "red"
        table.add_row(version, status,
                      f"[{result_style}]{status}[/{result_style}]")

    console.print(table)

    # 汇总信息
    if successful:
        console.print(
            f"[green]成功构建 {len(successful)} 个版本: {', '.join(successful)}[/green]")
    if failed:
        console.print(
            f"[red]构建失败 {len(failed)} 个版本: {', '.join(failed)}[/red]")


@cli.command()
@click.argument('app', required=True, nargs=1)
@click.option('-v', '--version', required=True, help='版本号')
@click.option('--out', type=click.Path(), help='输出路径，默认为/opt/images')
@click.option('--no-export', is_flag=True, help='导出镜像')
@click.option('--push', is_flag=True, help='推送到镜像仓库')
@click.option('--timeout', type=int, help='构建超时时间（秒）')
@click.option('--config-file', type=click.Path(exists=True), help='自定义配置文件路径')
@click.pass_context
@cli_command
def build(
    ctx: click.Context,
    app: str,
    version: str,
    out: Optional[str],
    no_export: bool,
    push: bool,
    timeout: Optional[int],
    config_file: Optional[str]
) -> None:
    """构建单个应用版本

    构建指定应用的指定版本镜像。

    示例:\n
        image build myapp -v 1.0.0 --no-export\n
        image build myapp -v 1.0.0 --out /tmp/images --push --timeout 600\n
    """
    console.print(f"[blue]开始构建 {app}:{version}[/blue]")

    # 创建构建器选项
    options: BuilderOptions = create_builder_options(
        out=out,
        export=not no_export,
        push=push,
        timeout=timeout
    )

    # 创建构建器
    builder: BaseBuilder = create_builder(app, app, config_file, options)

    success: bool
    _, success = builder.build(version)

    if success:
        console.print(f"[green]构建成功: {app}:{version}[/green]")
    else:
        console.print(f"[red]构建失败: {app}:{version}[/red]")
        raise ImageCLIError(f"构建 {app}:{version} 失败")


@cli.command()
@click.argument('app', required=True, nargs=1)
@click.option('--versions', required=True, type=LIST, help='版本列表，用逗号分隔')
@click.option('--out', type=click.Path(), help='输出路径，默认为/opt/images')
@click.option('--no-export', is_flag=True, help='导出镜像')
@click.option('--push', is_flag=True, help='推送到镜像仓库')
@click.option('--timeout', type=int, help='单个版本构建超时时间（秒）')
@click.option('--parallel', is_flag=True, default=True, help='并行构建')
@click.option('--config-file', type=click.Path(exists=True), help='自定义配置文件路径')
@click.pass_context
@cli_command
def build_multi(
    ctx: click.Context,
    app: str,
    versions: List[str],
    out: Optional[str],
    no_export: bool,
    push: bool,
    timeout: Optional[int],
    parallel: bool,
    config_file: Optional[str]
) -> None:
    """构建多个版本

    批量构建指定应用的多个版本镜像。

    示例:\n
        image build-multi myapp --versions 1.0.0,1.1.0,2.0.0 --no-export --parallel\n
        image build-multi myapp --versions 1.0.0,1.1.0 --out /tmp/images\n
    """
    console.print(f"[blue]开始批量构建 {app}，共 {len(versions)} 个版本[/blue]")

    # 创建构建器选项
    options: BuilderOptions = create_builder_options(
        out=out,
        export=not no_export,
        push=push,
        timeout=timeout,
        parallel=parallel
    )

    # 创建构建器
    builder: BaseBuilder = create_builder(app, app, config_file, options)

    # 执行批量构建
    results: List[Tuple[str, bool]]
    if parallel:
        results = []
        build_results: List[Tuple[str, bool]
                            ] = builder.build_multi(versions)
        for _, (version, success) in enumerate(build_results):
            # progress.update(task, advance=1)
            results.append((version, success))

            status = "成功" if success else "失败"
            # progress.console.print(f"  {status} {version}")
            console.print(f"  {status} {version}")
    else:
        results = builder.build_sequential(versions)

        for version, success in results:
            status = "成功" if success else "失败"
            console.print(f"  {status} {version}")

    # 显示结果
    format_build_results(app, results)

    # 检查是否有失败的构建
    failed_count: int = sum(1 for _, success in results if not success)
    if failed_count > 0:
        raise ImageCLIError(f"有 {failed_count} 个版本构建失败")


@cli.command()
@click.argument('app', required=True, nargs=1)
@click.option('--out', type=click.Path(), help='输出路径，默认为/opt/images')
@click.option('--no-export', is_flag=True, help='导出镜像')
@click.option('--push', is_flag=True, help='推送到镜像仓库')
@click.option('--timeout', type=int, help='单个版本构建超时时间（秒）')
@click.option('--parallel', is_flag=True, default=True, help='并行构建')
@click.option('--config-file', type=click.Path(exists=True), help='自定义配置文件路径')
@click.pass_context
@cli_command
def build_all(
    ctx: click.Context,
    app: str,
    out: Optional[str],
    no_export: bool,
    push: bool,
    timeout: Optional[int],
    parallel: bool,
    config_file: Optional[str]
) -> None:
    """构建应用所有版本

    构建指定应用的所有可用版本镜像。

    示例:\n
        image build-all myapp --no-export\n
        image build-all myapp --out /tmp/images --parallel\n
    """
    console.print(f"[blue]开始构建 {app} 的所有版本[/blue]")

    # 创建构建器选项
    options: BuilderOptions = create_builder_options(
        out=out,
        export=not no_export,
        push=push,
        timeout=timeout,
        parallel=parallel
    )

    # 创建构建器
    builder: BaseBuilder = create_builder(app, app, config_file, options)

    # 获取所有支持的版本
    versions: List[str]
    help_info: str
    versions, help_info = builder.supported_versions()

    if not versions:
        console.print(f"[yellow]应用 {app} 没有找到可用版本[/yellow]")
        return

    console.print(
        f"[cyan]找到 {len(versions)} 个版本: {', '.join(versions)}[/cyan]")

    if help_info:
        console.print(Panel(help_info, title="版本选择提示", border_style="cyan"))

    # 构建所有版本（复用 build_multi 逻辑）
    ctx.invoke(build_multi, **{
        'app': app,
        'versions': versions,
        'out': out,
        'no_export': no_export,
        'push': push,
        'timeout': timeout,
        'parallel': parallel,
        'config_file': config_file
    })


@cli.command()
@click.option('--detailed', '-d', is_flag=True, help='显示详细信息')
@cli_command
def list_apps(detailed: bool) -> None:
    """列出支持的应用

    显示所有可用的镜像构建器及其支持的信息。

    示例:\n
        image list-apps\n
        image list-apps --detailed\n
    """
    loader: LazyBuilderLoader = create_loader()
    builders: Dict[str, Any] = loader.get_all_builders()

    if not builders:
        console.print("[yellow]没有找到可用的构建器[/yellow]")
        return

    if detailed:
        # 详细信息表格
        table = Table(title="可用应用详细信息", show_lines=True)
        table.add_column("应用名称", style="cyan")
        table.add_column("描述", style="magenta")
        table.add_column("版本", style="yellow")
        table.add_column("提示", style="green")

        for name in sorted(builders.keys()):
            try:
                # 创建临时构建器获取信息
                metadata = loader.get_builder_metadata(name)
                temp_builder: BaseBuilder = create_builder(name, name)
                versions: List[str]
                help_info: str
                versions, help_info = temp_builder.supported_versions()

                description: str = metadata.description or "无描述" if metadata else "无描述"
                version_str: str = ', '.join(versions) if versions else "无版本"

                # table.add_row(
                #     name,
                #     description[:50] +
                #     "..." if len(description) > 50 else description,
                #     version_str[:30] +
                #     "..." if len(version_str) > 30 else version_str,
                #     help_info[:40] + "..." if help_info and len(
                #         help_info) > 40 else (help_info or "")
                # )
                table.add_row(
                    name,
                    description,
                    version_str,
                    help_info
                )
            except Exception as e:
                table.add_row(name, f"加载失败: {str(e)}", "N/A", "N/A")

        console.print(table)
    else:
        # 简单列表
        for name in sorted(builders.keys()):
            try:
                metadata = loader.get_builder_metadata(name)
                description: str = metadata.description or "无描述" if metadata else "无描述"
                console.print(f"[cyan]• {name}[/cyan]: {description}")
            except Exception as e:
                console.print(
                    f"[red]• {name}[/red]: [dim]加载失败: {str(e)}[/dim]")


@cli.command()
@click.argument('app_name', required=False)
@cli_command
def info(app_name: Optional[str]) -> None:
    """显示应用详细信息

    显示指定应用或所有应用的详细构建信息。

    示例:\n
        image info myapp\n
        image info\n
    """
    loader: LazyBuilderLoader = create_loader()

    if app_name:
        # 显示特定应用信息
        try:
            metadata = loader.get_builder_metadata(app_name)
            if not metadata:
                console.print(f"[red]应用 '{app_name}' 不存在[/red]")
                return

            # 创建临时构建器获取版本信息
            temp_builder: BaseBuilder = create_builder(app_name, app_name)
            versions: List[str]
            help_info: str
            versions, help_info = temp_builder.supported_versions()

            # 构建信息面板
            info_text: str = f"""
[bold cyan]应用名称:[/bold cyan] {app_name}
[bold cyan]描述:[/bold cyan] {metadata.description or '无描述'}
[bold cyan]版本:[/bold cyan] {', '.join(versions) if versions else '无版本'}
[bold cyan]模块:[/bold cyan] {metadata.class_type.__module__}
[bold cyan]类名:[/bold cyan] {metadata.class_type.__name__}
"""

            if metadata.version:
                info_text += f"[bold cyan]构建器版本:[/bold cyan] {metadata.version}\n"

            if metadata.author:
                info_text += f"[bold cyan]作者:[/bold cyan] {metadata.author}\n"

            if metadata.supported_features:
                info_text += f"[bold cyan]支持特性:[/bold cyan] {', '.join(metadata.supported_features)}\n"

            if help_info:
                info_text += f"\n[bold yellow]版本选择提示:[/bold yellow]\n{help_info}"

            console.print(
                Panel(info_text.strip(), title=f"{app_name} 详细信息", border_style="cyan"))

        except Exception as e:
            console.print(f"[red]获取应用 '{app_name}' 信息失败: {str(e)}[/red]")
    else:
        # 显示所有应用简要信息
        ctx: click.Context = click.get_current_context()
        ctx.invoke(list_apps, detailed=True)


@cli.command()
@click.argument('apps', required=False, nargs=-1)
@cli_command
def clean(apps: Optional[list[str]]) -> None:
    """清理构建产物

    清理指定应用或所有应用的构建产物（镜像文件、临时文件等）。

    示例:\n
        image clean myapp\n
        image clean\n
    """
    if apps:
        # 清理特定应用
        for app in apps:
            image_dir: Path = Path("/opt/images")
            if image_dir.exists():
                # 删除该应用的所有镜像文件
                deleted_count: int = 0
                for file_path in image_dir.glob(f"{app}-*.image.tar"):
                    try:
                        file_path.unlink()
                        deleted_count += 1
                        console.print(f"[green]删除: {file_path.name}[/green]")
                    except Exception as e:
                        console.print(
                            f"[red]删除失败 {file_path.name}: {str(e)}[/red]")

                if deleted_count > 0:
                    console.print(
                        f"[green]清理了 {deleted_count} 个 {app} 的镜像文件[/green]")
                else:
                    console.print(f"[yellow]没有找到 {app} 的镜像文件[/yellow]")
            else:
                console.print("[yellow]镜像目录不存在[/yellow]")
    else:
        # 清理所有应用
        if click.confirm("确定要清理所有应用的构建产物吗？"):
            image_dir: Path = Path("/opt/images")
            if image_dir.exists():
                try:
                    shutil.rmtree(image_dir)
                    image_dir.mkdir(parents=True, exist_ok=True)
                    console.print("[green]清理了所有构建产物[/green]")
                except Exception as e:
                    console.print(f"[red]清理失败: {str(e)}[/red]")
            else:
                console.print("[yellow]镜像目录不存在[/yellow]")


# 导入并集成ctr子命令
cli.add_command(ctr_cli, "ctr")


if __name__ == '__main__':
    cli()
