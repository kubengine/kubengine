"""Kafka 应用默认配置（基于 bitnami/kafka chart，KRaft 模式）。"""

from datetime import datetime

from core.orm.app import AppSchema
from core.orm.app_field_config import AppFieldConfigSchema, ConfigTypeEnum


def get_kafka_app(create_time: datetime) -> AppSchema:
    """构建 Kafka 应用配置。

    Args:
        create_time: 应用创建时间

    Returns:
        Kafka 应用 Schema
    """
    return AppSchema(
        name="kafka",
        category=["中间件"],
        description="高可用分布式 Kafka 消息队列（KRaft 模式，无需外部 Zookeeper）",
        helm_chart="kafka",
        create_time=create_time,
        app_field_configs=[
            # 节点数
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="nodes",
                label="集群节点数",
                extra="KRaft 模式下 controller+broker 组合节点数，建议奇数（3 或 5）以保证仲裁多数",
                order=0,
                form_item_props={"required": True},
                type="radio",
                initial_value="3",
                rules=[],
                field_props={
                    "options": [
                        {"label": "1（单节点，仅测试）", "value": "1"},
                        {"label": "3（推荐）", "value": "3"},
                        {"label": "5", "value": "5"},
                    ],
                },
                helm_props={
                    "keys": ["controller.replicaCount"],
                    "type": "number",
                    "unit": "",
                },
            ),
            # CPU 配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="cpu",
                label="cpu",
                extra="每个节点的核数，Kafka 对 CPU 敏感，高吞吐场景建议 2 核及以上",
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
                    "keys": ["controller.resources.requests.cpu"],
                    "type": "number",
                    "unit": "",
                },
            ),
            # 内存配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="memory",
                label="内存",
                extra="每个节点的内存大小(单位 Gi)，Kafka 堆内存默认占用可用内存的 75%",
                order=2,
                form_item_props={"required": True},
                type="radio",
                initial_value="2",
                rules=[],
                field_props={
                    "options": [
                        {"label": "1", "value": "1"},
                        {"label": "2", "value": "2"},
                        {"label": "4", "value": "4"},
                    ],
                },
                helm_props={
                    "keys": ["controller.resources.requests.memory"],
                    "type": "number",
                    "unit": "Gi",
                },
            ),
            # 磁盘配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="disk",
                label="硬盘大小",
                extra="8Gi - 64Gi, 节点数据日志磁盘大小，建议根据保留时长与吞吐量评估",
                order=3,
                form_item_props={"required": True},
                type="number",
                initial_value=8,
                rules=[
                    {"type": "number", "message": "仅允许设置 8 - 64", "min": 8, "max": 64},
                ],
                field_props={"options": []},
                helm_props={
                    "keys": ["controller.persistence.size"],
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
            # 客户端用户名（支持数组，逗号分隔多个用户名）
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="clientUser",
                label="客户端用户名",
                extra="SASL 客户端认证用户名，多个用户名用逗号分隔（默认 SASL_PLAINTEXT+PLAIN 机制）",
                order=0,
                form_item_props={"required": True},
                type="text",
                initial_value="user1",
                rules=[],
                field_props={"placeholder": "多个用户名用逗号分隔，如 user1,user2", "options": []},
                helm_props={
                    "keys": ["sasl.client.users"],
                    "type": "array",
                    "unit": "",
                },
            ),
            # 客户端密码
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="clientPassword",
                label="客户端密码",
                extra="SASL 客户端认证密码（默认用户名 user1，SASL_PLAINTEXT+PLAIN）；自定义需包含大小写字母、数字，最低 6 位",
                order=1,
                form_item_props={"required": True},
                type="password",
                initial_value="kubengine@kafka*CLI",
                rules=[
                    {
                        "type": "string",
                        "message": "密码需包含大小写字母、数字和特殊字符，最低 6 位，最高 20 位",
                        "min": 6,
                        "max": 20,
                    },
                ],
                field_props={"placeholder": "请输入客户端密码", "options": []},
                helm_props={
                    "keys": ["sasl.client.passwords"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 日志保留时间
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="logRetentionHours",
                label="日志保留时间",
                extra="Kafka 日志保留时间（小时），超过此时间的日志将被清理。生产环境建议 168（7天）",
                order=2,
                form_item_props={"required": False},
                type="number",
                initial_value=168,
                rules=[
                    {"type": "number", "message": "仅允许设置 1 - 8760", "min": 1, "max": 8760},
                ],
                field_props={"options": []},
                helm_props={
                    "keys": ["overrideConfiguration.[[log.retention.hours]]"],
                    "type": "number",
                    "unit": "",
                },
            ),
            # 自动创建 Topic
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="autoCreateTopicsEnable",
                label="自动创建Topic",
                extra="是否允许 Producer 自动创建不存在的 Topic。生产环境建议关闭（false）以避免误创建",
                order=3,
                form_item_props={"required": False},
                type="radio",
                initial_value="false",
                rules=[],
                field_props={
                    "options": [
                        {"label": "关闭（推荐生产）", "value": "false"},
                        {"label": "开启", "value": "true"},
                    ],
                },
                helm_props={
                    "keys": ["overrideConfiguration.autoCreateTopicsEnable"],
                    "type": "boolean",
                    "unit": "",
                },
            ),
            # 默认副本因子
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="defaultReplicationFactor",
                label="默认副本数",
                extra="Topic 默认副本因子，建议与 Broker 节点数匹配（3节点集群可选 2 或 3）",
                order=4,
                form_item_props={"required": False},
                type="radio",
                initial_value="1",
                rules=[],
                field_props={
                    "options": [
                        {"label": "1（无副本）", "value": "1"},
                        {"label": "2", "value": "2"},
                        {"label": "3（3节点推荐）", "value": "3"},
                    ],
                },
                helm_props={
                    "keys": ["overrideConfiguration.defaultReplicationFactor"],
                    "type": "number",
                    "unit": "",
                },
            ),
            # 默认分区数
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="numPartitions",
                label="默认分区数",
                extra="自动创建的 Topic 默认分区数，分区越多并行度越高。建议根据预期吞吐量设置",
                order=5,
                form_item_props={"required": False},
                type="radio",
                initial_value="1",
                rules=[],
                field_props={
                    "options": [
                        {"label": "1", "value": "1"},
                        {"label": "3", "value": "3"},
                        {"label": "6", "value": "6"},
                        {"label": "12", "value": "12"},
                    ],
                },
                helm_props={
                    "keys": ["overrideConfiguration.numPartitions"],
                    "type": "number",
                    "unit": "",
                },
            ),
            # 消息最大字节数
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="maxMessageBytes",
                label="消息最大字节",
                extra="单个消息最大字节数（bytes），默认 1048576（1MB）。传输大消息时可适当调大",
                order=6,
                form_item_props={"required": False},
                type="select",
                initial_value="1048576",
                rules=[],
                field_props={
                    "options": [
                        {"label": "512KB", "value": "524288"},
                        {"label": "1MB（默认）", "value": "1048576"},
                        {"label": "2MB", "value": "2097152"},
                        {"label": "5MB", "value": "5242880"},
                        {"label": "10MB", "value": "10485760"},
                    ],
                },
                helm_props={
                    "keys": ["overrideConfiguration.[[message.max.bytes]]"],
                    "type": "number",
                    "unit": "",
                },
            ),
            # 消息压缩类型
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="compressionType",
                label="消息压缩类型",
                extra="Producer 端消息压缩算法，producer 表示由 producer 自行决定。压缩可降低网络带宽但增加 CPU 开销",
                order=7,
                form_item_props={"required": False},
                type="radio",
                initial_value="producer",
                rules=[],
                field_props={
                    "options": [
                        {"label": "producer（由客户端决定）", "value": "producer"},
                        {"label": "gzip", "value": "gzip"},
                        {"label": "snappy", "value": "snappy"},
                        {"label": "lz4", "value": "lz4"},
                        {"label": "zstd", "value": "zstd"},
                    ],
                },
                helm_props={
                    "keys": ["overrideConfiguration.[[compression.type]]"],
                    "type": "string",
                    "unit": "",
                },
            ),
        ],
    )
