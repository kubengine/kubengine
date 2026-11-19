# CLI 命令文档

KubeEngine 提供两种命令行使用方式：

1. **原生 Python 方式**：适合开发调试和源码运行
2. **kubengine 命令方式**：适合生产环境和 RPM 安装后使用

---

## 应用管理

### 启动 API 服务

#### 原生 Python 方式

```bash
python -m cli.app run [OPTIONS]
```

#### kubengine 命令方式

```bash
kubengine app run [OPTIONS]
```

**选项：**
| 选项 | 说明 | 默认值 |
|------|------|--------|
| `--host TEXT` | 监听的主机地址 | `0.0.0.0` |
| `--port INTEGER` | 监听的端口号 | `8080` |
| `--workers INTEGER` | 工作进程数 | `2` |

**示例：**
```bash
# 原生 Python 方式
python -m cli.app run --host 0.0.0.0 --port 8080 --workers 4

# kubengine 命令方式
kubengine app run --host 0.0.0.0 --port 8080 --workers 4
```

### 启动统一任务 Worker

应用、镜像导入及 Deployment 运维由独立持久化 worker 执行，生产环境交给 systemd 托管：

```bash
kubengine app task-worker --concurrency 1

# 兼容命令，执行同一个 worker
kubengine app image-worker --concurrency 1
```

