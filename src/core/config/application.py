"""Application configuration constants class.

This module defines the Application class which holds global configuration
constants injected from configuration files using the map_config_to_class decorator.
"""

from typing import ClassVar, List
from .inject import config_class, map_config_to_class


@config_class(
    ROOT_DIR="tls.root_dir",
    CA_COUNTRY_CODE="tls.ca_country_code",
    CA_STATE_NAME="tls.ca_state_name",
    CA_LOCALITY_NAME="tls.ca_locality_name",
    CA_ORGANIZATION_NAME="tls.ca_organization_name",
    CA_COMMON_NAME="tls.ca_common_name",
    CA_EMAIL_ADDRESS="tls.ca_email_address",
    CA_PASSWORD="tls.ca_password",
    CA_VALID_DAYS="tls.ca_valid_days",
    CA_KEY_LENGTH="tls.ca_key_length"
)
class TLSConfig:
    """TLS 证书配置类

    包含TLS证书相关的所有配置参数，包括证书路径、CA信息和生成参数。
    """
    # 证书路径配置
    ROOT_DIR: ClassVar[str] = "/opt/kubengine/config/certs"
    # 动态计算的路径，通过属性方法实现

    @property
    def SERVER_CRT(self) -> str:
        return f"{self.ROOT_DIR}/server/server.crt"

    @property
    def SERVER_KEY(self) -> str:
        return f"{self.ROOT_DIR}/server/server.key"

    @property
    def CA_CRT(self) -> str:
        return f"{self.ROOT_DIR}/ca/ca.crt"

    @property
    def CA_KEY(self) -> str:
        return f"{self.ROOT_DIR}/ca/ca.key"

    # CA证书信息配置
    CA_COUNTRY_CODE: ClassVar[str] = "CN"
    CA_STATE_NAME: ClassVar[str] = "Beijing"
    CA_LOCALITY_NAME: ClassVar[str] = "Beijing"
    CA_ORGANIZATION_NAME: ClassVar[str] = "kubengine"
    CA_COMMON_NAME: ClassVar[str] = "kubengine root ca"
    CA_EMAIL_ADDRESS: ClassVar[str] = "ssl@kubengine.io"

    # CA生成参数配置
    CA_PASSWORD: ClassVar[str] = "kubengine"
    CA_VALID_DAYS: ClassVar[int] = 3650
    CA_KEY_LENGTH: ClassVar[int] = 4096


@config_class(
    LEVEL="logger.level",
    CONSOLE_OUTPUT="logger.console",
    LOG_FORMAT="logger.format",
    DATE_FORMAT="logger.date_format",
    ROTATE_ENABLE="logger.rotate.enable",
    ROTATE_WHEN="logger.rotate.when",
    ROTATE_BACKUP_COUNT="logger.rotate.backup_count",
)
class LoggerConfig:
    """日志配置数据类，默认值适配大多数场景"""
    LEVEL: ClassVar[str] = "INFO"
    FORMAT: ClassVar[str] = "%(asctime)s - %(name)s - %(levelname)s - %(process)d - %(filename)s:%(lineno)d - %(message)s"
    DATE_FORMAT: ClassVar[str] = "%Y-%m-%d %H:%M:%S"
    # 日志轮转配置
    ROTATE_ENABLE: ClassVar[bool] = True  # 是否开启日志轮转
    ROTATE_WHEN: ClassVar[str] = "D"  # 按天轮转（可选：S/Min/H/D/W0-W6）
    ROTATE_BACKUP_COUNT: ClassVar[int] = 7  # 保留7天日志
    # 第三方库日志级别管控（降噪）
    THIRD_PARTY_LOG_LEVELS: dict[str, str] = {
        "uvicorn": "WARNING",
        "uvicorn.access": "WARNING",
        "click": "INFO",
        "fastapi": "INFO"
    }


@config_class(
    MASTER_SCHEDULABLE="kubernetes.master.schedulable",
    MASTER_IP="kubernetes.master.ip",
    WORKER_IPS="kubernetes.worker.ips",
    SERVICE_CIDR="kubernetes.cidr.service",
    POD_CIDR="kubernetes.cidr.pod",
    LOADBALANCER_IP_POOLS="kubernetes.loadbalancer.ip-pools",
    NAMESERVER="kubernetes.nameserver"
)
class KubernetesConfig:
    """Kubernetes 配置类

    包含Kubernetes集群相关的配置参数。
    """
    MASTER_SCHEDULABLE: ClassVar[bool] = False
    MASTER_IP: ClassVar[str] = ""
    WORKER_IPS: ClassVar[List[str]] = []
    SERVICE_CIDR: ClassVar[str] = "10.96.0.0/16"
    POD_CIDR: ClassVar[str] = "10.97.0.0/16"
    LOADBALANCER_IP_POOLS: ClassVar[List[str]] = []
    NAMESERVER: ClassVar[List[str]] = ["8.8.8.8"]
    DEPLOY_SRC: ClassVar[str] = "/root/offline-deploy"


@config_class(
    ALGORITHM="auth.jwt.algorithm",
    TOKEN_EXPIRE_MINUTES="auth.jwt.token.expire_minutes",
    TOKEN_RENEW_THRESHOLD_MINUTES="auth.jwt.token.renew_threshold_minutes",
    USERS_ADMIN_PASSWORD_HASH="auth.users.admin.password_hash",
    USERS_ADMIN_AK="auth.users.admin.ak",
    USERS_ADMIN_SK_HASH="auth.users.admin.sk_hash"
)
class AuthenticationConfig:
    """认证配置类

    包含JWT令牌、用户认证相关的配置参数。
    """
    # JWT配置
    ALGORITHM: ClassVar[str] = "HS256"

    # 令牌配置
    TOKEN_EXPIRE_MINUTES: ClassVar[int] = 30
    TOKEN_RENEW_THRESHOLD_MINUTES: ClassVar[int] = 5

    # 管理员用户配置
    USERS_ADMIN_PASSWORD_HASH: ClassVar[str] = "$2b$12$TofW8liw7vV/1cR7QkauvOceZN4syUvwYPrKsR5BezGzKkiRykYK."
    USERS_ADMIN_AK: ClassVar[str] = "AK2A085428"
    USERS_ADMIN_SK_HASH: ClassVar[str] = "$2b$12$XnH3EqjMQ1Kc8.qEoPtoueT8pT3/cdbf.IxvYpZHtt2NdHBu881iW"


@config_class(
    USERNAME="registry.username",
    PASSWORD="registry.password",
)
class RegistryConfig:
    """镜像仓库配置类

    包含镜像仓库用户名、密码等相关的配置参数。
    """
    USERNAME: ClassVar[str] = "admin"
    PASSWORD: ClassVar[str] = "Harbor@123"


@map_config_to_class(
    ROOT_DIR="root_dir",
    DOMAIN="domain",
    TLS_CONFIG="tls",
    K8S_CONFIG="kubernetes",
    LOGGER_CONFIG="logger",
    AUTH="auth",
    REGISTRY="registry"
)
class Application:
    """Global constants class with configuration injection.

    This class holds application-wide configuration constants that are
    dynamically injected from configuration files at runtime.
    """

    # Class variables with explicit type annotations
    ROOT_DIR: ClassVar[str] = "/opt/kubengine"
    DOMAIN: ClassVar[str] = "kubengine.io"
    TLS_CONFIG: TLSConfig
    K8S_CONFIG: KubernetesConfig
    LOGGER_CONFIG: LoggerConfig
    AUTH: AuthenticationConfig
    REGISTRY: RegistryConfig
