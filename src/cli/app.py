"""
KubeEngine 统一命令行工具
提供完整的 KubeEngine 平台管理功能，包括：
- 应用管理：启动、配置、数据初始化
- 集群管理：主机名配置、SSH 互信、命令执行
- 镜像构建：单个/批量构建、镜像管理
- K8s 部署：自动化集群部署、组件安装

使用示例：
    # 应用管理
    kubengine app run --host 0.0.0.0 --port 8080
    kubengine app set-password
    kubengine app init-data

    # 集群管理
    kubengine cluster configure-cluster --hosts 172.31.65.150,localhost \
        --hostname-map 172.31.65.150:node-1,localhost:node-2

    # 镜像构建
    kubengine image build redis -v 7.2
    kubengine image list-apps

    # K8s 部署
    请使用 kubengine_k8s 命令，例如：kubengine_k8s deploy --deploy-src /root/offline-deploy
"""

import os
import uuid
import click
from datetime import datetime

from typing import Any
from core.config import Application, ConfigDict
from core.logger import get_logger, setup_cli_logging
from core.orm.app import App, AppSchema, create_application
from core.orm.app_field_config import (
    AppFieldConfigSchema,
    ConfigTypeEnum,
)
from core.orm.engine import Base, engine, get_db
from web.utils.auth import get_password_hash
# 导入子命令
from cli.cluster import cli as cluster_cli
from cli.image import cli as image_cli

# 初始化日志
setup_cli_logging(
    level="INFO",
    log_file=f"{Application.ROOT_DIR}/logs/app_cli.log",
    console_output=True  # 禁用控制台输出
)
logger = get_logger(__name__)


# ============================ 主命令行入口 ============================


@click.group()
def cli() -> None:
    """
    KubeEngine 统一命令行工具

    提供完整的 KubeEngine 平台管理功能。

    核心功能模块：\n
        1. 应用管理：app run, set-password, init-data\n
        2. 集群管理：cluster configure-cluster, execute-cmd\n
        3. 镜像构建：image build, image list-apps\n
    """
    pass


# 添加子命令
cli.add_command(cluster_cli, "cluster")
cli.add_command(image_cli, "image")


# ============================ 应用管理子命令 ============================
@click.group()
def app() -> None:
    """
    应用管理命令

    提供应用服务器的启动、配置和数据初始化功能。
    """
    pass


cli.add_command(app, "app")

# ============================ 应用管理命令 ============================


@app.command()
@click.option(
    "--host",
    default="0.0.0.0",
    show_default=True,
    help="监听的主机地址",
)
@click.option(
    "--port",
    default=8080,
    show_default=True,
    type=int,
    help="监听的端口号",
)
@click.option(
    "--workers",
    default=1,
    show_default=True,
    type=int,
    help="工作进程数（reload=True时强制为1）",
)
@click.option(
    "--reload",
    default=False,
    show_default=True,
    type=bool,
    help="是否启用热重载（仅开发环境使用）",
)
def run(host: str, port: int, workers: int, reload: bool) -> None:
    """
    启动应用服务器

    使用 Uvicorn 启动 FastAPI 应用服务器。

    Args:
        host: 监听的主机地址
        port: 监听的端口号
        workers: 工作进程数
        reload: 是否启用热重载（仅开发环境）
    """
    import uvicorn
    import signal
    import sys
    import asyncio

    # 热重载模式下强制workers=1（Uvicorn不支持reload+多workers）
    if reload:
        workers = 1
        logger.warning("热重载模式已启用，强制设置workers=1")

    logger.info(f"启动应用服务器: {host}:{port}，工作进程数: {workers}")

    # 定义优雅退出的处理函数
    def handle_shutdown(signum: Any, frame: Any):
        logger.info("接收到停止信号，正在优雅关闭服务器...")
        # 停止Uvicorn的事件循环
        loop = asyncio.get_event_loop()
        loop.stop()
        sys.exit(0)

    # 注册信号处理（处理Ctrl+C和kill命令）
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    try:
        uvicorn.run(
            "web.main:app",
            host=host,
            port=port,
            reload=reload,  # 由参数控制，默认关闭
            workers=workers,
            log_level="info",
        )
    except KeyboardInterrupt:
        logger.info("服务器已手动停止")
    except Exception as e:
        logger.error(f"启动服务器失败: {str(e)}", exc_info=True)
        sys.exit(1)


