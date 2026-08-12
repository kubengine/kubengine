"""RabbitMQ 应用默认配置（基于 bitnami/rabbitmq chart）。"""

from datetime import datetime

from core.orm.app import AppSchema
from core.orm.app_field_config import AppFieldConfigSchema, ConfigTypeEnum


def get_rabbitmq_app(create_time: datetime) -> AppSchema:
    """构建 RabbitMQ 应用配置。

    Args:
        create_time: 应用创建时间

    Returns:
        RabbitMQ 应用 Schema
    """
    return AppSchema(
        name="rabbitmq",
        category=["消息与集成"],
        description="高可用 RabbitMQ 消息队列，支持 AMQP、MQTT、STOMP 等协议",
        helm_chart="rabbitmq",
        create_time=create_time,
        app_field_configs=[
            # 节点数
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="replicaCount",
                label="集群节点数",
                extra="RabbitMQ 集群节点数，1 为单机模式，3 及以上为集群模式（需保证 Erlang Cookie 一致）",
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
                extra="每个节点的核数",
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
                extra="内存大小(单位 Gi)，RabbitMQ 建议内存不低于 1Gi",
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
                extra="8Gi - 50Gi, 节点持久化存储大小",
                order=3,
                form_item_props={"required": True},
                type="number",
                initial_value=8,
                rules=[
                    {"type": "number", "message": "仅允许设置8 - 50", "min": 8, "max": 50}
                ],
                field_props={},
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
            # 用户名
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="username",
                label="管理员用户名",
                extra="RabbitMQ 管理账号的用户名",
                order=0,
                form_item_props={"required": True},
                type="text",
                initial_value="admin",
                rules=[
                    {"required": True, "message": "请输入管理员用户名"},
                    {"min": 3, "message": "用户名至少3个字符"},
                ],
                field_props={},
                helm_props={
                    "keys": ["auth.username"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 密码
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="password",
                label="管理员密码",
                extra="RabbitMQ 管理账号的密码，留空则自动生成",
                order=1,
                form_item_props={"required": False},
                type="password",
                initial_value="",
                rules=[],
                field_props={},
                helm_props={
                    "keys": ["auth.password"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # Erlang Cookie
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="erlangCookie",
                label="Erlang Cookie",
                extra="集群节点间通信认证密钥，多节点部署时必须一致，留空则自动生成",
                order=2,
                form_item_props={"required": False},
                type="password",
                initial_value="",
                rules=[],
                field_props={},
                helm_props={
                    "keys": ["auth.erlangCookie"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 内存高水位线类型
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="memoryWatermarkType",
                label="内存高水位线类型",
                extra="absolute: 绝对内存值; relative: 相对比例（0~1）",
                order=3,
                form_item_props={"required": True},
                type="select",
                initial_value="relative",
                rules=[],
                field_props={
                    "allowClear": False,
                    "options": [
                        {"label": "relative（相对比例，默认 0.4）", "value": "relative"},
                        {"label": "absolute（绝对值，单位 MiB）", "value": "absolute"},
                    ],
                },
                helm_props={
                    "keys": ["memoryHighWatermark.type"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 内存高水位线值
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="memoryWatermarkValue",
                label="内存高水位线值",
                extra="当类型为 relative 时取值 0~1（如 0.4 表示 40%），类型为 absolute 时单位为 MiB",
                order=4,
                form_item_props={"required": True},
                type="text",
                initial_value="0.4",
                rules=[],
                field_props={},
                helm_props={
                    "keys": ["memoryHighWatermark.value"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 额外插件
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="extraPlugins",
                label="额外插件",
                extra="逗号分隔的插件列表，如 rabbitmq_mqtt,rabbitmq_management",
                order=5,
                form_item_props={"required": False},
                type="text",
                initial_value="rabbitmq_management",
                rules=[],
                field_props={},
                helm_props={
                    "keys": ["extraPlugins"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 集群分区处理
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="clusterPartitionHandling",
                label="集群分区处理策略",
                extra="网络分区时的处理策略，单节点可忽略",
                order=6,
                form_item_props={"required": True},
                type="select",
                initial_value="pause_minority",
                rules=[],
                field_props={
                    "allowClear": False,
                    "options": [
                        {"label": "ignore（忽略，不推荐）", "value": "ignore"},
                        {"label": "pause_minority（少数派暂停，推荐）", "value": "pause_minority"},
                        {"label": "autoheal（自动恢复）", "value": "autoheal"},
                    ],
                },
                helm_props={
                    "keys": ["clustering.partitionHandling"],
                    "type": "string",
                    "unit": "",
                },
            ),
        ],
    )
