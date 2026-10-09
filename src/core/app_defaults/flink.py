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
        category=["消息与集成"],
        description="Apache Flink 分布式流处理框架，支持有状态计算与批流统一处理",
        helm_chart="flink",
        create_time=create_time,
        app_field_configs=[
            # JobManager 副本数
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="jobmanagerReplicaCount",
                label="JobManager副本数",
                extra=(
                    "JobManager 为集群协调节点，默认 1。多副本会自动启用 "
                    "Kubernetes 高可用，并使用共享存储（ReadWriteMany）"
                    "保存 HA 元数据，请确保默认存储类支持 RWX"
                ),
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
            # JobManager CPU 配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="jobmanagerCpu",
                label="JobManager cpu",
                extra=(
                    "JobManager 协调节点的 CPU 核数，负责任务调度与资源"
                    "管理，生产环境建议 2 核及以上"
                ),
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
                    "keys": ["jobmanager.resources.requests.cpu"],
                    "type": "number",
                    "unit": "",
                },
            ),
            # JobManager 内存配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="jobmanagerMemory",
                label="JobManager内存",
                extra=(
                    "JobManager 内存大小(单位 Gi)，用于作业调度与元数据"
                    "管理，生产环境建议 2Gi 及以上"
                ),
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
                    "keys": ["jobmanager.resources.requests.memory"],
                    "type": "number",
                    "unit": "Gi",
                },
            ),
            # JobManager 持久化存储大小
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="jobmanagerDisk",
                label="JobManager存储大小",
                extra=(
                    "8Gi - 100Gi, JobManager 协调节点的持久化存储大小，"
                    "用于存放作业元数据与检查点元信息，通常无需过大"
                ),
                order=4,
                form_item_props={"required": True},
                type="number",
                initial_value=8,
                rules=[
                    {
                        "type": "number",
                        "message": "仅允许设置 8 - 100",
                        "min": 8,
                        "max": 100,
                    },
                ],
                field_props={"options": []},
                helm_props={
                    "keys": ["jobmanager.persistence.size"],
                    "type": "number",
                    "unit": "Gi",
                },
            ),
            # JobManager 高可用元数据存储大小
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="highAvailabilityDisk",
                label="HA元数据存储大小",
                extra=(
                    "1Gi - 50Gi, JobManager 高可用的共享元数据存储大小"
                    "（ReadWriteMany），用于存放 job graph、作业 jar 与"
                    "检查点元数据指针，全集群共用一份；仅当 JobManager"
                    " 副本数大于 1 时生效，与各节点本地数据盘无关"
                ),
                order=5,
                form_item_props={"required": True},
                type="number",
                initial_value=2,
                rules=[
                    {
                        "type": "number",
                        "message": "仅允许设置 1 - 50",
                        "min": 1,
                        "max": 50,
                    },
                ],
                field_props={"options": []},
                helm_props={
                    "keys": [
                        "jobmanager.highAvailability.persistence.size"
                    ],
                    "type": "number",
                    "unit": "Gi",
                },
            ),
            # TaskManager CPU 配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="taskmanagerCpu",
                label="TaskManager cpu",
                extra=(
                    "TaskManager 工作节点的 CPU 核数，负责执行实际计算，"
                    "生产环境建议 2 核及以上"
                ),
                order=6,
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
                    "keys": ["taskmanager.resources.requests.cpu"],
                    "type": "number",
                    "unit": "",
                },
            ),
            # TaskManager 内存配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="taskmanagerMemory",
                label="TaskManager内存",
                extra=(
                    "TaskManager 内存大小(单位 Gi)，用于任务执行与状态"
                    "存储，Flink 对内存敏感，建议不低于 2Gi"
                ),
                order=7,
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
                    "keys": ["taskmanager.resources.requests.memory"],
                    "type": "number",
                    "unit": "Gi",
                },
            ),
            # TaskManager 持久化存储大小
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="taskmanagerDisk",
                label="TaskManager存储大小",
                extra=(
                    "8Gi - 100Gi, TaskManager 工作节点的持久化存储大小，"
                    "用于存放本地状态（如 RocksDB）与中间结果，"
                    "建议按状态规模评估"
                ),
                order=8,
                form_item_props={"required": True},
                type="number",
                initial_value=8,
                rules=[
                    {
                        "type": "number",
                        "message": "仅允许设置 8 - 100",
                        "min": 8,
                        "max": 100,
                    },
                ],
                field_props={"options": []},
                helm_props={
                    "keys": ["taskmanager.persistence.size"],
                    "type": "number",
                    "unit": "Gi",
                },
            ),
            # Service 类型
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="service",
                label="Service服务",
                extra=(
                    "请选择 K8s Service 类型（ClusterIP 集群内用、LoadBalancer"
                    " 公网负载均衡）"
                ),
                order=9,
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
                extra=(
                    "开启后所有探针将被禁用并覆盖容器启动命令，"
                    "用于排查容器启动问题。正常部署时请关闭"
                ),
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
