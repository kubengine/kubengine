"""Flink 应用默认配置（基于 bitnami/flink chart）。"""

from datetime import datetime

from core.orm.app import AppSchema
from core.orm.app_field_config import AppFieldConfigSchema, ConfigTypeEnum


def get_flink_app(create_time: datetime) -> AppSchema:
    """构建 Flink 应用配置。

    Args:
        create_time: 应用创建时间

    Returns:
        Flink 应用 Schema
    """
    return AppSchema(
        name="flink",
        category=["计算"],
        description="Apache Flink 分布式流处理框架，支持有状态计算与批流统一处理",
        helm_chart="flink",
        create_time=create_time,
        app_field_configs=[
            # JobManager 副本数
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="jobmanagerReplicaCount",
                label="JobManager副本数",
                extra="JobManager 为集群协调节点，默认 1。多副本需额外配置高可用（如 Zookeeper）",
                order=0,
                form_item_props={"required": True},
                type="radio",
                initial_value="1",
                rules=[],
                field_props={
                    "options": [
                        {"label": "1（单副本，默认）", "value": "1"},
                        {"label": "2（需配置高可用）", "value": "2"},
                        {"label": "3（需配置高可用）", "value": "3"},
                    ]
                },
                helm_props={
                    "keys": ["jobmanager.replicaCount"],
                    "type": "number",
                    "unit": "",
                },
            ),
            # TaskManager 副本数
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="taskmanagerReplicaCount",
                label="TaskManager副本数",
                extra="TaskManager 为实际执行作业的工作节点，根据并行度与吞吐量需求横向扩展",
                order=1,
                form_item_props={"required": True},
                type="radio",
                initial_value="1",
                rules=[],
                field_props={
                    "options": [
                        {"label": "1（默认）", "value": "1"},
                        {"label": "2", "value": "2"},
                        {"label": "3", "value": "3"},
                        {"label": "5", "value": "5"},
                    ]
                },
                helm_props={
                    "keys": ["taskmanager.replicaCount"],
                    "type": "number",
                    "unit": "",
                },
            ),
            # CPU 配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="cpu",
                label="cpu",
                extra="每个节点的核数，JobManager 负责协调调度，TaskManager 执行实际计算，生产环境建议 2 核及以上",
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
                    ]
                },
                helm_props={
                    "keys": [
                        "jobmanager.resources.requests.cpu",
                        "taskmanager.resources.requests.cpu",
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
                extra="内存大小(单位 Gi)，Flink 对内存敏感，TaskManager 建议不低于 2Gi",
                order=3,
                form_item_props={"required": True},
                type="radio",
                initial_value="2",
                rules=[],
                field_props={
                    "options": [
                        {"label": "1", "value": "1"},
                        {"label": "2", "value": "2"},
                        {"label": "4", "value": "4"},
                        {"label": "8", "value": "8"},
                    ]
                },
                helm_props={
                    "keys": [
                        "jobmanager.resources.requests.memory",
                        "taskmanager.resources.requests.memory",
                    ],
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
                order=4,
                form_item_props={"required": True},
                type="radio",
                initial_value="ClusterIP",
                rules=[],
                field_props={
                    "options": [
                        {"label": "ClusterIP", "value": "ClusterIP"},
                        {"label": "LoadBalancer", "value": "LoadBalancer"},
                    ]
                },
                helm_props={
                    "keys": [
                        "jobmanager.service.type",
                        "taskmanager.service.type",
                    ],
                    "type": "string",
                    "unit": "",
                },
            ),
            # ========== 环境相关配置 ==========
            # 网络策略开关
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="networkPolicyEnabled",
                label="网络策略",
                extra="是否为 Flink Pod 创建 NetworkPolicy 进行网络隔离，生产环境建议开启",
                order=0,
                form_item_props={"required": False},
                type="switch",
                initial_value=None,
                rules=[],
                field_props={"options": []},
                helm_props={
                    "keys": [
                        "jobmanager.networkPolicy.enabled",
                        "taskmanager.networkPolicy.enabled",
                    ],
                    "type": "boolean",
                    "unit": "",
                },
            ),
            # 诊断模式
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="diagnosticMode",
                label="诊断模式",
                extra="开启后所有探针将被禁用并覆盖容器启动命令，用于排查容器启动问题。正常部署时请关闭",
                order=1,
                form_item_props={"required": False},
                type="switch",
                initial_value=None,
                rules=[],
                field_props={"options": []},
                helm_props={
                    "keys": ["diagnosticMode.enabled"],
                    "type": "boolean",
                    "unit": "",
                },
            ),
        ],
    )
