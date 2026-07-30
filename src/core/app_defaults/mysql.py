"""MySQL 应用默认配置（基于 bitnami/mysql chart）。"""

from datetime import datetime

from core.orm.app import AppSchema
from core.orm.app_field_config import AppFieldConfigSchema, ConfigTypeEnum


def get_mysql_app(create_time: datetime) -> AppSchema:
    """构建 MySQL 应用配置。

    Args:
        create_time: 应用创建时间

    Returns:
        MySQL 应用 Schema
    """
    return AppSchema(
        name="mysql",
        category=["数据库"],
        description="开源的关系型数据库系统，适用于高负载生产环境下的关键业务应用",
        helm_chart="mysql",
        create_time=create_time,
        app_field_configs=[
            # ========== 集群相关配置 ==========
            # 部署模式
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="architecture",
                label="部署模式",
                extra="MySQL 部署模式。单机模式为单节点；主从复制模式建议 2 个及以上副本以保证高可用",
                order=0,
                form_item_props={"required": True},
                type="select",
                initial_value="standalone",
                rules=[],
                field_props={
                    "allowClear": False,
                    "options": [
                        {"label": "单机模式", "value": "standalone"},
                        {"label": "主从复制模式", "value": "replication"},
                    ],
                },
                helm_props={
                    "keys": ["architecture"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 副本数
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="replicaCount",
                label="副本数",
                extra="MySQL 节点副本数，主从复制模式下建议 2（含 1 主 1 从）",
                order=1,
                form_item_props={"required": True},
                type="radio",
                initial_value="1",
                rules=[],
                field_props={
                    "options": [
                        {"label": "1（单节点）", "value": "1"},
                        {"label": "2（1主1从）", "value": "2"},
                    ],
                },
                helm_props={
                    "keys": ["primary.replicaCount"],
                    "type": "number",
                    "unit": "",
                },
            ),
            # CPU 配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="cpu",
                label="cpu",
                extra="每个 MySQL 节点的核数，数据量较大或并发较高时建议 2 核及以上",
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
                    "keys": ["primary.resources.requests.cpu"],
                    "type": "number",
                    "unit": "",
                },
            ),
            # 内存配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="memory",
                label="内存",
                extra="每个 MySQL 节点的内存大小(单位 Gi)",
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
                    "keys": ["primary.resources.requests.memory"],
                    "type": "number",
                    "unit": "Gi",
                },
            ),
            # 磁盘配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="disk",
                label="硬盘大小",
                extra="8Gi - 100Gi, MySQL 数据持久化磁盘大小",
                order=4,
                form_item_props={"required": True},
                type="number",
                initial_value=8,
                rules=[
                    {"type": "number", "message": "仅允许设置 8 - 100", "min": 8, "max": 100},
                ],
                field_props={"options": []},
                helm_props={
                    "keys": ["primary.persistence.size"],
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
                    "keys": ["primary.service.type"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 端口配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="port",
                label="端口",
                extra="MySQL 服务暴露端口（默认 3306，范围 1 - 65535）",
                order=6,
                form_item_props={"required": True},
                type="number",
                initial_value=3306,
                rules=[
                    {"type": "number", "message": "仅允许设置 1 - 65535", "min": 1, "max": 65535},
                ],
                field_props={"options": []},
                helm_props={
                    "keys": ["primary.service.ports.mysql"],
                    "type": "number",
                    "unit": "",
                },
            ),
            # ========== 服务环境参数设置 ==========
            # root 密码
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="mysqlRootPassword",
                label="root密码",
                extra="MySQL root 用户密码",
                order=0,
                form_item_props={"required": True},
                type="password",
                initial_value="kubengine@mysql*SVR",
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
                    "keys": ["auth.rootPassword"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 业务库名称
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="mysqlDatabase",
                label="业务库名",
                extra="MySQL 自动创建的业务数据库名称",
                order=1,
                form_item_props={"required": True},
                type="text",
                initial_value="my_database",
                rules=[],
                field_props={"placeholder": "请输入数据库名称"},
                helm_props={
                    "keys": ["auth.database"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 业务用户
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="mysqlUsername",
                label="业务账号",
                extra="MySQL 业务用户名（可留空，留空则使用 root）",
                order=2,
                form_item_props={"required": False},
                type="text",
                initial_value="",
                rules=[],
                field_props={"placeholder": "请输入业务用户名"},
                helm_props={
                    "keys": ["auth.username"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 业务用户密码
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="mysqlPassword",
                label="业务密码",
                extra="MySQL 业务用户密码（若业务账号留空则此项不生效）",
                order=3,
                form_item_props={"required": True},
                type="password",
                initial_value="kubengine@mysql*SVR",
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
                    "keys": ["auth.password"],
                    "type": "string",
                    "unit": "",
                },
            ),
        ],
    )
