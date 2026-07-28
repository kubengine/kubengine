"""Flink Kubernetes Operator 应用默认配置（基于 apache/flink-kubernetes-operator chart）。"""

from datetime import datetime

from core.orm.app import AppSchema
from core.orm.app_field_config import AppFieldConfigSchema, ConfigTypeEnum


def get_flink_operator_app(create_time: datetime) -> AppSchema:
    """构建 Flink Kubernetes Operator 应用配置。

    Args:
        create_time: 应用创建时间

    Returns:
        Flink Kubernetes Operator 应用 Schema
    """
    return AppSchema(
        name="flink_operator",
        category=["计算"],
        description="Apache Flink Kubernetes Operator，用于在 Kubernetes 上自动化管理 Flink 应用生命周期",
        helm_chart="flink-kubernetes-operator",
        create_time=create_time,
        app_field_configs=[
            # ========== 集群相关配置 ==========
            # Operator 副本数
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="replicas",
                label="Operator副本数",
                extra="Operator 实例数，默认 1。开启 Leader Election 后可设置为多副本实现高可用",
                order=0,
                form_item_props={"required": True},
                type="radio",
                initial_value="1",
                rules=[],
                field_props={
                    "options": [
                        {"label": "1（单副本，默认）", "value": "1"},
                        {"label": "2（需开启 Leader Election）", "value": "2"},
                        {"label": "3（需开启 Leader Election）", "value": "3"},
                    ],
                },
                helm_props={
                    "keys": ["replicas"],
                    "type": "number",
                    "unit": "",
                },
            ),
            # 镜像版本
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="imageTag",
                label="镜像版本",
                extra="Flink Kubernetes Operator 镜像 Tag，对应 Operator 版本",
                order=1,
                form_item_props={"required": True},
                type="select",
                initial_value="f504138",
                rules=[],
                field_props={
                    "allowClear": False,
                    "options": [
                        {"label": "f504138（默认）", "value": "f504138"},
                    ],
                },
                helm_props={
                    "keys": ["image.tag"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # Operator CPU 配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="operatorCpu",
                label="Operator CPU",
                extra="Operator 主容器 CPU 核数，管理大规模 Flink 作业时建议 2 核及以上",
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
                    "keys": ["operatorPod.resources.requests.cpu"],
                    "type": "number",
                    "unit": "",
                },
            ),
            # Operator 内存配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="operatorMemory",
                label="Operator 内存",
                extra="Operator 主容器内存大小(单位 Gi)，默认建议不低于 1Gi",
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
                    "keys": ["operatorPod.resources.requests.memory"],
                    "type": "number",
                    "unit": "Gi",
                },
            ),
            # Webhook CPU 配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="webhookCpu",
                label="Webhook CPU",
                extra="Admission Webhook 容器 CPU 核数",
                order=4,
                form_item_props={"required": False},
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
                    "keys": ["operatorPod.webhook.resources.requests.cpu"],
                    "type": "number",
                    "unit": "",
                },
            ),
            # Webhook 内存配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="webhookMemory",
                label="Webhook 内存",
                extra="Admission Webhook 容器内存大小(单位 Gi)",
                order=5,
                form_item_props={"required": False},
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
                    "keys": ["operatorPod.webhook.resources.requests.memory"],
                    "type": "number",
                    "unit": "Gi",
                },
            ),
            # ========== 环境相关配置 ==========
            # 监听命名空间
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="watchNamespaces",
                label="监听命名空间",
                extra="Operator 监听 FlinkDeployment 变更的命名空间列表，多个用逗号分隔。留空表示监听所有命名空间（集群级别）",
                order=0,
                form_item_props={"required": False},
                type="text",
                initial_value="",
                rules=[],
                field_props={"placeholder": "如 flink,flink-jobs；留空监听所有命名空间", "options": []},
                helm_props={
                    "keys": ["watchNamespaces"],
                    "type": "array",
                    "unit": "",
                },
            ),
            # Webhook 开关
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="webhookCreate",
                label="Webhook 校验",
                extra="是否启用 Admission Webhook（对 FlinkDeployment 资源进行校验和变更）。生产环境建议开启",
                order=1,
                form_item_props={"required": True},
                type="radio",
                initial_value="true",
                rules=[],
                field_props={
                    "options": [
                        {"label": "开启（推荐）", "value": "true"},
                        {"label": "关闭", "value": "false"},
                    ],
                },
                helm_props={
                    "keys": ["webhook.create"],
                    "type": "boolean",
                    "unit": "",
                },
            ),
            # TLS 开关
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="tlsCreate",
                label="TLS 加密",
                extra="是否为 Operator 与 Kubernetes API 之间启用 TLS 加密通信",
                order=2,
                form_item_props={"required": True},
                type="radio",
                initial_value="false",
                rules=[],
                field_props={
                    "options": [
                        {"label": "关闭（默认）", "value": "false"},
                        {"label": "开启", "value": "true"},
                    ],
                },
                helm_props={
                    "keys": ["tls.create"],
                    "type": "boolean",
                    "unit": "",
                },
            ),
            # 默认配置加载
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="defaultConfigurationCreate",
                label="创建默认配置",
                extra="是否创建 Flink 默认配置 ConfigMap。关闭后下方 Flink 配置覆盖项将不生效",
                order=3,
                form_item_props={"required": True},
                type="radio",
                initial_value="true",
                rules=[],
                field_props={
                    "options": [
                        {"label": "开启（推荐）", "value": "true"},
                        {"label": "关闭", "value": "false"},
                    ],
                },
                helm_props={
                    "keys": ["defaultConfiguration.create"],
                    "type": "boolean",
                    "unit": "",
                },
            ),
            # 协调间隔
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="reconcileInterval",
                label="协调间隔",
                extra="Operator 协调 FlinkDeployment 状态的间隔。值越小响应越快但开销越大，生产环境建议 15s",
                order=4,
                form_item_props={"required": False},
                type="select",
                initial_value="15 s",
                rules=[],
                field_props={
                    "allowClear": False,
                    "options": [
                        {"label": "5 s（高频，开销大）", "value": "5 s"},
                        {"label": "15 s（默认推荐）", "value": "15 s"},
                        {"label": "30 s", "value": "30 s"},
                        {"label": "60 s（低频）", "value": "60 s"},
                    ],
                },
                helm_props={
                    "keys": ["defaultConfiguration.flink-conf.yaml.[[kubernetes.operator.reconcile.interval]]"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 观测进度检查间隔
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="observerProgressCheckInterval",
                label="观测进度检查间隔",
                extra="Operator 检查作业部署进度的间隔。值越小状态更新越及时但增加 API Server 负载",
                order=5,
                form_item_props={"required": False},
                type="select",
                initial_value="5 s",
                rules=[],
                field_props={
                    "allowClear": False,
                    "options": [
                        {"label": "5 s（默认）", "value": "5 s"},
                        {"label": "10 s", "value": "10 s"},
                        {"label": "30 s", "value": "30 s"},
                    ],
                },
                helm_props={
                    "keys": ["defaultConfiguration.flink-conf.yaml.[[kubernetes.operator.observer.progress-check.interval]]"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # Metrics 端口
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="metricsPort",
                label="Metrics 端口",
                extra="Operator 暴露 Metrics 指标的端口，留空则不暴露。对接监控系统时建议设置为 9999",
                order=6,
                form_item_props={"required": False},
                type="number",
                initial_value="",
                rules=[
                    {"type": "number", "message": "仅允许设置 1024 - 65535", "min": 1024, "max": 65535},
                ],
                field_props={"placeholder": "如 9999；留空不暴露", "options": []},
                helm_props={
                    "keys": ["metrics.port"],
                    "type": "number",
                    "unit": "",
                },
            ),
            # 健康检查端口
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="operatorHealthPort",
                label="健康检查端口",
                extra="Operator 健康检查端口（Liveness/Startup 探针），默认 8085",
                order=7,
                form_item_props={"required": True},
                type="number",
                initial_value=8085,
                rules=[
                    {"type": "number", "message": "仅允许设置 1024 - 65535", "min": 1024, "max": 65535},
                ],
                field_props={"options": []},
                helm_props={
                    "keys": ["operatorHealth.port"],
                    "type": "number",
                    "unit": "",
                },
            ),
        ],
    )
