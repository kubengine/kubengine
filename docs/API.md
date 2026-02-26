# API 文档

KubeEngine 提供完整的 RESTful API，支持 Kubernetes 集群管理、应用部署、镜像构建等功能。

## 访问方式

启动服务后访问：

- **Swagger UI**：`http://localhost:8080/docs`
- **ReDoc**：`http://localhost:8080/redoc`

---

## 认证

大部分 API 端点需要认证。使用 JWT Token 进行身份验证。

### 默认管理员账户

| 项目 | 值 |
|------|-----|
| 用户名 | `admin` |
| 默认密码 | `Admin@123` |
| AK（访问密钥 ID） | `AK8F60249C` |
| SK（密钥） | `SK17F1B276797F4957` |

> ⚠️ **安全警告**：生产环境请立即修改默认密码！

### 获取 Token

通过登录接口获取访问令牌：

```bash
curl -X POST "http://localhost:8080/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "admin",
    "password": "Admin@123"
  }'
```

**响应示例**：

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "expires_in": 1800
}
```

### 使用 Token

在请求头中添加 Authorization：

```bash
curl -X GET "http://localhost:8080/api/v1/k8s/node" \
  -H "Authorization: Bearer <access_token>"
```

### 刷新 Token

```bash
curl -X POST "http://localhost:8080/api/v1/auth/renew" \
  -H "Authorization: Bearer <refresh_token>"
```

---

## API 端点

### 认证 (`/api/v1/auth`)

#### POST `/api/v1/auth/login`

用户登录，返回 JWT Token。

**请求体**：

```json
{
  "username": "admin",
  "password": "password"
}
```

**响应**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `access_token` | string | 访问令牌 |
| `refresh_token` | string | 刷新令牌 |
| `token_type` | string | 令牌类型（bearer） |
| `expires_in` | integer | 过期时间（秒） |

---

#### POST `/api/v1/auth/renew`

刷新访问令牌。

**请求头**：

```
Authorization: Bearer <refresh_token>
```

**响应**：与登录响应相同

---

### 健康检查 (`/api/v1/health`)

#### GET `/api/v1/health/`

系统健康状态检查。

**响应示例**：

```json
{
  "status": "healthy",
  "version": "0.1.0"
}
```

---

### SSH 管理 (`/api/v1/ssh`)

#### POST `/api/v1/ssh/execute`

在远程主机执行命令。

**请求体**：

```json
{
  "hosts": ["172.31.57.23", "172.31.57.22"],
  "command": "hostname",
  "username": "root"
}
```

**参数**：

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `hosts` | array | 是 | 目标主机 IP 列表 |
| `command` | string | 是 | 要执行的命令 |
| `username` | string | 否 | SSH 用户名（默认 root） |
| `password` | string | 否 | SSH 密码 |
| `key_file` | string | 否 | SSH 私钥文件路径 |

**响应示例**：

```json
{
  "results": [
    {
      "host": "172.31.57.23",
      "output": "kubengine3\n",
      "error": "",
      "exit_code": 0
    },
    {
      "host": "172.31.57.22",
      "output": "kubengine2\n",
      "error": "",
      "exit_code": 0
    }
  ]
}
```

---

### Kubernetes 管理 (`/api/v1/k8s`)

#### GET `/api/v1/k8s/node`

获取集群节点信息。

**响应示例**：

```json
{
  "items": [
    {
      "metadata": {
        "name": "kubengine1"
      },
      "status": {
        "capacity": {
          "cpu": "4",
          "memory": "8Gi"
        },
        "conditions": [
          {
            "type": "Ready",
            "status": "True"
          }
        ]
      }
    }
  ]
}
```

---

#### GET `/api/v1/k8s/overview`

获取集群概览，含 CPU/内存指标。

**响应示例**：

```json
{
  "nodes": 3,
  "pods": 42,
  "namespaces": 8,
  "cpu_usage": "45%",
  "memory_usage": "62%"
}
```

---

#### GET `/api/v1/k8s/dashboard/resource/{type}`

列出 K8s 资源（Pod、Service 等）。

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `type` | string | 资源类型（pod, service, deployment 等） |

**查询参数**：

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `namespace` | string | 否 | 命名空间（默认 all） |

**示例**：

```bash
# 列出所有 Pod
curl "http://localhost:8080/api/v1/k8s/dashboard/resource/pod"

