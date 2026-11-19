# 配置说明

Deployment 运维的独立平台观察时限可通过 `deployment_operations.observation_seconds`
配置，默认 600 秒，范围 30–86400 秒。超时后保留操作并只读核实，不自动重放或回滚；
与 Kubernetes `progressDeadlineSeconds` 的关系见 [Deployment 运维](Deployment运维.md)。

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
  ca_locality_name: Beijing
  ca_organization_name: kubengine
  ca_common_name: kubengine root ca
  ca_email_address: ssl@kubengine.io
  ca_password: kubengine
  ca_valid_days: 3650
  ca_key_length: 4096

# 日志配置
logger:
  level: INFO
  console: true
  format: "%(asctime)s - %(name)s - %(levelname)s - %(process)d - %(filename)s:%(lineno)d - %(message)s%(context_suffix)s"
  date_format: "%Y-%m-%d %H:%M:%S"
  rotate:
    enable: true
    when: D
    backup_count: 7
  readability:
    strip_ansi: true
    max_event_field_length: 500
  third_party_levels:
    uvicorn: WARNING
    uvicorn.access: WARNING
    click: INFO
    fastapi: INFO
    asyncssh: WARNING
    asyncio: WARNING
    pyinfra: WARNING
    paramiko: WARNING
    paramiko.transport: WARNING
    paramiko.transport.sftp: WARNING
    urllib3: WARNING

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

# Kubernetes 配置（单 master 示例）
kubernetes:
  master:
    ip: 172.31.57.23
    schedulable: false
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
  nameserver:
    - 8.8.8.8

# 镜像仓库配置
registry:
  username: admin
  password: Harbor@123
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

平台域名，用于 TLS 证书生成（Server 证书 SAN、各组件访问地址）等服务。

- **类型**：字符串
- **默认值**：`kubengine.io`
- **说明**：根据实际部署环境修改。修改后重新执行部署会自动重新生成证书链（见下方「证书重新生成」说明）

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

#### `tls.ca_locality_name`

CA 证书所在城市名称。

- **类型**：字符串
- **默认值**：`Beijing`

#### `tls.ca_organization_name`

CA 证书组织名称。

- **类型**：字符串
- **默认值**：`kubengine`

#### `tls.ca_common_name`

CA 证书通用名称（CN），用于 CA 自签名证书的 Subject CN 字段。

- **类型**：字符串
- **默认值**：`kubengine root ca`
- **说明**：与 `domain` 相互独立；需要自定义 CA 名称时在此配置

#### `tls.ca_email_address`

CA 证书联系邮箱。

- **类型**：字符串
- **默认值**：`ssl@kubengine.io`

#### `tls.ca_password`

CA 私钥密码（预留，用于后续私钥加密）。

- **类型**：字符串
- **默认值**：`kubengine`

#### `tls.ca_valid_days`

CA 证书有效期（天）。

- **类型**：整数
- **默认值**：`3650`（10年）
- **说明**：生产环境建议使用较长有效期

#### `tls.ca_key_length`

CA 私钥长度（位）。

- **类型**：整数
- **默认值**：`4096`
- **说明**：仅在下一次证书重新生成时生效（见下方「证书重新生成」说明）

---

### 日志配置

#### `logger.level`

应用日志级别，支持 `DEBUG`、`INFO`、`WARNING`、`ERROR` 和 `CRITICAL`。
默认为 `INFO`。CLI 显式传入的 `--debug`/`--quiet` 级别优先于该配置。

#### `logger.console`

是否同时将日志输出到控制台，默认为 `true`。文件日志不受此开关影响。

#### `logger.format` 与 `logger.date_format`

分别控制日志行格式和时间格式。默认格式中的 `%(context_suffix)s`
用于输出 `request_id`、`deployment_id`、`host`、`command_id` 等链路字段，
建议保留。

#### `logger.rotate`

- `enable`：是否启用日志轮转。
- `when`：轮转周期，支持 `S`、`M`、`H`、`D` 和 `W0` 至 `W6`；默认按天轮转。
- `backup_count`：保留的历史文件数，默认为 `7`。

