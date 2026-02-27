"""
KubeEngine CLI 模块

本模块包含两个独立的 CLI：

1. kubengine - 应用管理和基础设施管理
   - app: 应用管理
   - cluster: 集群管理（主机配置、SSH 互信）
   - image: 镜像构建工具（用于构建自定义容器镜像）

2. kubengine_k8s - Kubernetes 集群部署
   - deploy: 自动化 Kubernetes 集群部署
   - config: 生成 Kubernetes 配置文件
   - join: 节点加入集群
   - etcd: 管理 etcd 集群

这样的分离解决了 gevent monkey patch 与 uvicorn asyncio 事件循环冲突的问题。
"""

__version__ = "0.1.0"
