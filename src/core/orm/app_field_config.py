import enum
from typing import Any, List, Optional

from pydantic import BaseModel
from core.orm.engine import Base
from sqlalchemy import JSON, Column, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import relationship


class ConfigTypeEnum(enum.Enum):
    '''
    description: 枚举类型：区分配置项归属（cluster / env）
    '''
    cluster = "cluster"  # 集群相关配置
    env = "env"          # 环境相关配置（如密码）


class AppFieldConfig(Base):
    """应用字段配置"""
    __tablename__ = "app_field_config"
    field_id = Column(Integer, primary_key=True, index=True)
    app_id = Column(
        Integer,
        ForeignKey("app.app_id", ondelete="CASCADE"),  # 数据库级联删除
        nullable=False
    )
    app = relationship(
        "App",
        back_populates="app_field_configs"  # 指向 App 中的 app_field_configs 字段
    )
    # 配置项归属类型（cluster/env）
    config_type = Column(Enum(ConfigTypeEnum), nullable=False,
                         comment="配置项类型：cluster-集群配置，env-环境配置")
    # 配置项名称（如architecture、cpu、password）
    name = Column(String(50), nullable=False, comment="配置项名称")
    # 配置项显示标签（如部署模式、cpu、密码）
    label = Column(String(50), nullable=False, comment="配置项显示标签")
    # 配置项额外说明
    extra = Column(String(500), nullable=True, comment="配置项额外说明信息")
    # 排序字段，默认值为0
    order = Column(Integer, nullable=False, default=0, comment="排序字段")
    # 表单项属性（如required: true），JSON类型存储字典
    form_item_props = Column(JSON, nullable=True, comment="表单项属性配置")
    # 表单控件类型（radio、number、password等）
    type = Column(String(20), nullable=False, comment="表单控件类型")
    # 初始值（可能是字符串、数字等，用JSON兼容不同类型）
    initial_value = Column(JSON, nullable=True, comment="配置项初始值")
    # 校验规则列表（JSON数组）
    rules = Column(JSON, nullable=True, comment="表单校验规则")
    # 字段属性（如placeholder），JSON类型存储字典（可选字段）
    field_props = Column(JSON, nullable=True, comment="表单字段属性配置")
    # helm 关联配置属性，JSON类型存储字典（可选字段）
    helm_props = Column(JSON, nullable=True, comment="表单字段属性配置")


class AppFieldConfigSchema(BaseModel):
    field_id: Optional[int] = None
    app_id: Optional[int] = None
    config_type: Optional[ConfigTypeEnum] = None  # 自动序列化枚举为字符串
    name: Optional[str] = None
    label: Optional[str] = None
    type: Optional[str] = None
    extra: Optional[str] = None
    order: Optional[int] = None
    form_item_props: Optional[dict[str, Any]] = None
    initial_value: Optional[Any] = None
    rules: Optional[List[dict[str, Any]]] = None
    field_props: Optional[dict[str, Any]] = None
    helm_props: Optional[dict[str, Any]] = None

    # 核心：开启ORM模式，支持从SQLAlchemy实例读取属性
    class Config:
        from_attributes = True  # Pydantic v2 关键配置
        arbitrary_types_allowed = True  # 可选：允许序列化JSON/Enum等特殊类型
