"""XXL-Job-Admin 应用默认配置（基于 xxl-job-admin chart）。"""

from datetime import datetime

from core.orm.app import AppSchema
from core.orm.app_field_config import AppFieldConfigSchema, ConfigTypeEnum


def get_xxl_job_admin_app(create_time: datetime) -> AppSchema:
    """构建 XXL-Job-Admin 应用配置。

    Args:
        create_time: 应用创建时间

    Returns:
        XXL-Job-Admin 应用 Schema
    """
    return AppSchema(
        name="xxl-job-admin",
        category=["应用支撑"],
        description="分布式任务调度平台，提供动态调度、任务路由、故障转移与日志监控能力",
        helm_chart="xxl-job-admin",
        create_time=create_time,
        app_field_configs=[
            # ========== 集群相关配置 ==========
            # 副本数
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="replicaCount",
                label="副本数",
                extra="XXL-Job-Admin 节点副本数，无状态应用可多副本横向扩展",
                order=0,
                form_item_props={"required": True},
                type="radio",
                initial_value="1",
                rules=[],
                field_props={
                    "options": [
                        {"label": "1（单节点）", "value": "1"},
                        {"label": "2", "value": "2"},
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
                extra="每个 XXL-Job-Admin 节点的核数，调度任务较多时建议 2 核及以上",
                order=1,
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
                extra="每个 XXL-Job-Admin 节点的内存大小(单位 Gi)",
                order=2,
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
            # Service 类型
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="service",
                label="Service服务",
                extra="请选择 K8s Service 类型（ClusterIP 集群内访问、LoadBalancer 公网负载均衡）",
                order=3,
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
                extra="开启后部署内置 MySQL 作为 XXL-Job-Admin 元数据存储（开箱即用）；关闭后需填写下方外部数据库信息",
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
                    "keys": ["mysql.primary.persistence.size"],
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
                initial_value="kubengine@xxl*SVR",
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
                extra="内置 MySQL 业务用户（xxl_job）密码",
                order=3,
                form_item_props={"required": True},
                type="password",
                initial_value="kubengine@xxl*SVR",
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
            # 外部数据库地址
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="externalDatabaseHost",
                label="外部数据库地址",
                extra="关闭内置 MySQL 时必填，XXL-Job-Admin 元数据库（MySQL）访问地址",
                order=4,
                form_item_props={"required": False},
                type="text",
                initial_value="",
                rules=[],
                field_props={"placeholder": "如 mysql.apps.svc.cluster.local"},
                helm_props={
                    "keys": ["externalDatabase.host"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 外部数据库端口
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="externalDatabasePort",
                label="外部数据库端口",
                extra="关闭内置 MySQL 时必填，XXL-Job-Admin 元数据库（MySQL）端口（默认 3306，范围 1 - 65535）",
                order=5,
                form_item_props={"required": True},
                type="number",
                initial_value=3306,
                rules=[
                    {"type": "number", "message": "仅允许设置 1 - 65535", "min": 1, "max": 65535},
                ],
                field_props={"options": []},
                helm_props={
                    "keys": ["externalDatabase.port"],
                    "type": "number",
                    "unit": "",
                },
            ),
            # 外部数据库名
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="externalDatabaseName",
                label="外部数据库名",
                extra="关闭内置 MySQL 时必填，需提前在 MySQL 中导入 tables_xxl_job.sql",
                order=6,
                form_item_props={"required": True},
                type="text",
                initial_value="xxl_job",
                rules=[],
                field_props={"placeholder": "请输入数据库名称"},
                helm_props={
                    "keys": ["externalDatabase.database"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 外部数据库用户
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="externalDatabaseUser",
                label="外部数据库账号",
                extra="关闭内置 MySQL 时必填，XXL-Job-Admin 元数据库（MySQL）业务用户名",
                order=7,
                form_item_props={"required": True},
                type="text",
                initial_value="xxl_job",
                rules=[],
                field_props={"placeholder": "请输入数据库用户名"},
                helm_props={
                    "keys": ["externalDatabase.user"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 外部数据库密码
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="externalDatabasePassword",
                label="外部数据库密码",
                extra="关闭内置 MySQL 时必填，XXL-Job-Admin 元数据库（MySQL）业务用户密码",
                order=8,
                form_item_props={"required": True},
                type="password",
                initial_value="kubengine@xxl*SVR",
                rules=[
                    {
                        "type": "string",
                        "message": "密码需包含大小写字母、数字和特殊字符，最低 6 位，最高 20 位",
                        "min": 6,
                        "max": 20,
                    },
                ],
                field_props={"placeholder": "请输入数据库密码"},
                helm_props={
                    "keys": ["externalDatabase.password"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 调度通讯令牌
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="xxlJobAccessToken",
                label="调度通讯令牌",
                extra="XXL-Job 调度中心与执行器之间的通讯令牌，执行器必须使用相同令牌才能注册",
                order=9,
                form_item_props={"required": True},
                type="password",
                initial_value="kubengine@xxl*SVR",
                rules=[
                    {
                        "type": "string",
                        "message": "令牌需包含大小写字母、数字和特殊字符，最低 6 位，最高 20 位",
                        "min": 6,
                        "max": 20,
                    },
                ],
                field_props={"placeholder": "请输入通讯令牌"},
                helm_props={
                    "keys": ["xxlJobAccessToken"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 控制台默认账号（仅提示，不映射 Helm key）
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="consoleAccount",
                label="控制台账号",
                extra="XXL-Job-Admin 控制台默认账号为 admin/123456，部署完成后请登录控制台及时修改密码（此项仅作提示，不会写入 Helm 配置）",
                order=10,
                form_item_props={"required": False},
                type="text",
                initial_value="admin/123456",
                rules=[],
                field_props={"disabled": True, "placeholder": "admin/123456"},
                helm_props={
                    "keys": [],
                    "type": "string",
                    "unit": "",
                },
            ),
        ],
    )
