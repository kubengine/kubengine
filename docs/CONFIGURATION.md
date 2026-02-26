# 配置说明

## 配置文件位置

KubeEngine 支持多种配置文件路径，按优先级从高到低：

1. **环境变量指定路径**：`KUBEENGINE_CONFIG` 环境变量
2. **默认安装路径**：`/opt/kubengine/config/application.yaml`（RPM 安装默认）
3. **相对路径**：`./config/application.yaml`（开发调试）

### 配置文件查找顺序

程序启动时会按以下顺序查找配置文件：

```python
1. 环境变量 KUBEENGINE_CONFIG 指定的路径
2. /opt/kubengine/config/application.yaml (默认安装路径)
3. ./config/application.yaml (相对路径，用于开发调试)
```

### 使用环境变量指定配置

```bash
export KUBEENGINE_CONFIG=/custom/path/application.yaml
kubengine app run
```

---

## 主配置文件

配置文件路径：`/opt/kubengine/config/application.yaml`

### 完整配置示例

```yaml
# KubeEngine 根目录
root_dir: /opt/kubengine

# 平台域名
domain: kubengine.io

# TLS 证书配置
tls:
  root_dir: /opt/kubengine/config/certs
  ca_country_code: CN
  ca_state_name: Beijing
  ca_organization_name: kubengine
  ca_valid_days: 3650

# 认证配置
auth:
  jwt:
    algorithm: HS256
  token:
    expire_minutes: 30
    renew_threshold_minutes: 5
  users:
    admin:
      password_hash: $2b$12$FoYHbiERoL12HGhlVaFG3ue5NhSfbx8xnZ/g8.7yTpZfPLknyxwLS
      ak: AK8F60249C
      sk_hash: $2b$12$pRoAn5q7YC/.alND0si3nOFDN6qTFS.msgaFmY6Ta80Wk62RIFuGC

# 集群节点配置
cluster:
  nodes:
    - 172.31.57.23
    - 172.31.57.22
    - 172.31.57.21
  hostnames:
    172.31.57.23: kubengine3
    172.31.57.22: kubengine2
    172.31.57.21: kubengine1

# Kubernetes 配置
kubernetes:
  master:
    ip: 172.31.57.23
    schedulable: True
  worker:
    ips:
      - 172.31.57.22
      - 172.31.57.21
  cidr:
    pod: 10.96.0.0/16
    service: 10.97.0.0/16
  loadbalancer:
    ip-pools:
      - 172.31.57.30-172.31.57.40
```

---

## 配置项说明

### 基础配置

#### `root_dir`

KubeEngine 根目录，用于存放数据、日志等文件。

- **类型**：字符串
- **默认值**：`/opt/kubengine`
- **说明**：建议使用默认值，确保目录权限正确

#### `domain`

平台域名，用于 TLS 证书生成等服务。

- **类型**：字符串
- **默认值**：`kubengine.io`
- **说明**：根据实际部署环境修改

---

### TLS 证书配置

#### `tls.root_dir`

TLS 证书存储目录。

- **类型**：字符串
- **默认值**：`/opt/kubengine/config/certs`
- **目录结构**：
  ```
  certs/
  ├── ca/              # CA 证书
  │   ├── ca.crt
  │   └── ca.key
  └── server/          # 服务器证书
      ├── server.crt
      └── server.key
  ```

#### `tls.ca_country_code`

CA 证书国家代码。

- **类型**：字符串
- **默认值**：`CN`
- **说明**：ISO 3166-1 alpha-2 国家代码

#### `tls.ca_state_name`

CA 证书省/州名称。

- **类型**：字符串
- **默认值**：`Beijing`

#### `tls.ca_organization_name`

CA 证书组织名称。

- **类型**：字符串
- **默认值**：`kubengine`

#### `tls.ca_valid_days`

CA 证书有效期（天）。

- **类型**：整数
- **默认值**：`3650`（10年）
- **说明**：生产环境建议使用较长有效期

---

### 认证配置

#### `auth.jwt.algorithm`

JWT 签名算法。

- **类型**：字符串
- **默认值**：`HS256`
- **可选值**：`HS256`, `HS384`, `HS512`, `RS256` 等

#### `auth.token.expire_minutes`

访问令牌过期时间（分钟）。

- **类型**：整数
- **默认值**：`30`
- **说明**：根据安全需求调整

#### `auth.token.renew_threshold_minutes`

令牌刷新阈值（分钟）。

- **类型**：整数
- **默认值**：`5`
- **说明**：令牌剩余有效期低于此值时可刷新

#### `auth.users`

管理员用户配置。

- **类型**：对象
- **字段**：
  - `password_hash`：bcrypt 哈希密码
  - `ak`：访问密钥 ID
  - `sk_hash`：bcrypt 哈希的密钥

**默认管理员账户**：

| 项目 | 值 |
|------|-----|
| 用户名 | `admin` |
| 默认密码 | `Admin@123` |
| AK（访问密钥 ID） | `AK8F60249C` |
| SK（密钥） | `SK17F1B276797F4957` |

> ⚠️ **安全警告**：生产环境安装后请立即修改默认密码！

**设置管理员密码**：

```bash
# 使用 CLI 命令设置密码（会自动生成新的 AK/SK）
kubengine app set-password
```

---

### 集群配置

#### `cluster.nodes`

集群节点 IP 列表。

- **类型**：数组
- **示例**：
  ```yaml
  cluster:
    nodes:
      - 172.31.57.23
      - 172.31.57.22
      - 172.31.57.21
  ```

#### `cluster.hostnames`

节点 IP 到主机名的映射。

- **类型**：对象
- **示例**：
  ```yaml
  cluster:
    hostnames:
      172.31.57.23: kubengine3
      172.31.57.22: kubengine2
      172.31.57.21: kubengine1
  ```