@app.command()
@click.option(
    "-p",
    "--password",
    prompt="请输入新密码",
    hide_input=True,
    confirmation_prompt=True,
    help="admin 账号密码",
)
def set_password(password: str) -> None:
    """
    设置管理员密码

    设置管理员账号的密码，并在首次设置或用户确认时生成 AK/SK 密钥对。
    密码修改后会保存到配置文件中。

    Args:
        password: 新密码
    """
    # 获取配置实例
    config = ConfigDict.get_instance()

    # 更新密码哈希
    config.auth.users.admin.password_hash = get_password_hash(password)
    logger.info("管理员密码已更新")

    # 判断是否需要生成 AK/SK
    gen_ak_flag = False
    if config.auth.users.admin.ak:
        # 如果已有 AK，询问是否重新生成
        gen_ak_flag = click.confirm("是否需要重新生成 AK/SK?")
    else:
        # 如果 AK 为空，说明是第一次设置密码，需要生成 AK 和 SK
        gen_ak_flag = True

    new_ak = ""
    new_sk = ""

    if gen_ak_flag:
        # 生成新的 AK/SK
        new_ak = f"AK{uuid.uuid4().hex.upper()[:8]}"
        new_sk = f"SK{uuid.uuid4().hex.upper()[:16]}"
        sk_hash = get_password_hash(new_sk)

        config.auth.users.admin.ak = new_ak
        config.auth.users.admin.sk_hash = sk_hash
        logger.info("AK/SK 密钥对已生成")

    # 保存配置到文件
    config_path = os.path.join(
        Application.ROOT_DIR, "config", "application.yaml")
    config.save_to_file(config_path)
    logger.info(f"配置已保存到: {config_path}")

    # 输出成功消息
    _print_separator()
    click.echo(click.style("新密码设置成功！", fg="green", bold=True))
    _print_separator()
    click.echo()

    # 如果生成了新的 AK/SK，输出密钥信息
    if gen_ak_flag:
        _print_separator()
        click.echo(click.style("AK/SK 信息生成成功！", fg="green", bold=True))
        click.echo(click.style(f"AK: {new_ak}", fg="green"))
        click.echo(click.style(f"SK: {new_sk}", fg="green", bold=True))
        click.echo(click.style("重要提示：", fg="yellow", bold=True))
        click.echo(
            click.style(
                "1. SK 仅显示一次，已无法找回，请立即保存！",
                fg="yellow",
            )
        )
        click.echo(
            click.style(
                "2. 旧 AK/SK 已失效，请更新所有依赖服务的配置！",
                fg="yellow",
            )
        )
        _print_separator()
        click.echo()


@app.command()
@click.option(
    "--force",
    is_flag=True,
    help="强制覆盖已存在的应用数据",
)
def init_data(force: bool) -> None:
    """
    初始化默认应用数据到数据库

    使用硬编码的默认配置（如 Redis 应用）初始化数据库。
    数据会插入到 app 和 app_field_config 表中。

    Args:
        force: 是否强制覆盖已存在的应用
    """
    # 创建数据库表
    click.echo("检查数据库表...")
    Base.metadata.create_all(bind=engine)
    click.echo(click.style("数据库表已就绪", fg="green"))

    # 默认应用配置（硬编码）
    default_apps = _get_default_apps()

    app_count = 0
    field_config_count = 0
    skipped_count = 0

    for app_schema in default_apps:
        app_name = app_schema.name

        try:
            with get_db() as db:
                # 检查应用是否已存在
                existing_app = (
                    db.query(App).filter(App.name == app_name).first()
                )

                if existing_app:
                    if force:
                        # 删除旧应用（级联删除字段配置）
                        db.delete(existing_app)
                        db.commit()
                        click.echo(
                            click.style(f"覆盖应用: {app_name}", fg="yellow")
                        )
                    else:
                        click.echo(f"跳过已存在的应用: {app_name}")
                        skipped_count += 1
                        continue

                # 创建新应用
                created_app = create_application(app_schema)
                app_count += 1
                click.echo(click.style(f"已创建应用: {app_name}", fg="green"))

                # 统计字段配置数量
                if created_app.app_field_configs:
                    field_config_count += len(created_app.app_field_configs)
                    click.echo(
                        f"  包含 {len(created_app.app_field_configs)} 个字段配置"
                    )

        except Exception as e:
            logger.error(f"初始化应用 {app_name} 失败: {e}")
            click.echo(
                click.style(f"初始化应用 {app_name} 失败: {e}", fg="red")
            )

    # 输出总结
    _print_separator()
    if app_count > 0:
        click.echo(click.style("数据初始化成功！", fg="green", bold=True))
        click.echo(f"新增应用数量: {app_count}")
        click.echo(f"字段配置数量: {field_config_count}")
    else:
        click.echo(click.style("没有新增应用", fg="yellow"))
    if skipped_count > 0:
        click.echo(f"跳过已存在的应用: {skipped_count} (使用 --force 强制覆盖)")
    _print_separator()

    logger.info(
        f"数据初始化完成: {app_count} 个应用, {field_config_count} 个字段配置"
    )


