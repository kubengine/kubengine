"""应用默认配置模板。

按应用拆分，每个文件定义一个应用的 AppSchema（含字段配置），
通过 get_default_apps() 统一聚合返回。
"""

from datetime import datetime

from core.orm.app import AppSchema
from core.app_defaults.redis import get_redis_app
from core.app_defaults.redis_cluster import get_redis_cluster_app
from core.app_defaults.kafka import get_kafka_app
from core.app_defaults.rabbitmq import get_rabbitmq_app
from core.app_defaults.flink import get_flink_app
from core.app_defaults.elasticsearch import get_elasticsearch_app
from core.app_defaults.etcd import get_etcd_app
from core.app_defaults.apisix import get_apisix_app
from core.app_defaults.nacos import get_nacos_app
from core.app_defaults.mysql import get_mysql_app
from core.app_defaults.xxl_job_admin import get_xxl_job_admin_app
# from core.app_defaults.flink_operator import get_flink_operator_app


def get_default_apps() -> list[AppSchema]:
    """获取所有默认应用配置。

    Returns:
        应用 Schema 列表
    """
    now = datetime.now()
    return [
        get_redis_app(now),
        get_redis_cluster_app(now),
        get_kafka_app(now),
        get_rabbitmq_app(now),
        get_flink_app(now),
        get_elasticsearch_app(now),
        get_etcd_app(now),
        get_apisix_app(now),
        get_nacos_app(now),
        get_mysql_app(now),
        get_xxl_job_admin_app(now),
        # get_flink_operator_app(now),  # 默认不注册flink-operator
    ]
