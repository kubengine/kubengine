"""Redis Cluster 应用默认配置（基于 bitnami/redis-cluster chart）。"""

from datetime import datetime

from core.orm.app import AppSchema
from core.orm.app_field_config import AppFieldConfigSchema, ConfigTypeEnum


def get_redis_cluster_app(create_time: datetime) -> AppSchema:
    """构建 Redis Cluster 应用配置。

    Args:
        create_time: 应用创建时间

    Returns:
        Redis Cluster 应用 Schema
    """
    return AppSchema(
        name="redis-cluster",
        category=["数据库"],
        description="高可用分布式 Redis 集群（基于 Redis Cluster，自动分片与故障转移）",
        helm_chart="redis-cluster",
        create_time=create_time,
        app_field_configs=[
            # 节点总数
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="nodes",
                label="集群节点数",
                extra="集群总节点数（含副本），master 数量需 >= 3。总数 = master数 + master数*副本数。例如 6 = 3主3从",
                order=0,
                form_item_props={"required": True},
                type="select",
                initial_value="6",
                rules=[],
                field_props={
                    "allowClear": False,
                    "options": [
                        {"label": "6 节点（3主3从）", "value": "6"},
                        {"label": "8 节点（4主4从）", "value": "8"},
                        {"label": "12 节点（6主6从）", "value": "12"},
                    ],
                },
                helm_props={
                    "keys": ["cluster.nodes"],
                    "type": "number",
                    "unit": "",
                },
            ),
            # 每主副本数
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="replicas",
                label="每主副本数",
                extra="每个 master 节点的副本（slave）数量，1 表示每个 master 对应 1 个副本",
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
                    "keys": ["cluster.replicas"],
                    "type": "number",
                    "unit": "",
                },
            ),
            # CPU 配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="cpu",
                label="cpu",
                extra="每个节点的核数，作为分布式缓存单核通常足够，高吞吐场景可提升至双核",
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
                    "keys": ["redis.resources.requests.cpu"],
                    "type": "number",
                    "unit": "",
                },
            ),
            # 内存配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="memory",
                label="内存",
                extra="每个节点的内存大小(单位 Gi)",
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
                    ],
                },
                helm_props={
                    "keys": ["redis.resources.requests.memory"],
                    "type": "number",
                    "unit": "Gi",
                },
            ),
            # 磁盘配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.cluster,
                name="disk",
                label="硬盘大小",
                extra="8Gi - 32Gi, 节点磁盘大小，建议至少为节点内存的三倍",
                order=4,
                form_item_props={"required": True},
                type="number",
                initial_value=8,
                rules=[
                    {"type": "number", "message": "仅允许设置 8 - 32", "min": 8, "max": 32},
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
                    "keys": ["service.type"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 密码配置
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="password",
                label="密码",
                extra="Redis 集群访问密码，留空则自动随机生成；自定义需包含大小写字母、数字，最低 6 位",
                order=0,
                form_item_props={"required": True},
                type="password",
                initial_value="kubengine@redis*CLU",
                rules=[
                    {
                        "type": "string",
                        "message": "密码需包含大小写字母、数字和特殊字符，最低 6 位，最高 20 位",
                        "min": 6,
                        "max": 20,
                    },
                ],
                field_props={"placeholder": "请输入密码", "options": []},
                helm_props={
                    "keys": ["password"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 内存淘汰策略
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="maxmemoryPolicy",
                label="内存淘汰策略",
                extra="达到 maxmemory 时 Redis 选择的淘汰策略。生产环境建议 allkeys-lru（最近最少使用淘汰）",
                order=1,
                form_item_props={"required": False},
                type="select",
                initial_value="allkeys-lru",
                rules=[],
                field_props={
                    "allowClear": False,
                    "options": [
                        {"label": "allkeys-lru（推荐生产）", "value": "allkeys-lru"},
                        {"label": "allkeys-lfu", "value": "allkeys-lfu"},
                        {"label": "volatile-lru", "value": "volatile-lru"},
                        {"label": "volatile-lfu", "value": "volatile-lfu"},
                        {"label": "allkeys-random", "value": "allkeys-random"},
                        {"label": "volatile-random", "value": "volatile-random"},
                        {"label": "volatile-ttl", "value": "volatile-ttl"},
                        {"label": "noeviction（不淘汰，写入报错）", "value": "noeviction"},
                    ],
                },
                helm_props={
                    "keys": ["redis.config.maxmemoryPolicy"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # AOF 持久化
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="appendonly",
                label="AOF持久化",
                extra="开启 AOF 日志，每次写操作追加到文件末尾。数据安全优先选 yes，性能优先选 no",
                order=2,
                form_item_props={"required": False},
                type="radio",
                initial_value="no",
                rules=[],
                field_props={
                    "options": [
                        {"label": "开启", "value": "yes"},
                        {"label": "关闭", "value": "no"},
                    ],
                },
                helm_props={
                    "keys": ["redis.config.appendonly"],
                    "type": "string",
                    "unit": "",
                },
            ),
            # 客户端连接超时
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="timeout",
                label="连接超时(秒)",
                extra="客户端空闲 N 秒后关闭连接。0 表示不关闭，生产环境建议 300 秒",
                order=3,
                form_item_props={"required": False},
                type="number",
                initial_value=300,
                rules=[
                    {"type": "number", "message": "仅允许设置 0 - 86400", "min": 0, "max": 86400},
                ],
                field_props={"options": []},
                helm_props={
                    "keys": ["redis.config.timeout"],
                    "type": "number",
                    "unit": "",
                },
            ),
            # 最大客户端连接数
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="maxclients",
                label="最大连接数",
                extra="Redis 同时处理的最大客户端连接数。默认 10000，连接数过高时建议适当调低",
                order=4,
                form_item_props={"required": False},
                type="number",
                initial_value=10000,
                rules=[
                    {"type": "number", "message": "仅允许设置 1 - 100000", "min": 1, "max": 100000},
                ],
                field_props={"options": []},
                helm_props={
                    "keys": ["redis.config.maxclients"],
                    "type": "number",
                    "unit": "",
                },
            ),
            # 数据库数量
            AppFieldConfigSchema(
                config_type=ConfigTypeEnum.env,
                name="databases",
                label="数据库数量",
                extra="Redis 可用数据库数量（db0 ~ dbN-1），默认 16。多租户场景可按需调整",
                order=5,
                form_item_props={"required": False},
                type="number",
                initial_value=16,
                rules=[
                    {"type": "number", "message": "仅允许设置 1 - 256", "min": 1, "max": 256},
                ],
                field_props={"options": []},
                helm_props={
                    "keys": ["redis.config.databases"],
                    "type": "number",
                    "unit": "",
                },
            ),
        ],
    )
