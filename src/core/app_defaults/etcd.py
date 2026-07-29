"""etcd 应用默认配置（基于 bitnami/etcd chart）。"""

from datetime import datetime

from core.orm.app import AppSchema
from core.orm.app_field_config import AppFieldConfigSchema, ConfigTypeEnum


def get_etcd_app(create_time: datetime) -> AppSchema:
    """构建 etcd 应用配置。

    Args:
        create_time: 应用创建时间

    Returns:
        etcd 应用 Schema
    """
    return AppSchema(
        name="etcd",
        category=["数据库"],
        description="高可用分布式键值存储，适用于配置中心、服务发现与元数据管理场景",
        helm_chart="etcd",
        create_time=create_time,
        app_field_configs=[
            # ========== 集群相关配置 ==========
            # 节点数
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="replicaCount",
                label="集群节点数",
                extra="etcd 集群节点数，1 为单机模式，生产环境建议 3 或 5（奇数）以保证 Raft 仲裁多数",
                order=0,
                form_item_props={"required": True},
                type="radio",
                initial_value="1",
                rules=[],
                field_props={
                    "options": [
                        {"label": "1（单节点）", "value": "1"},
                        {"label": "3（集群推荐）", "value": "3"},
                        {"label": "5", "value": "5"},
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
                extra="每个节点的核数，写入频繁场景建议 2 核及以上",
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
                extra="每个节点的内存大小(单位 Gi)，etcd 对内存敏感，建议不低于 1Gi",
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
                        {"label": "8", "value": "8"},
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
                extra="8Gi - 100Gi, 节点数据持久化存储大小。etcd 对磁盘 IO 敏感，建议使用高性能磁盘",
                order=3,
                form_item_props={"required": True},
                type="number",
                initial_value=8,
                rules=[
                    {"type": "number", "message": "仅允许设置 8 - 100", "min": 8, "max": 100},
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
                order=4,
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
            # root 密码
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="rootPassword",
                label="root用户密码",
                extra="etcd root 用户密码（默认开启 RBAC）。留空则自动生成",
                order=0,
                form_item_props={"required": False},
                type="password",
                initial_value="",
                rules=[
                    {
                        "type": "string",
                        "message": "密码需包含大小写字母、数字和特殊字符，最低 6 位，最高 20 位",
                        "min": 6,
                        "max": 20,
                    },
                ],
                field_props={"placeholder": "留空则自动生成"},
                helm_props={
                    "keys": ["auth.rbac.rootPassword"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 允许无认证访问
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="allowNoneAuthentication",
                label="允许无认证访问",
                extra="开启后允许未配置凭证访问 etcd。生产环境建议关闭（false）以强制认证",
                order=1,
                form_item_props={"required": True},
                type="radio",
                initial_value="true",
                rules=[],
                field_props={
                    "options": [
                        {"label": "开启（允许匿名访问）", "value": "true"},
                        {"label": "关闭（强制认证，推荐生产）", "value": "false"},
                    ],
                },
                helm_props={
                    "keys": ["auth.rbac.allowNoneAuthentication"],
                    "type": "boolean",
                    "unit": "",
                },
            ),
            # 日志级别
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="logLevel",
                label="日志级别",
                extra="etcd 进程日志级别，生产环境建议 info，排查问题时可调为 debug",
                order=2,
                form_item_props={"required": True},
                type="select",
                initial_value="info",
                rules=[],
                field_props={
                    "allowClear": False,
                    "options": [
                        {"label": "debug", "value": "debug"},
                        {"label": "info（默认）", "value": "info"},
                        {"label": "warn", "value": "warn"},
                        {"label": "error", "value": "error"},
                    ],
                },
                helm_props={
                    "keys": ["logLevel"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 自动压缩模式
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="autoCompactionMode",
                label="自动压缩模式",
                extra="历史数据压缩模式：periodic 按时长、revision 按版本号。留空表示不压缩",
                order=3,
                form_item_props={"required": False},
                type="select",
                initial_value="periodic",
                rules=[],
                field_props={
                    "allowClear": True,
                    "options": [
                        {"label": "不压缩", "value": ""},
                        {"label": "periodic（按时长）", "value": "periodic"},
                        {"label": "revision（按版本号）", "value": "revision"},
                    ],
                },
                helm_props={
                    "keys": ["autoCompactionMode"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 自动压缩保留时长
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="autoCompactionRetention",
                label="自动压缩保留",
                extra="压缩保留值。periodic 模式下为时长（如 1h），revision 模式下为版本数（如 1000）",
                order=4,
                form_item_props={"required": False},
                type="text",
                initial_value="1h",
                rules=[],
                field_props={"placeholder": "periodic 模式如 1h；revision 模式如 1000"},
                helm_props={
                    "keys": ["autoCompactionRetention"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 监控指标
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="metricsEnabled",
                label="监控指标导出",
                extra="开启后暴露 etcd 运行指标，供 Prometheus 等监控系统采集",
                order=5,
                form_item_props={"required": True},
                type="radio",
                initial_value="false",
                rules=[],
                field_props={
                    "options": [
                        {"label": "关闭", "value": "false"},
                        {"label": "开启", "value": "true"},
                    ],
                },
                helm_props={
                    "keys": ["metrics.enabled"],
                    "type": "boolean",
                    "unit": "",
                },
            ),
            # 灾难恢复快照
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="disasterRecovery",
                label="自动灾难恢复",
                extra="开启后定时对 keyspace 做快照，发生故障时可用快照恢复集群",
                order=6,
                form_item_props={"required": True},
                type="radio",
                initial_value="false",
                rules=[],
                field_props={
                    "options": [
                        {"label": "关闭", "value": "false"},
                        {"label": "开启", "value": "true"},
                    ],
                },
                helm_props={
                    "keys": ["disasterRecovery.enabled"],
                    "type": "boolean",
                    "unit": "",
                },
            ),
            # 快照卷存储类（灾难恢复快照 PVC，bitnami chart 默认为 nfs，需显式覆盖）
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="snapshotStorageClass",
                label="快照卷存储类",
                extra="开启灾难恢复时，快照卷使用的 StorageClass（需支持 ReadWriteMany 多节点读写）。留空则使用集群默认 StorageClass",
                order=7,
                form_item_props={"required": True},
                type="select",
                initial_value="longhorn",
                rules=[],
                field_props={
                    "allowClear": True,
                    "options": [
                        {"label": "longhorn", "value": "longhorn"},
                        {"label": "集群默认 StorageClass", "value": ""},
                    ],
                },
                helm_props={
                    "keys": ["disasterRecovery.pvc.storageClassName"],
                    "type": "string",
                    "unit": "",
                },
            ),
        ],
    )