# 列出 default 命名空间的 Service
curl "http://localhost:8080/api/v1/k8s/dashboard/resource/service?namespace=default"
```

---

#### GET `/api/v1/k8s/dashboard/resourcedetail/{type}/{namespace}/{name}`

获取资源详情。

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `type` | string | 资源类型 |
| `namespace` | string | 命名空间 |
| `name` | string | 资源名称 |

---

#### GET `/api/v1/k8s/dashboard/resourcepod/{type}/{namespace}/{name}`

获取资源关联的 Pod。

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `type` | string | 资源类型 |
| `namespace` | string | 命名空间 |
| `name` | string | 资源名称 |

---

#### 节点污点管理

##### GET `/api/v1/k8s/node/{name}/taints`

获取节点污点配置。

##### POST `/api/v1/k8s/node/{name}/taints`

添加节点污点。

**请求体**：

```json
{
  "key": "key1",
  "value": "value1",
  "effect": "NoSchedule"
}
```

**效应类型（effect）**：

- `NoSchedule`：不允许未匹配的 Pod 调度
- `PreferNoSchedule`：尽量避免调度
- `NoExecute`：驱逐已存在的未匹配 Pod

##### DELETE `/api/v1/k8s/node/{name}/taints`

删除节点污点。

**请求体**：

```json
{
  "key": "key1",
  "effect": "NoSchedule"
}
```

---

### 应用管理 (`/api/v1/app`)

#### GET `/api/v1/app/list`

分页列出应用。

**查询参数**：

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `page` | integer | 否 | 页码（默认 1） |
| `page_size` | integer | 否 | 每页数量（默认 10） |

**响应示例**：

```json
{
  "items": [
    {
      "id": 1,
      "name": "redis",
      "version": "7.0.15",
      "description": "Redis 缓存服务"
    }
  ],
  "total": 42,
  "page": 1,
  "page_size": 10
}
```

---

#### GET `/api/v1/app/get/{app_id}`

根据 ID 获取应用详情。

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `app_id` | integer | 应用 ID |

---

#### POST `/api/v1/app/add`

创建新应用。

**请求体**：

```json
{
  "name": "redis",
  "version": "7.0.15",
  "description": "Redis 缓存服务",
  "chart_name": "redis",
  "repo_url": "https://charts.bitnami.com/bitnami"
}
```

---

#### PUT `/api/v1/app/update`

更新应用。

**请求体**：与添加应用相同

---

#### DELETE `/api/v1/app/del/{app_id}`

删除应用。

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `app_id` | integer | 应用 ID |

---

#### POST `/api/v1/app/deploy`

部署应用到 Kubernetes 集群。

**请求体**：

```json
{
  "app_id": 1,
  "cluster_id": 1,
  "namespace": "default",
  "values": {
    "replicaCount": 3,
    "image": {
      "repository": "redis",
      "tag": "7.0.15"
    }
  }
}
```

---

#### GET `/api/v1/app/cluster`

列出所有集群。

**响应示例**：

```json
{
  "items": [
    {
      "id": 1,
      "name": "生产集群",
      "endpoint": "https://172.31.57.23:6443",
      "status": "active"
    }
  ]
}
```

---

#### GET `/api/v1/app/cluster/{cluster_id}`

获取集群详情。

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `cluster_id` | integer | 集群 ID |

---

#### GET `/api/v1/app/clusterInfo/{cluster_id}`

获取集群资源详情（节点、Pod 等统计信息）。

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `cluster_id` | integer | 集群 ID |

---

#### PUT `/api/v1/app/cluster/{cluster_id}/name`

更新集群名称。

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `cluster_id` | integer | 集群 ID |

**请求体**：

```json
{
  "name": "新集群名称"
}
```

---

#### DELETE `/api/v1/app/cluster/{cluster_ip}`

删除集群。

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `cluster_ip` | string | 集群 IP 地址 |

---

### 制品管理 (`/api/v1/artifacts`)

#### GET `/api/v1/artifacts/list`

列出制品文件。

**查询参数**：

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `path` | string | 否 | 子目录路径 |

**响应示例**：

```json
{
  "items": [
    {
      "name": "app-v1.0.0.tar.gz",
      "size": 1048576,
      "modified_at": "2025-01-15T10:30:00Z"
    }
  ]
}
```

---

#### POST `/api/v1/artifacts/upload`

上传制品文件。

**请求类型**：`multipart/form-data`

**表单字段**：

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `file` | file | 是 | 制品文件 |

**示例**：

```bash
curl -X POST "http://localhost:8080/api/v1/artifacts/upload" \
  -H "Authorization: Bearer <token>" \
  -F "file=@app-v1.0.0.tar.gz"