文件日志默认使用进程锁，可供多个 Uvicorn worker 或 CLI 进程安全写入
同一路径。开启轮转时，同一时刻只有一个进程执行重命名和历史文件
清理；其他进程会自动重开新文件。运行时会在日志旁生成
`<日志文件>.lock`，该文件为正常的进程协调文件，请勿在服务运行期间删除。

#### `logger.readability`

- `strip_ansi`：移除 PyInfra 等组件输出的 ANSI 颜色控制符，默认为 `true`。
- `max_event_field_length`：生命周期事件中单个字段的最大展示长度，
  默认为 `500`，最小生效值为 `50`。该限制只影响文本展示，不修改命令返回值。

#### `logger.third_party_levels`

按 Logger 名称配置第三方库日志级别。默认将 PyInfra、Paramiko 和
urllib3 调整为 `WARNING`，避免逐主机成功、SSH 握手等高频日志淹没
业务生命周期事件。排查第三方库问题时，可临时将对应项调整为
`INFO` 或 `DEBUG`。修改日志配置后需重启对应进程。

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

首次启动或首次执行 `set-password` 时的管理员引导配置。
迁移完成后数据库为唯一身份来源；更改本节不会更新密码、增补用户或重新启用停用账号。

- **类型**：对象
- **字段**：
  - `password_hash`：bcrypt 哈希密码
  - `ak`：旧访问密钥 ID，仅为迁移兼容保留
  - `sk_hash`：旧密钥哈希，仅为迁移兼容保留

首次迁移将实际配置中的全部用户导入为平台管理员，保留原 bcrypt 哈希。
部署前应核对配置用户，并设置自己的管理员密码。旧 AK/SK 认证已停用，不再生成密钥对。

**设置管理员密码**：

```bash
# 修改数据库中已有、启用的 admin 管理员密码；撤销其全部旧会话
kubengine app set-password
```

迁移与回滚、停用保护和用户管理 API 见[用户权限与审计设计](用户权限与审计设计.md)。

---

### 上传与归档配额

通过 `security.uploads` 设置上传和留存限制，配置值须为正整数：

| 配置项 | 默认值 | 含义 |
| --- | --- | --- |
| `image_bytes` | 10737418240 | 单个镜像归档 10 GiB；Chart 固定 2 MiB |
| `archive_bytes` | 53687091200 | 留存镜像归档总计 50 GiB |
| `reserve_bytes` | 1073741824 | 临时盘、归档盘预留 1 GiB |
| `concurrency` | 2 | 同一安装各 API 进程的上传总并发 |
| `timeout_seconds` | 3600 | 请求接收、归档保存及中断上传回收期限，秒 |
| `retention_seconds` | 604800 | 已结束且清理完成的归档保留 7 天 |
| `pod_file_bytes` | 104857600 | Pod 单文件上传、下载上限 100 MiB |
| `pod_timeout_seconds` | 300 | Pod 单段传输超时，秒 |

超出请求体或文件限制返回 413，并发占满返回 429，空间或归档配额不足返回 507，
接收超时返回 408。维护循环约每 30 秒运行，清理失败至少间隔 60 秒再试；未完成清理
的归档仍计入配额。归档过期后任务结果保留，重试需要重新上传，不重复推送已成功项。

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
kubengine cluster config \
  --hosts 172.31.57.23,172.31.57.22,172.31.57.21 \
  --hostname-map 172.31.57.23:kubengine3,172.31.57.22:kubengine2,172.31.57.21:kubengine1