def _get_default_apps() -> list[AppSchema]:
    """
    获取硬编码的默认应用配置

    Returns:
        应用 Schema 列表
    """
    # Redis 应用配置
    redis_app = AppSchema(
        name="redis",
        category=["数据库"],
        description="高性能高可用的内存键值数据库",
        helm_chart="redis",
        create_time=datetime.now(),
        app_field_configs=[
            # 架构选择
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="architecture",
                label="部署模式",
                extra="默认为单机模式",
                order=0,
                form_item_props={"required": True},
                type="select",
                initial_value="standalone",
                rules=[],
                field_props={
                    "allowClear": False,
                    "options": [
                        {"label": "单机模式", "value": "standalone"},
                        {"label": "主从复制模式", "value": "replication"}
                    ]
                },
                helm_props={
                    "keys": ["architecture"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 哨兵模式
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="sentinel",
                label="哨兵模式",
                extra="是否开启redis哨兵模式（哨兵模式仅支持部署模式为主从复制模式时使用）",
                order=1,
                form_item_props={"required": True},
                type="switch",
                initial_value=None,
                rules=[],
                field_props={"options": []},
                helm_props={
                    "keys": ["sentinel.enabled"],
                    "type": "boolean",
                    "unit": "",
                },
            ),
            # CPU 配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="cpu",
                label="cpu",
                extra="每个节点的核数，一般作为缓存，单核就足够了，在数据量较大，数据写入速度很快的情况下，可以视情况使用双核",
                order=2,
                form_item_props={"required": True},
                type="radio",
                initial_value="1",
                rules=[],
                field_props={
                    "options": [
                        {"label": "1", "value": "1"},
                        {"label": "2", "value": "2"}
                    ]
                },
                helm_props={
                    "keys": [
                        "master.resources.requests.cpu",
                        "sentinel.resources.requests.cpu",
                        "replica.resources.requests.cpu",
                    ],
                    "type": "number",
                    "unit": "",
                },
            ),
            # 内存配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="memory",
                label="内存",
                extra="内存大小(单位 Gi)",
                order=3,
                form_item_props={"required": True},
                type="radio",
                initial_value="1",
                rules=[],
                field_props={
                    "options": [
                        {"label": "1", "value": "1"},
                        {"label": "2", "value": "2"},
                        {"label": "4", "value": "4"},
                        {"label": "8", "value": "8"}
                    ]
                },
                helm_props={
                    "keys": [
                        "replica.resources.requests.memory",
                        "master.resources.requests.memory",
                        "sentinel.resources.requests.memory",
                    ],
                    "type": "number",
                    "unit": "Gi",
                },
            ),
            # 磁盘配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="disk",
                label="硬盘大小",
                extra="8Gi - 24Gi, 节点磁盘的大小，建议最少设置为节点内存的三倍",
                order=4,
                form_item_props={"required": True},
                type="number",
                initial_value=8,
                rules=[
                    {"type": "number", "message": "仅允许设置8 - 24", "min": 8, "max": 24}
                ],
                field_props={"options": []},
                helm_props={
                    "keys": ["replica.persistence.size", "master.persistence.size"],
                    "type": "number",
                    "unit": "Gi",
                },
            ),
            # Service 类型
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="service",
                label="Service服务",
                extra="请选择 K8s Service 类型（ClusterIP 集群内用、LoadBalancer 公网负载均衡）",
                order=5,
                form_item_props={"required": True},
                type="radio",
                initial_value="ClusterIP",
                rules=[],
                field_props={
                    "options": [
                        {"label": "ClusterIP", "value": "ClusterIP"},
                        {"label": "LoadBalancer", "value": "LoadBalancer"}
                    ]
                },
                helm_props={
                    "keys": [
                        "master.service.type",
                        "replica.service.type",
                        "sentinel.service.type",
                        "sentinel.masterService.type",
                    ],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 密码配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="password",
                label="密码",
                extra="Redis服务的密码，密码必须包含至少一个大写字母、一个小写字母、一个数字和一个特殊字符，最低 6 位",
                order=0,
                form_item_props={"required": True},
                type="password",
                initial_value="kubekylin@redis*SVR",
                rules=[
                    {
                        "type": "string",
                        "message": "密码必须包含至少一个大写字母、一个小写字母、一个数字和一个特殊字符，最低 6 位，最高 20 位",
                        "min": 6,
                        "max": 20
                    }
                ],
                field_props={
                    "placeholder": "请输入密码",
                    "options": []
                },
                helm_props={
                    "keys": ["global.redis.password"],
                    "type": "string",
                    "unit": "",
                },
            ),
        ],
    )

    return [redis_app]


# ============================ 辅助函数 ============================


def _print_separator(char: str = "=", length: int = 50) -> None:
    """
    打印分隔线

    Args:
        char: 分隔字符
        length: 分隔线长度
    """
    click.echo(char * length)


# ============================ 主程序入口 ============================


if __name__ == "__main__":
    # 启动 CLI 命令行入口
    cli()