```

---

#### GET `/api/v1/artifacts/download/{filename}`

下载制品文件。

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `filename` | string | 文件名 |

---

#### DELETE `/api/v1/artifacts/delete/{filename}`

删除制品文件。

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `filename` | string | 文件名 |

---

### WebSocket (`/api/v1/ws`)

#### `/api/v1/ws/logs`

实时任务日志流。

**连接方式**：

```javascript
const ws = new WebSocket('ws://localhost:8080/api/v1/ws/logs?task_id=123');

ws.onmessage = (event) => {
  console.log(event.data); // 实时日志输出
};
```

**查询参数**：

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `task_id` | string | 是 | 任务 ID |

---

## 错误响应

API 错误响应遵循标准 HTTP 状态码。

### 错误响应格式

```json
{
  "detail": "错误描述信息"
}
```

### 常见状态码

| 状态码 | 说明 |
|--------|------|
| `200 OK` | 请求成功 |
| `201 Created` | 资源创建成功 |
| `400 Bad Request` | 请求参数错误 |
| `401 Unauthorized` | 未认证或 Token 无效 |
| `403 Forbidden` | 无权限访问 |
| `404 Not Found` | 资源不存在 |
| `422 Unprocessable Entity` | 请求格式正确但语义错误 |
| `500 Internal Server Error` | 服务器内部错误 |

### 错误示例

**认证失败**：

```json
{
  "detail": "Invalid authentication credentials"
}
```

**资源不存在**：

```json
{
  "detail": "Application not found"
}
```

---

## 使用示例

### 完整的 API 调用流程

```bash
# 1. 登录获取 Token
TOKEN=$(curl -s -X POST "http://localhost:8080/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "password"}' \
  | jq -r '.access_token')

# 2. 获取集群节点列表
curl -X GET "http://localhost:8080/api/v1/k8s/node" \
  -H "Authorization: Bearer $TOKEN"

# 3. 部署应用
curl -X POST "http://localhost:8080/api/v1/app/deploy" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "app_id": 1,
    "cluster_id": 1,
    "namespace": "default"
  }'

# 4. 执行远程命令
curl -X POST "http://localhost:8080/api/v1/ssh/execute" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "hosts": ["172.31.57.23"],
    "command": "kubectl get nodes"
  }'
```

---

## Python SDK 示例

```python
import requests

BASE_URL = "http://localhost:8080/api/v1"

# 登录
response = requests.post(f"{BASE_URL}/auth/login", json={
    "username": "admin",
    "password": "password"
})
token = response.json()["access_token"]

# 设置认证头
headers = {"Authorization": f"Bearer {token}"}

# 获取集群节点
nodes = requests.get(f"{BASE_URL}/k8s/node", headers=headers).json()

# 部署应用
requests.post(f"{BASE_URL}/app/deploy", headers=headers, json={
    "app_id": 1,
    "cluster_id": 1,
    "namespace": "default"
})
```

---

## 速率限制

API 可能实施速率限制以防止滥用。

**默认限制**：
- 每小时 1000 请求/用户
- 超出限制返回 `429 Too Many Requests`

**速率限制响应头**：

```
X-RateLimit-Limit: 1000
X-RateLimit-Remaining: 950
X-RateLimit-Reset: 1641234567
```

---

## API 版本

当前 API 版本：`v1`

URL 路径包含版本号：`/api/v1/...`

主要版本变更会在文档中注明。