`--concurrency` 控制同时处理的镜像包数量，默认为 `1`；任务通过数据库租约、
心跳和过期接管机制在 worker 重启后恢复。原 `kubengine-image-worker.service`
名称保持不变。各类任务的恢复边界见 [API 说明](API.md#任务恢复与清理)。

---

### 设置管理员密码

#### 原生 Python 方式

```bash
python -m cli.app set-password
```

#### kubengine 命令方式

```bash
kubengine app set-password
```

首次执行时会幂等迁移配置中的用户到数据库，之后只修改数据库里已有、启用且角色为
平台管理员的 `admin` 密码，并撤销该账号全部登录会话。不修改配置文件中的旧哈希，
也不重新启用已停用的账号。用户名不存在、被停用或已降权时使用其他管理员的用户
管理 API 处理。旧 AK/SK 认证已关闭，不再生成密钥对。

迁移、备份与回滚约定见[用户权限与审计设计](用户权限与审计设计.md)。

---

### 初始化默认应用数据

#### 原生 Python 方式

```bash
python -m cli.app init-data [OPTIONS]
```

#### kubengine 命令方式

```bash
kubengine app init-data [OPTIONS]
```

**选项：**
| 选项 | 说明 |
|------|------|
| `--force` | 强制覆盖已存在的应用数据 |

---

## 集群管理

### 配置集群（主机名 + SSH 互信）

#### 原生 Python 方式

```bash
python -m cli.cluster config [OPTIONS]
```

#### kubengine 命令方式

```bash
kubengine cluster config [OPTIONS]
```

**选项：**
| 选项 | 说明 | 默认值 |
|------|------|--------|
| `--hosts TEXT` | 集群节点 IP 列表，逗号分隔 | - |
| `--hostname-map TEXT` | IP:主机名 映射，逗号分隔 | - |
| `--username TEXT` | SSH 用户名 | `root` |
| `--password TEXT` | SSH 密码 | - |
| `--key-file TEXT` | SSH 私钥文件路径 | `~/.ssh/id_rsa` |
| `--skip-verify` | 跳过 SSH 互信验证 | - |

**示例：**
```bash
# 原生 Python 方式
python -m cli.cluster config \
  --hosts 172.31.57.23,172.31.57.22,172.31.57.21 \
  --hostname-map 172.31.57.23:kubengine3,172.31.57.22:kubengine2,172.31.57.21:kubengine1 \
  --username root

# kubengine 命令方式
kubengine cluster config \
  --hosts 172.31.57.23,172.31.57.22,172.31.57.21 \
  --hostname-map 172.31.57.23:kubengine3,172.31.57.22:kubengine2,172.31.57.21:kubengine1 \
  --username root
```

---

### 显示集群配置

#### 原生 Python 方式

```bash
python -m cli.cluster show
```

#### kubengine 命令方式

```bash
kubengine cluster show
```

---

### 在集群节点上执行命令

#### 原生 Python 方式

```bash
python -m cli.cluster exec [OPTIONS] "COMMAND"
```

#### kubengine 命令方式

```bash
kubengine cluster exec [OPTIONS] "COMMAND"
```

**选项：**
| 选项 | 说明 |
|------|------|
| `--hosts TEXT` | 节点 IP 列表 |
| `--username TEXT` | SSH 用户名 |
| `--password TEXT` | SSH 密码 |
| `--key-file TEXT` | SSH 私钥文件路径 |

**示例：**
```bash
# 在所有节点执行命令
kubengine cluster exec "hostname" --hosts 172.31.57.23,172.31.57.22
```

---

### 禁用防火墙

#### 原生 Python 方式

```bash
python -m cli.cluster disable-firewalld [OPTIONS]
```

#### kubengine 命令方式

```bash
kubengine cluster disable-firewalld [OPTIONS]
```

---

## 镜像构建

### 构建单个应用版本

#### 原生 Python 方式

```bash
python -m cli.image build [OPTIONS]
```

#### kubengine 命令方式

```bash
kubengine image build [OPTIONS]
```

**选项：**
| 选项 | 说明 |
|------|------|
| `--app TEXT` | 应用名称（必需） |
| `--version TEXT` | 版本号 |
| `--push` | 构建后推送镜像 |
| `--registry TEXT` | 目标仓库地址 |

**示例：**
```bash
# 构建 Redis 镜像
kubengine image build --app redis --version 7.0.15

# 构建并推送镜像
kubengine image build --app redis --version 7.0.15 --push --registry harbor.example.com
```

---

### 构建多个版本

#### 原生 Python 方式

```bash
python -m cli.image build-multi [OPTIONS]
```

#### kubengine 命令方式

```bash
kubengine image build-multi [OPTIONS]
```

---

### 构建所有版本

#### 原生 Python 方式

```bash
python -m cli.image build-all [OPTIONS]
```

#### kubengine 命令方式

```bash
kubengine image build-all [OPTIONS]
```

---

### 列出支持的应用

#### 原生 Python 方式

```bash
python -m cli.image list-apps
```

#### kubengine 命令方式

```bash
kubengine image list-apps
```

---

### 显示应用信息

#### 原生 Python 方式

```bash
python -m cli.image info [OPTIONS]
```

#### kubengine 命令方式

```bash
kubengine image info [OPTIONS]
```

**选项：**
| 选项 | 说明 |
|------|------|
| `--app TEXT` | 应用名称（必需） |

---

### 清理构建产物

#### 原生 Python 方式

```bash
python -m cli.image clean [OPTIONS]
```

#### kubengine 命令方式

```bash
kubengine image clean [OPTIONS]
```

---

## Kubernetes 部署

### 使用 kubengine-k8s 独立命令（推荐）

kubengine-k8s 是专门用于 Kubernetes 部署的独立命令行工具，提供更强大的功能和更好的用户体验：

```bash
kubengine-k8s --help
```

**可用命令：**
| 命令 | 说明 |
|------|------|
| `deploy` | 部署 Kubernetes 集群 |
| `config` | 配置管理（显示/验证） |
| `reset-state` | 重置部署状态 |
| `scale` | 集群扩容（添加 Worker 节点） |
| `init-harbor` | 初始化 Harbor 项目 |
| `push-images` | 推送集群节点镜像到 Harbor |
| `sync-bitnami` | 离线同步 Bitnami Charts 与镜像 |

#### 部署 Kubernetes 集群

```bash
kubengine-k8s deploy [OPTIONS]
```

**选项：**
| 选项 | 说明 |
|------|------|
| `--deploy-src TEXT` | 离线部署文件根目录 | `/root/offline-deploy` |
| `-v, --verbose` | 日志详细级别：-v/-vv/-vvv | - |
| `--show-config` | 显示当前配置（不执行部署） | - |

**示例：**
```bash
# 使用默认配置部署
kubengine-k8s deploy

# 指定离线部署目录
kubengine-k8s deploy --deploy-src /path/to/offline-files

# 显示配置但不执行部署
kubengine-k8s deploy --show-config

# 详细输出日志
kubengine-k8s deploy -vvv
```

---

#### 检查点与失败续跑

`deploy`、`scale`、`reset-state` 共用安装目录内的互斥锁。
`${root_dir}/config/.k8s_deployment_state.json` 使用版本 2，记录组件 owner、attempt、
阶段和输入摘要，以 0600 临时文件、fsync 和原子替换保存；损坏或不支持的状态会报错。

组件输入摘要覆盖脚本、共享 helper、执行包装器、SSH/配置代码、部署参数，以及声明的
模板、Chart 和离线包。只有已成功且输入未变化的组件才跳过，执行中输入变化拒绝写入
成功检查点；新增输入须维护 `core/deployment_inputs.py`。检查点不负责持续检测外部漂移。

已有控制面且状态缺失或配置变化时，不会自动重新 bootstrap。Worker 扩容改变配置后，
旧 deploy 检查点不能证明新拓扑完成；不要删除状态文件绕过保护。扩容节点已加入但配置
写回失败时，修复存储问题后可重复相同 scale 请求补齐。主机信任与初始化边界见
[SSH 与发布说明](security-bootstrap-release.md)。

#### 显示和验证配置

```bash
kubengine-k8s config [OPTIONS]
```

**选项：**
| 选项 | 说明 |
|------|------|
| `--validate` | 验证配置 |
| `--show` | 显示配置 |

**示例：**
```bash
# 显示当前配置
kubengine-k8s config --show

# 验证配置
kubengine-k8s config --validate
```

---

#### 重置部署状态

```bash
kubengine-k8s reset-state [OPTIONS]
```

**选项：**
| 选项 | 说明 |
|------|------|
| `--force` | 强制重置状态 |

**示例：**
```bash
kubengine-k8s reset-state --force
```

---

#### 集群扩容

```bash
kubengine-k8s scale [OPTIONS]
```

**选项：**
| 选项 | 说明 |
|------|------|
| `--worker-ip TEXT` | 新 Worker 节点 IP（可多次指定，必需） |
| `--deploy-src TEXT` | 离线部署文件根目录 | `/root/offline-deploy` |
| `--dry-run` | 仅预览，不执行 |
| `-v, --verbose` | 日志详细级别 |

**示例：**
```bash
# 扩容单个节点
kubengine-k8s scale --worker-ip 172.31.57.30

# 扩容多个节点并预览
kubengine-k8s scale --worker-ip 172.31.57.30 --worker-ip 172.31.57.31 --dry-run
```

---

#### 初始化 Harbor 项目

```bash
kubengine-k8s init-harbor [OPTIONS]
```

**选项：**
| 选项 | 说明 | 默认值 |
|------|------|--------|
| `--timeout INTEGER` | 等待 Harbor 就绪最大时间（秒） | `600` |
| `--interval INTEGER` | 轮询间隔（秒） | `10` |

---

#### 推送集群镜像到 Harbor

```bash
kubengine-k8s push-images [OPTIONS]
```

收集集群所有节点 containerd 中的镜像，去重后推送到 Harbor。

**选项：**
| 选项 | 说明 |
|------|------|
| `--dry-run` | 仅展示待推送镜像，不执行 |
| `--image-timeout INTEGER` | 单个镜像推送超时（秒），默认 `600` |

---

#### 离线同步 Bitnami Charts

`sync-bitnami` 命令组用于在互联网环境导出 Bitnami Charts 与镜像，在内网环境导入 Harbor。

**子命令：**
| 子命令 | 说明 |
|--------|------|
| `export` | 阶段一：打包 chart 并提取镜像清单 |
| `pull-images` | 阶段一.五：通过国内镜像源拉取镜像并导出 tar |
| `import` | 阶段二：导入单个 chart 包与镜像到 Harbor |
| `import-dir` | 阶段二：批量扫描目录导入所有 bundle |

```bash
# 批量导入所有应用（推荐）
kubengine-k8s sync-bitnami import-dir /root/offline-deploy/bitnami-bundles/

# 导入单个应用
kubengine-k8s sync-bitnami import redis-23.1.1.tgz --images-tar redis-images.images.tar

# 预览批量导入
kubengine-k8s sync-bitnami import-dir /root/offline-deploy/bitnami-bundles/ --dry-run
```

---

### 原生 Python 方式

如果需要直接通过 Python 模块执行：

```bash
python -m cli.k8s deploy [OPTIONS]
python -m cli.k8s config [OPTIONS]
python -m cli.k8s reset-state [OPTIONS]
```

---

## 镜像仓库操作

### 拉取镜像

#### 原生 Python 方式

```bash
python -m cli.image ctr pull [OPTIONS]
```

#### kubengine 命令方式

```bash
kubengine image ctr pull [OPTIONS]
```

---

### 推送镜像

#### 原生 Python 方式

```bash
python -m cli.image ctr push [OPTIONS]
```

#### kubengine 命令方式

```bash
kubengine image ctr push [OPTIONS]
```

---

### 添加仓库代理

#### 原生 Python 方式

```bash
python -m cli.image ctr add-proxy [OPTIONS]
```

#### kubengine 命令方式

```bash
kubengine image ctr add-proxy [OPTIONS]
```

---

### 列出代理配置

#### 原生 Python 方式

```bash
python -m cli.image ctr list-proxy
```

#### kubengine 命令方式

```bash
kubengine image ctr list-proxy
```

---

## 命令方式对比

| 特性 | 原生 Python 方式 | kubengine 命令方式 |
|------|------------------|-------------------|
| 使用场景 | 开发调试、源码运行 | 生产环境、RPM 安装 |
| 命令格式 | `python -m <模块>` | `kubengine <子命令>` |
| 依赖要求 | Python 环境 + 源码 | 安装到系统/虚拟环境 |
| 便捷性 | 需指定完整模块路径 | 简洁易记 |
| 示例 | `python -m cli.app run` | `kubengine app run` |

---

## 常见使用场景

### 开发环境快速启动

```bash
# 进入项目目录
cd kubengine

# 创建虚拟环境
python3.11 -m venv venv
source venv/bin/activate

# 安装依赖
pip install -e .

# 启动服务
python -m cli.app run
```

### 生产环境（RPM 安装后）

```bash
# RPM 安装后，kubengine 命令已可用
# 启动服务
systemctl start kubengine-api

# 或手动启动
kubengine app run --host 0.0.0.0 --port 8080
```

### 集群初始化完整流程

```bash
# 1. 配置集群
kubengine cluster config \
  --hosts 172.31.57.23,172.31.57.22,172.31.57.21 \
  --hostname-map 172.31.57.23:kubengine3,172.31.57.22:kubengine2,172.31.57.21:kubengine1

# 2. 禁用防火墙
kubengine cluster disable-firewalld

# 3. 部署 Kubernetes
kubengine-k8s deploy

# 4. 初始化 Harbor 项目
kubengine-k8s init-harbor

# 5. 推送集群镜像到 Harbor
kubengine-k8s push-images

# 6. 导入应用离线包到 Harbor
kubengine-k8s sync-bitnami import-dir /root/offline-deploy/bitnami-bundles/

# 7. 初始化应用数据
kubengine app init-data

# 8. 设置管理员密码
kubengine app set-password

# 9. 启动 API 服务（或配置 systemd 托管）
kubengine app run
```

### 集群扩容流程

```bash
# 1. 纳管新节点（SSH 互信 + 主机名）
kubengine cluster config \
  --hosts <新节点IP>,<已有节点IP列表> \
  --hostname-map <新节点IP>:<主机名>,...

# 2. 执行扩容
kubengine-k8s scale --worker-ip <新节点IP>
```
