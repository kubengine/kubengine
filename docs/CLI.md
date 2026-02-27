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
| `--workers INTEGER` | 工作进程数 | `1` |

**示例：**
```bash
# 原生 Python 方式
python -m cli.app run --host 0.0.0.0 --port 8080 --workers 4

# kubengine 命令方式
kubengine app run --host 0.0.0.0 --port 8080 --workers 4
```

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

**默认管理员账户**：

| 项目 | 值 |
|------|-----|
| 用户名 | `admin` |
| 默认密码 | `Admin@123` |
| AK（访问密钥 ID） | `AK8F60249C` |
| SK（密钥） | `SK17F1B276797F4957` |

> ⚠️ **安全警告**：生产环境请立即修改默认密码！

**说明**：首次设置会自动生成新的 AK/SK 密钥对。

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
python -m cli.cluster configure-cluster [OPTIONS]
```

#### kubengine 命令方式

```bash
kubengine cluster configure-cluster [OPTIONS]
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
python -m cli.cluster configure-cluster \
  --hosts 172.31.57.23,172.31.57.22,172.31.57.21 \
  --hostname-map 172.31.57.23:kubengine3,172.31.57.22:kubengine2,172.31.57.21:kubengine1 \
  --username root

# kubengine 命令方式
kubengine cluster configure-cluster \
  --hosts 172.31.57.23,172.31.57.22,172.31.57.21 \
  --hostname-map 172.31.57.23:kubengine3,172.31.57.22:kubengine2,172.31.57.21:kubengine1 \
  --username root
```

---

### 显示集群配置

#### 原生 Python 方式

```bash
python -m cli.cluster show-cluster-config
```

#### kubengine 命令方式

```bash
kubengine cluster show-cluster-config
```

---

### 在集群节点上执行命令

#### 原生 Python 方式

```bash
python -m cli.cluster execute-cmd [OPTIONS]
```

#### kubengine 命令方式

```bash
kubengine cluster execute-cmd [OPTIONS]
```

**选项：**
| 选项 | 说明 |
|------|------|
| `--hosts TEXT` | 节点 IP 列表 |
| `--cmd TEXT` | 要执行的命令（必需） |
| `--username TEXT` | SSH 用户名 |
| `--password TEXT` | SSH 密码 |
| `--key-file TEXT` | SSH 私钥文件路径 |

**示例：**
```bash
# 在所有节点执行命令
kubengine cluster execute-cmd --hosts 172.31.57.23,172.31.57.22 --cmd "hostname"
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

### 使用 kubengine_k8s 独立命令（推荐）

kubengine_k8s 是专门用于 Kubernetes 部署的独立命令行工具，提供更强大的功能和更好的用户体验：

```bash
kubengine_k8s --help
```

**可用命令：**
| 命令 | 说明 |
|------|------|
| `deploy` | 部署 Kubernetes 集群 |
| `config` | 配置管理命令 |
| `reset-state` | 重置部署状态 |

#### 部署 Kubernetes 集群

```bash
kubengine_k8s deploy [OPTIONS]
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
kubengine_k8s deploy

# 指定离线部署目录
kubengine_k8s deploy --deploy-src /path/to/offline-files

# 显示配置但不执行部署
kubengine_k8s deploy --show-config

# 详细输出日志
kubengine_k8s deploy -vvv
```

---

#### 显示和验证配置

```bash
kubengine_k8s config [OPTIONS]
```

**选项：**
| 选项 | 说明 |
|------|------|
| `--validate` | 验证配置 |
| `--show` | 显示配置 |

**示例：**
```bash
# 显示当前配置
kubengine_k8s config --show

# 验证配置
kubengine_k8s config --validate
```

---

#### 重置部署状态

```bash
kubengine_k8s reset-state [OPTIONS]
```

**选项：**
| 选项 | 说明 |
|------|------|
| `--force` | 强制重置状态 |

**示例：**
```bash
kubengine_k8s reset-state --force
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
kubengine cluster configure-cluster \
  --hosts 172.31.57.23,172.31.57.22,172.31.57.21 \
  --hostname-map 172.31.57.23:kubengine3,172.31.57.22:kubengine2,172.31.57.21:kubengine1

# 2. 禁用防火墙
kubengine cluster disable-firewalld

# 3. 部署 Kubernetes
kubengine_k8s deploy

# 4. 初始化应用数据
kubengine app init-data

# 5. 设置管理员密码
kubengine app set-password

# 6. 启动 API 服务
kubengine app run
```
