"""Elasticsearch 应用默认配置（基于 bitnami/elasticsearch chart）。"""

from datetime import datetime

from core.orm.app import AppSchema
from core.orm.app_field_config import AppFieldConfigSchema, ConfigTypeEnum


def get_elasticsearch_app(create_time: datetime) -> AppSchema:
    """构建 Elasticsearch 应用配置。

    Args:
        create_time: 应用创建时间

    Returns:
        Elasticsearch 应用 Schema
    """
    return AppSchema(
        name="elasticsearch",
        category=["中间件"],
        description="分布式搜索与分析引擎，适用于全文检索、日志监控与实时分析场景",
        helm_chart="elasticsearch",
        create_time=create_time,
        app_field_configs=[
            # ========== 集群相关配置 ==========
            # 数据节点数
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="dataNodes",
                label="数据节点数",
                extra="承载数据读写的 data 节点数量，建议与数据分片数匹配，多节点可提升吞吐与可用性",
                order=0,
                form_item_props={"required": True},
                type="radio",
                initial_value="2",
                rules=[],
                field_props={
                    "options": [
                        {"label": "1（单节点，仅测试）", "value": "1"},
                        {"label": "2（默认）", "value": "2"},
                        {"label": "3（推荐）", "value": "3"},
                        {"label": "5", "value": "5"},
                    ],
                },
                helm_props={
                    "keys": ["data.replicaCount"],
                    "type": "number",
                    "unit": "",
                },
            ),
            # CPU 配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="cpu",
                label="cpu",
                extra="每个节点的核数，所有节点（master/data/coordinating/ingest）统一应用。搜索/聚合密集场景建议 2 核及以上",
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
                        "master.resources.requests.cpu",
                        "master.resources.limits.cpu",
                        "data.resources.requests.cpu",
                        "data.resources.limits.cpu",
                        "coordinating.resources.requests.cpu",
                        "coordinating.resources.limits.cpu",
                        "ingest.resources.requests.cpu",
                        "ingest.resources.limits.cpu",
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
                extra="每个节点的内存大小(单位 Gi)，所有节点统一应用。建议 heap 为内存的 50%，设置 limits 以确保 JVM 正确分配直接内存",
                order=2,
                form_item_props={"required": True},
                type="radio",
                initial_value="2",
                rules=[],
                field_props={
                    "options": [
                        {"label": "2", "value": "2"},
                        {"label": "4", "value": "4"},
                        {"label": "8", "value": "8"},
                        {"label": "16", "value": "16"},
                    ],
                },
                helm_props={
                    "keys": [
                        "master.resources.requests.memory",
                        "master.resources.limits.memory",
                        "data.resources.requests.memory",
                        "data.resources.limits.memory",
                        "coordinating.resources.requests.memory",
                        "coordinating.resources.limits.memory",
                        "ingest.resources.requests.memory",
                        "ingest.resources.limits.memory",
                    ],
                    "type": "number",
                    "unit": "Gi",
                },
            ),
            # 磁盘配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="disk",
                label="硬盘大小",
                extra="8Gi - 200Gi, 节点数据存储磁盘大小，需根据索引数据量与保留策略评估",
                order=3,
                form_item_props={"required": True},
                type="number",
                initial_value=8,
                rules=[
                    {"type": "number", "message": "仅允许设置 8 - 200", "min": 8, "max": 200},
                ],
                field_props={"options": []},
                helm_props={
                    "keys": [
                        "master.persistence.size",
                        "data.persistence.size",
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
            # 集群名称
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="clusterName",
                label="集群名称",
                extra="Elasticsearch 集群名称，同一集群内所有节点必须一致",
                order=0,
                form_item_props={"required": True},
                type="text",
                initial_value="elastic",
                rules=[
                    {"required": True, "message": "请输入集群名称"},
                    {"min": 2, "message": "集群名称至少 2 个字符"},
                ],
                field_props={"placeholder": "如 elastic"},
                helm_props={
                    "keys": ["clusterName"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 安全认证
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="securityEnabled",
                label="安全认证",
                extra="开启 X-Pack Security（含密码认证与 TLS）。开启后自动生成 TLS 证书，需同时配置密码",
                order=1,
                form_item_props={"required": True},
                type="radio",
                initial_value="false",
                rules=[],
                field_props={
                    "options": [
                        {"label": "关闭", "value": "false"},
                        {"label": "开启（推荐生产）", "value": "true"},
                    ],
                },
                helm_props={
                    "keys": ["security.enabled", "security.tls.autoGenerated"],
                    "type": "boolean",
                    "unit": "",
                },
            ),
            # elastic 用户密码
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="elasticPassword",
                label="elastic用户密码",
                extra="超级管理员 elastic 账号密码，留空则自动生成",
                order=2,
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
                field_props={"placeholder": "开启安全认证后请输入密码"},
                helm_props={
                    "keys": ["security.elasticPassword"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 数据节点 JVM 堆内存
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="dataHeapSize",
                label="数据节点JVM堆内存",
                extra="data 节点 JVM 堆大小，建议为节点内存的 50%。需与所选节点规格匹配",
                order=3,
                form_item_props={"required": True},
                type="select",
                initial_value="1024m",
                rules=[],
                field_props={
                    "allowClear": False,
                    "options": [
                        {"label": "512m", "value": "512m"},
                        {"label": "1024m（默认）", "value": "1024m"},
                        {"label": "2048m", "value": "2048m"},
                        {"label": "4096m", "value": "4096m"},
                        {"label": "8192m", "value": "8192m"},
                    ],
                },
                helm_props={
                    "keys": ["data.heapSize"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 预装插件
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="plugins",
                label="预装插件",
                extra="初始化时安装的插件列表，多个插件用逗号分隔，如 analysis-ik,ingest-attachment",
                order=4,
                form_item_props={"required": False},
                type="text",
                initial_value="",
                rules=[],
                field_props={"placeholder": "如 analysis-ik,ingest-attachment"},
                helm_props={
                    "keys": ["plugins"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 监控指标
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="metricsEnabled",
                label="监控指标导出",
                extra="开启 Prometheus Exporter，暴露集群运行指标供监控系统采集",
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
        ],
    )