```

---

### Kubernetes 配置

#### `kubernetes.master.ip`

Master 节点 IP 地址。

- **类型**：字符串
- **必需**：是

#### `kubernetes.master.additional_ips`

附加 Master 节点 IP 列表（高可用模式）。

- **类型**：数组
- **默认值**：`[]`
- **说明**：配置后启用 HA 模式，附加 Master 通过 `--control-plane` join 加入控制面

#### `kubernetes.master.control-plane-endpoint`

控制面端点（VIP），用于 apiserver 高可用。

- **类型**：字符串
- **默认值**：空（单 master 模式）
- **说明**：HA 模式下必须配置，通常为 Keepalived VIP

#### `kubernetes.master.interface`

Master 节点网卡名称。

- **类型**：字符串
- **默认值**：空（自动检测）
- **说明**：Keepalived VIP 绑定的网卡，不指定时自动检测

#### `kubernetes.master.schedulable`

Master 节点是否可调度 Pod。

- **类型**：布尔值
- **默认值**：`false`
- **说明**：小型集群可设为 true 以充分利用资源

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

#### `kubernetes.nameserver`

DNS 服务器列表，写入各节点 `/etc/resolv.conf`。

- **类型**：数组
- **默认值**：`["8.8.8.8"]`
- **说明**：内网环境应配置内部 DNS 服务器

---

### 镜像仓库配置

#### `registry.username`

Harbor 镜像仓库用户名。

- **类型**：字符串
- **默认值**：`admin`

#### `registry.password`

Harbor 镜像仓库密码。执行 Chart 或镜像推送前必须配置；空密码不会触发
交互式密码输入，因为 Web 后台任务和自动化 CLI 没有可用终端。

- **类型**：字符串
- **默认值**：空
- **说明**：填写 Harbor 的真实管理员或机器人账号密码，并在修改后重启服务

---

## 配置项速查表

| 配置项 | 说明 | 默认值 | 必需 |
|--------|------|--------|------|
| `root_dir` | KubeEngine 根目录 | `/opt/kubengine` | 否 |
| `domain` | 平台域名 | `kubengine.io` | 否 |
| `tls.root_dir` | TLS 证书目录 | `/opt/kubengine/config/certs` | 否 |
| `tls.ca_country_code` | CA 国家代码 | `CN` | 否 |
| `tls.ca_state_name` | CA 省/州 | `Beijing` | 否 |
| `tls.ca_locality_name` | CA 城市 | `Beijing` | 否 |
| `tls.ca_organization_name` | CA 组织名 | `kubengine` | 否 |
| `tls.ca_common_name` | CA 通用名称（CN） | `kubengine root ca` | 否 |
| `tls.ca_email_address` | CA 联系邮箱 | `ssl@kubengine.io` | 否 |
| `tls.ca_password` | CA 私钥密码 | `kubengine` | 否 |
| `tls.ca_valid_days` | CA 证书有效期(天) | `3650` | 否 |
| `tls.ca_key_length` | CA 私钥长度(位) | `4096` | 否 |
| `auth.jwt.algorithm` | JWT 算法 | `HS256` | 否 |
| `auth.token.expire_minutes` | Token 过期时间(分钟) | `30` | 否 |
| `auth.token.renew_threshold_minutes` | Token 刷新阈值(分钟) | `5` | 否 |
| `cluster.nodes` | 集群节点 IP 列表 | `[]` | 是 |
| `cluster.hostnames` | 节点 IP 到主机名映射 | `{}` | 是 |
| `kubernetes.master.ip` | Master 节点 IP | - | 是 |
| `kubernetes.master.additional_ips` | 附加 Master IP（HA 模式） | `[]` | 否 |
| `kubernetes.master.control-plane-endpoint` | 控制面 VIP（HA 模式） | 空 | 否 |
| `kubernetes.master.interface` | Master 网卡名 | 自动检测 | 否 |
| `kubernetes.master.schedulable` | Master 是否可调度 | `false` | 否 |
| `kubernetes.worker.ips` | Worker 节点 IP 列表 | `[]` | 是 |
| `kubernetes.cidr.pod` | Pod CIDR | `10.96.0.0/16` | 否 |
| `kubernetes.cidr.service` | Service CIDR | `10.97.0.0/16` | 否 |
| `kubernetes.loadbalancer.ip-pools` | 负载均衡 IP 池 | `[]` | 否 |
| `kubernetes.nameserver` | DNS 服务器 | `["8.8.8.8"]` | 否 |
| `registry.username` | Harbor 用户名 | `admin` | 否 |
| `registry.password` | Harbor 密码 | 空 | 推送时是 |

---

## 配置验证

### 检查配置文件

```bash
# 显示当前配置
kubengine cluster show
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
kubengine cluster config --hosts <节点IP列表>
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
