# KubeEngine

<div align="center">
  <img src="./static/kubeengine-icon.png" alt="KubeEngine Logo" width="150"/>

### 面向麒麟服务器的 Kubernetes 离线部署与管理平台

[![License: Apache 2.0](https://github.com/kubengine/kubengine/blob/main/static/badge/License-Apache%202.0-blue.svg)](LICENSE.txt)
[![Kylin OS](https://github.com/kubengine/kubengine/blob/main/static/badge/Base%20OS-Kylin%20Server%20V11-orange.svg)](http://www.kylinos.cn/)
[![Python 3.11](https://github.com/kubengine/kubengine/blob/main/static/badge/python-3.11-blue.svg)](https://www.python.org/)
[![FastAPI](https://github.com/kubengine/kubengine/blob/main/static/badge/FastAPI-0.121+-green.svg)](https://fastapi.tiangolo.com/)
</div>

KubeEngine 面向 Kylin Server V11（x86_64），提供 Kubernetes 集群离线部署、扩容、组件安装、应用分发和 Web 管理能力。项目由 Python CLI、FastAPI API、内置 Web UI 和基于 pyinfra 的基础设施脚本组成。

## 当前能力

- 单控制面与多控制面高可用部署，支持 Keepalived VIP 和附加控制面节点加入。
- Worker 节点扩容、部署状态续跑/重置、节点 SSH 互信与批量命令执行。
- 自动安装 Chrony、containerd、CNI、Kubernetes、Calico、Helm、MetalLB、Ingress Nginx、Longhorn、Harbor、Metrics Server、Dashboard、Kuboard 和 cert-manager。
- 离线文件分发具备超时、停顿检测和重试机制。
- Harbor 项目初始化、集群镜像归集，以及 Bitnami Chart/镜像的离线导出和导入。
- 应用模板初始化与 Helm 部署；当前内置 Redis、Redis Cluster、MySQL、PostgreSQL、Kafka、RabbitMQ、Elasticsearch、etcd、Nacos、Flink、Flink Operator、APISIX 和 XXL-JOB Admin 等模板。
- Harbor 制品管理、Chart 上传、离线镜像导入任务查询和失败项重试。
- JWT 登录、REST API、WebSocket 任务状态和内置 Web UI。

## 快速开始

### 环境要求

- 管理节点：Kylin Server V11 x86_64，Python 3.11+。
- 目标节点：可通过 SSH 访问；生产部署通常需要 root 权限。
- 离线部署：准备与目标版本匹配的 `offline-deploy` 目录。
- 资源容量、磁盘规划和网络端口应按实际集群规模评估；README 不将最低规格作为生产建议。

### 从源码安装

```bash
git clone https://github.com/kubengine/kubengine.git
cd kubengine

python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

默认读取 `/opt/kubengine/config/application.yaml`；开发环境也可通过环境变量指定配置：

```bash
export KUBEENGINE_CONFIG="$PWD/config/application.yaml"
kubengine --help
kubengine-k8s --help
```

配置文件中的示例 IP、域名、密码哈希和仓库凭据不能直接用于生产环境。完整字段和查找顺序见[配置说明](docs/CONFIGURATION.md)。

### 启动管理服务

```bash
# 首次使用先设置管理员密码，并妥善保存命令输出的 AK/SK
kubengine app set-password

# 初始化或补充内置应用模板；--force 会覆盖已有应用配置
kubengine app init-data

# 启动 API 与内置 Web UI
kubengine app run --host 0.0.0.0 --port 8080
```

启动后可访问：

- Web UI：`http://<管理节点>:8080/`
- Swagger UI：`http://<管理节点>:8080/docs`
- ReDoc：`http://<管理节点>:8080/redoc`
- 健康检查：`http://<管理节点>:8080/api/v1/health`

### 部署 Kubernetes

先修改配置并纳管所有节点，再校验和执行部署：

```bash
kubengine cluster config \
  --hosts 192.168.1.10,192.168.1.11 \
  --hostname-map 192.168.1.10:master01,192.168.1.11:worker01 \
  --username root

kubengine cluster show
kubengine-k8s config --validate
kubengine-k8s deploy --deploy-src /root/offline-deploy -vv
```

部署完成后按需初始化 Harbor、归集节点镜像并导入应用离线包：

```bash
kubengine-k8s init-harbor
kubengine-k8s push-images
kubengine-k8s sync-bitnami import-dir /root/offline-deploy/bitnami-bundles/
```

生产环境的节点准备、HA 配置、Longhorn 数据盘和 systemd 托管步骤见[部署文档](docs/部署文档.md)。如果不安装或不调用 KubeEngine，请参考[手动部署文档](docs/手动部署文档.md)。

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `kubengine app run` | 启动 API 和 Web UI |
| `kubengine app set-password` | 修改管理员密码并生成/轮换 AK/SK |
| `kubengine app init-data` | 初始化内置应用模板 |
| `kubengine cluster config/show/exec` | 纳管节点、查看配置、批量执行命令 |
| `kubengine cluster disable-firewalld` | 在集群节点关闭 firewalld |
| `kubengine image ...` | 构建、查看和清理镜像 |
| `kubengine-k8s deploy` | 部署或从已保存状态继续部署集群 |
| `kubengine-k8s scale` | 为已有集群添加 Worker 节点 |
| `kubengine-k8s reset-state` | 重置本地部署状态 |
| `kubengine-k8s init-harbor` | 初始化 Harbor 项目 |
| `kubengine-k8s push-images` | 将各节点的现有镜像归集至 Harbor |
| `kubengine-k8s sync-bitnami ...` | 导出、拉取和导入离线 Chart/镜像包 |

所有选项以命令自身的 `--help` 为准，完整示例见 [CLI 文档](docs/CLI.md)。

## 文档导航

| 文档 | 内容 |
| --- | --- |
| [部署文档](docs/部署文档.md) | 使用 KubeEngine 完成单 Master/HA 离线部署 |
| [手动部署文档](docs/手动部署文档.md) | 不依赖 KubeEngine 的手工部署流程 |
| [CLI 文档](docs/CLI.md) | 平台、集群、镜像和 Kubernetes 命令 |
| [配置说明](docs/CONFIGURATION.md) | 配置路径、字段和生产建议 |
| [API 文档](docs/API.md) | 鉴权、响应格式和当前路由概览 |
| [NFS 存储对接教程](docs/NFS存储对接教程.md) | 通过 external provisioner 提供 NFS StorageClass |
| [新增应用指南](docs/如何新增一个应用.md) | 添加应用模板与部署配置 |
| [Image Builders 指南](docs/IMAGE_BUILDERS.md) | 开发并注册自定义镜像构建器 |
| [RPM 构建](docs/RPM_BUILD.md) | 构建 RPM 安装包 |
| [pip 安装](docs/PYPI_INSTALL.md) | wheel/源码包安装与发布 |

## 项目结构

```text
kubengine/
├── config/                 # 主配置、证书和部署状态
├── docs/                   # 使用、部署和开发文档
├── scripts/                # pip/RPM/Cython 构建脚本
├── src/
│   ├── builder/            # 可扩展镜像构建器
│   ├── cli/                # kubengine 与 kubengine-k8s
│   ├── core/               # 配置、ORM、SSH、客户端和通用能力
│   ├── infra/              # pyinfra 集群与组件部署脚本
│   └── web/                # FastAPI、API 路由和 Web 静态产物
├── pyproject.toml          # Python 包、依赖和入口点
├── setup.py                # 兼容 RPM/Cython 的构建配置
└── kubengine.spec          # RPM spec
```

运行数据默认写入 `config/sqlite.db`，日志默认写入 `logs/`。这两类运行时文件不应作为可移植配置或发布产物使用。

## 开发

```bash
python -m pip install -e ".[dev]"
pytest
black src tests
isort src tests
mypy src
```

仓库当前可能不包含 `tests/` 目录；新增功能时建议同时补充对应测试。项目要求 Python 3.11+，包版本以 `pyproject.toml` 为准。

## 许可证

项目采用 [Apache License 2.0](LICENSE.txt)。
