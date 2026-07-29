"""Apache APISIX 应用默认配置（基于 bitnami/apisix chart）。"""

from datetime import datetime

from core.orm.app import AppSchema
from core.orm.app_field_config import AppFieldConfigSchema, ConfigTypeEnum


def get_apisix_app(create_time: datetime) -> AppSchema:
    """构建 Apache APISIX 应用配置。

    Args:
        create_time: 应用创建时间

    Returns:
        Apache APISIX 应用 Schema
    """
    return AppSchema(
        name="apisix",
        category=["消息与集成"],
        description="高性能实时 API 网关，提供负载均衡、动态上游、灰度发布、熔断、认证与可观测性等能力",
        helm_chart="apisix",
        create_time=create_time,
        app_field_configs=[
            # ========== 集群相关配置 ==========
            # 数据面副本数
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="dataReplicas",
                label="数据面副本数",
                extra="APISIX 数据面（实际处理流量的网关）副本数，建议 2 及以上以实现高可用",
                order=0,
                form_item_props={"required": True},
                type="radio",
                initial_value="1",
                rules=[],
                field_props={
                    "options": [
                        {"label": "1（单节点）", "value": "1"},
                        {"label": "2（推荐）", "value": "2"},
                        {"label": "3", "value": "3"},
                    ],
                },
                helm_props={
                    "keys": ["dataPlane.replicaCount"],
                    "type": "number",
                    "unit": "",
                },
            ),
            # CPU 配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="cpu",
                label="cpu",
                extra="数据面与控制面每个节点的核数，高 QPS 场景建议 2 核及以上",
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
                    "keys": [
                        "dataPlane.resources.requests.cpu",
                        "controlPlane.resources.requests.cpu",
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
                extra="数据面与控制面每个节点的内存大小(单位 Gi)",
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
                    "keys": [
                        "dataPlane.resources.requests.memory",
                        "controlPlane.resources.requests.memory",
                    ],
                    "type": "number",
                    "unit": "Gi",
                },
            ),
            # etcd 存储盘
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="etcdDisk",
                label="etcd存储大小",
                extra="8Gi - 50Gi, 内置 etcd（配置存储后端）的持久化磁盘大小",
                order=3,
                form_item_props={"required": True},
                type="number",
                initial_value=8,
                rules=[
                    {"type": "number", "message": "仅允许设置 8 - 50", "min": 8, "max": 50},
                ],
                field_props={"options": []},
                helm_props={
                    "keys": ["etcd.persistence.size"],
                    "type": "number",
                    "unit": "Gi",
                },
            ),
            # Service 类型
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="service",
                label="数据面Service",
                extra="数据面对外暴露的 Service 类型，网关通常需通过 LoadBalancer 对外提供访问",
                order=4,
                form_item_props={"required": True},
                type="radio",
                initial_value="LoadBalancer",
                rules=[],
                field_props={
                    "options": [
                        {"label": "ClusterIP（集群内访问）", "value": "ClusterIP"},
                        {"label": "LoadBalancer（公网负载均衡）", "value": "LoadBalancer"},
                    ],
                },
                helm_props={
                    "keys": ["dataPlane.service.type"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # ========== 服务环境参数设置 ==========
            # 内置 etcd 副本数
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="etcdReplicas",
                label="etcd节点数",
                extra="内置 etcd 集群节点数，生产环境建议 3（奇数）以保证仲裁多数",
                order=0,
                form_item_props={"required": True},
                type="radio",
                initial_value="3",
                rules=[],
                field_props={
                    "options": [
                        {"label": "1（单节点，仅测试）", "value": "1"},
                        {"label": "3（推荐）", "value": "3"},
                    ],
                },
                helm_props={
                    "keys": ["etcd.replicaCount"],
                    "type": "number",
                    "unit": "",
                },
            ),
            # 启用 Dashboard
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="dashboardEnabled",
                label="启用Dashboard",
                extra="开启 APISIX 可视化管理控制台，提供路由、上游、消费者等资源的图形化管理",
                order=1,
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
                    "keys": ["dashboard.enabled"],
                    "type": "boolean",
                    "unit": "",
                },
            ),
            # Dashboard 用户名
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="dashboardUsername",
                label="Dashboard用户名",
                extra="Dashboard 登录用户名，仅在启用 Dashboard 时生效",
                order=2,
                form_item_props={"required": False},
                type="text",
                initial_value="admin",
                rules=[
                    {"min": 3, "message": "用户名至少 3 个字符"},
                ],
                field_props={"placeholder": "如 admin"},
                helm_props={
                    "keys": ["dashboard.username"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # Dashboard 密码
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="dashboardPassword",
                label="Dashboard密码",
                extra="Dashboard 登录密码，留空则自动生成。仅在启用 Dashboard 时生效",
                order=3,
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
                    "keys": ["dashboard.password"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 启用 Ingress Controller
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="ingressControllerEnabled",
                label="Ingress控制器",
                extra="开启 APISIX Ingress Controller，可通过 K8s Ingress/APISIX CRD 资源声明式管理路由",
                order=4,
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
                    "keys": ["ingressController.enabled"],
                    "type": "boolean",
                    "unit": "",
                },
            ),
            # 数据面 TLS
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="dataPlaneTls",
                label="数据面TLS",
                extra="开启数据面 HTTPS 监听（9443），默认自签证书，可后续替换为正式证书",
                order=5,
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
                    "keys": ["dataPlane.tls.enabled"],
                    "type": "boolean",
                    "unit": "",
                },
            ),
            # 监控指标
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="metricsEnabled",
                label="监控指标导出",
                extra="开启 Prometheus 指标导出，暴露数据面运行指标供监控系统采集",
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
                    "keys": ["dataPlane.metrics.enabled"],
                    "type": "boolean",
                    "unit": "",
                },
            ),
        ],
    )
