"""Nacos 应用默认配置（基于 nacos chart）。"""

from datetime import datetime

from core.orm.app import AppSchema
from core.orm.app_field_config import AppFieldConfigSchema, ConfigTypeEnum


def get_nacos_app(create_time: datetime) -> AppSchema:
    """构建 Nacos 应用配置。

    Args:
        create_time: 应用创建时间

    Returns:
        Nacos 应用 Schema
    """
    return AppSchema(
        name="nacos",
        category=["消息与集成"],
        description="易于使用的动态服务发现、配置管理和服务管理平台，用于构建云原生应用",
        helm_chart="nacos",
        create_time=create_time,
        app_field_configs=[
            # ========== 集群相关配置 ==========
            # 部署模式
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="mode",
                label="部署模式",
                extra="Nacos 部署模式。单机模式需同时关闭内置 MySQL；集群模式建议 3 节点以实现高可用",
                order=0,
                form_item_props={"required": True},
                type="select",
                initial_value="cluster",
                rules=[],
                field_props={
                    "allowClear": False,
                    "options": [
                        {"label": "集群模式", "value": "cluster"},
                        {"label": "单机模式", "value": "standalone"},
                    ],
                },
                helm_props={
                    "keys": ["mode"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 副本数
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="replicaCount",
                label="副本数",
                extra="Nacos 节点副本数，集群模式下建议 3（奇数）以保证选举仲裁",
                order=1,
                form_item_props={"required": True},
                type="radio",
                initial_value="3",
                rules=[],
                field_props={
                    "options": [
                        {"label": "1（单节点）", "value": "1"},
                        {"label": "3（推荐）", "value": "3"},
                    ],
                },
                helm_props={
                    "keys": ["replicaCount"],
                    "type": "number",
                    "unit": "",
                },
            ),
            # CPU 配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="cpu",
                label="cpu",
                extra="每个 Nacos 节点的核数，配置与服务规模较大时建议 2 核及以上",
                order=2,
                form_item_props={"required": True},
                type="radio",
                initial_value="1",
                rules=[],
                field_props={
                    "options": [
                        {"label": "1", "value": "1"},
                        {"label": "2", "value": "2"},
                    ],
                },
                helm_props={
                    "keys": ["resources.requests.cpu"],
                    "type": "number",
                    "unit": "",
                },
            ),
            # 内存配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="memory",
                label="内存",
                extra="每个 Nacos 节点的内存大小(单位 Gi)",
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
                    ],
                },
                helm_props={
                    "keys": ["resources.requests.memory"],
                    "type": "number",
                    "unit": "Gi",
                },
            ),
            # 磁盘配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="disk",
                label="硬盘大小",
                extra="5Gi - 50Gi, Nacos 节点数据持久化磁盘大小",
                order=4,
                form_item_props={"required": True},
                type="number",
                initial_value=5,
                rules=[
                    {"type": "number", "message": "仅允许设置 5 - 50", "min": 5, "max": 50},
                ],
                field_props={"options": []},
                helm_props={
                    "keys": ["persistence.size"],
                    "type": "number",
                    "unit": "Gi",
                },
            ),
            # Service 类型
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="service",
                label="Service服务",
                extra="请选择 K8s Service 类型（ClusterIP 集群内访问、LoadBalancer 公网负载均衡）",
                order=5,
                form_item_props={"required": True},
                type="radio",
                initial_value="ClusterIP",
                rules=[],
                field_props={
                    "options": [
                        {"label": "ClusterIP", "value": "ClusterIP"},
                        {"label": "LoadBalancer", "value": "LoadBalancer"},
                    ],
                },
                helm_props={
                    "keys": ["service.type"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # ========== 服务环境参数设置 ==========
            # 内置 MySQL
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="mysqlEnabled",
                label="内置MySQL",
                extra="开启后部署内置 MySQL 作为 Nacos 配置存储；单机模式下建议关闭并使用外部数据库",
                order=0,
                form_item_props={"required": True},
                type="radio",
                initial_value="true",
                rules=[],
                field_props={
                    "options": [
                        {"label": "开启", "value": "true"},
                        {"label": "关闭", "value": "false"},
                    ],
                },
                helm_props={
                    "keys": ["mysql.enabled"],
                    "type": "boolean",
                    "unit": "",
                },
            ),
            # MySQL 存储大小
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="mysqlDisk",
                label="MySQL存储大小",
                extra="8Gi - 50Gi, 内置 MySQL 的持久化磁盘大小",
                order=1,
                form_item_props={"required": True},
                type="number",
                initial_value=8,
                rules=[
                    {"type": "number", "message": "仅允许设置 8 - 50", "min": 8, "max": 50},
                ],
                field_props={"options": []},
                helm_props={
                    "keys": [
                        "mysql.primary.persistence.size",
                        "mysql.secondary.persistence.size",
                    ],
                    "type": "number",
                    "unit": "Gi",
                },
            ),
            # MySQL root 密码
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="mysqlRootPassword",
                label="MySQL root密码",
                extra="内置 MySQL root 用户密码",
                order=2,
                form_item_props={"required": True},
                type="password",
                initial_value="kubengine@nacos*SVR",
                rules=[
                    {
                        "type": "string",
                        "message": "密码需包含大小写字母、数字和特殊字符，最低 6 位，最高 20 位",
                        "min": 6,
                        "max": 20,
                    },
                ],
                field_props={"placeholder": "请输入密码"},
                helm_props={
                    "keys": ["mysql.auth.rootPassword"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # MySQL 业务用户密码
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="mysqlPassword",
                label="MySQL业务密码",
                extra="内置 MySQL 业务用户（nacos）密码",
                order=3,
                form_item_props={"required": True},
                type="password",
                initial_value="kubengine@nacos*SVR",
                rules=[
                    {
                        "type": "string",
                        "message": "密码需包含大小写字母、数字和特殊字符，最低 6 位，最高 20 位",
                        "min": 6,
                        "max": 20,
                    },
                ],
                field_props={"placeholder": "请输入密码"},
                helm_props={
                    "keys": ["mysql.auth.password"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # Nacos 控制台默认账号（仅提示，不映射 Helm key）
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="consoleAccount",
                label="控制台账号",
                extra="Nacos 控制台默认账号为 nacos/nacos，部署完成后请登录控制台及时修改密码（此项仅作提示，不会写入 Helm 配置）",
                order=4,
                form_item_props={"required": False},
                type="text",
                initial_value="nacos/nacos",
                rules=[],
                field_props={"disabled": True, "placeholder": "nacos/nacos"},
                helm_props={
                    "keys": [],
                    "type": "string",
                    "unit": "",
                },
            ),
        ],
    )