**使用 CLI 配置集群**：

```bash
kubengine cluster configure-cluster \
  --hosts 172.31.57.23,172.31.57.22,172.31.57.21 \
  --hostname-map 172.31.57.23:kubengine3,172.31.57.22:kubengine2,172.31.57.21:kubengine1
```

---

### Kubernetes 配置

#### `kubernetes.master.ip`

Master 节点 IP 地址。

- **类型**：字符串
- **必需**：是

#### `kubernetes.master.schedulable`

Master 节点是否可调度 Pod。

- **类型**：布尔值
- **默认值**：`True`
- **说明**：小型集群可设为 True 以充分利用资源

#### `kubernetes.worker.ips`

Worker 节点 IP 列表。

- **类型**：数组
- **示例**：
  ```yaml
  kubernetes:
    worker:
      ips:
        - 172.31.57.22
        - 172.31.57.21
  ```

#### `kubernetes.cidr.pod`

Pod 网络的 CIDR 地址段。

- **类型**：字符串
- **默认值**：`10.96.0.0/16`
- **说明**：确保与网络环境不冲突

#### `kubernetes.cidr.service`

Service 网络的 CIDR 地址段。

- **类型**：字符串
- **默认值**：`10.97.0.0/16`
- **说明**：确保与 Pod 网络和环境网络不冲突

#### `kubernetes.loadbalancer.ip-pools`

MetalLB 负载均衡的 IP 地址池。

- **类型**：数组
- **示例**：
  ```yaml
  kubernetes:
    loadbalancer:
      ip-pools:
        - 172.31.57.30-172.31.57.40
  ```
- **说明**：地址范围需在同一网段且未被占用

---

## 配置项速查表

| 配置项 | 说明 | 默认值 | 必需 |
|--------|------|--------|------|
| `root_dir` | KubeEngine 根目录 | `/opt/kubengine` | 否 |
| `domain` | 平台域名 | `kubengine.io` | 否 |
| `tls.root_dir` | TLS 证书目录 | `/opt/kubengine/config/certs` | 否 |
| `tls.ca_country_code` | CA 国家代码 | `CN` | 否 |
| `tls.ca_state_name` | CA 省/州 | `Beijing` | 否 |
| `tls.ca_organization_name` | CA 组织名 | `kubengine` | 否 |
| `tls.ca_valid_days` | CA 证书有效期(天) | `3650` | 否 |
| `auth.jwt.algorithm` | JWT 算法 | `HS256` | 否 |
| `auth.token.expire_minutes` | Token 过期时间(分钟) | `30` | 否 |
| `auth.token.renew_threshold_minutes` | Token 刷新阈值(分钟) | `5` | 否 |
| `cluster.nodes` | 集群节点 IP 列表 | `[]` | 是 |
| `cluster.hostnames` | 节点 IP 到主机名映射 | `{}` | 是 |
| `kubernetes.master.ip` | Master 节点 IP | - | 是 |
| `kubernetes.master.schedulable` | Master 是否可调度 | `True` | 否 |
| `kubernetes.worker.ips` | Worker 节点 IP 列表 | `[]` | 是 |
| `kubernetes.cidr.pod` | Pod CIDR | `10.96.0.0/16` | 否 |
| `kubernetes.cidr.service` | Service CIDR | `10.97.0.0/16` | 否 |
| `kubernetes.loadbalancer.ip-pools` | 负载均衡 IP 池 | `[]` | 否 |

---

## 配置验证

### 检查配置文件

```bash
# 显示当前配置
kubengine cluster show-cluster-config
```

### 常见配置问题

#### 1. 配置文件不存在

**错误信息**：
```
配置文件不存在，请检查以下路径：
  - /opt/kubengine/config/application.yaml (默认安装路径)
  - ./config/application.yaml (相对路径)
  或设置环境变量 KUBEENGINE_CONFIG 指定配置文件路径
```

**解决方法**：
- 确保 RPM 已正确安装
- 或使用环境变量指定配置文件路径

#### 2. 节点 IP 配置错误

**错误信息**：
```
集群节点配置不完整
```

**解决方法**：
```bash
kubengine cluster configure-cluster --hosts <节点IP列表>
```

#### 3. CIDR 地址冲突

**现象**：Pod 或 Service 无法正常通信

**解决方法**：检查网络环境，修改 CIDR 配置避免冲突

---

## 生产环境建议

### 安全建议

1. **修改默认密码**：安装后立即修改管理员密码
   ```bash
   kubengine app set-password
   ```

2. **限制配置文件权限**：
   ```bash
   chmod 640 /opt/kubengine/config/application.yaml
   chown root:root /opt/kubengine/config/application.yaml
   ```

3. **定期更新 TLS 证书**：根据 `ca_valid_days` 设置定期更新

### 性能建议

1. **根据集群规模调整 Token 有效期**：大型集群建议延长过期时间

2. **Master 节点可调度**：小型集群可启用 Master 可调度以充分利用资源

3. **合理规划 CIDR**：根据预期 Pod 和 Service 数量规划地址段大小

---

## 开发环境配置

开发环境可以使用相对路径配置文件：

```bash
# 在项目根目录创建配置
mkdir -p config
cp config/application.yaml.example config/application.yaml

# 编辑配置文件
vim config/application.yaml

# 运行服务
python -m cli.app run
```

开发环境配置文件示例：

```yaml
root_dir: ./data
domain: dev.kubengine.io
cluster:
  nodes:
    - 192.168.1.10
  hostnames:
    192.168.1.10: dev-node1
kubernetes:
  master:
    ip: 192.168.1.10
    schedulable: True
  worker:
    ips: []
  cidr:
    pod: 10.96.0.0/16
    service: 10.97.0.0/16
```
