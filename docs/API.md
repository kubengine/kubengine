# API 文档

KubeEngine 的 HTTP API 由 FastAPI 提供，默认前缀为 `/api/v1`。本页记录稳定的接入方式和当前路由概览；请求体、查询参数和响应模型应以运行中服务自动生成的 OpenAPI 文档为准。

## 访问入口

启动服务：

```bash
kubengine app run --host 0.0.0.0 --port 8080
```

| 地址 | 用途 |
| --- | --- |
| `/` | 内置 Web UI |
| `/docs` | Swagger UI |
| `/redoc` | ReDoc |
| `/openapi.json` | OpenAPI 描述文件 |
| `/api/v1/health` | 无鉴权健康检查 |

## 登录与鉴权

先使用管理员账号登录：

```bash
curl -X POST 'http://localhost:8080/api/v1/login' \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<管理员密码>"}'
```

当前登录响应包含以下字段：

```json
{
  "name": "admin",
  "access_token": "<JWT>",
  "token_type": "Bearer",
  "expires_at": "2026-09-23T10:30:00",
  "renewed": false
}
```

调用受保护接口时携带 Bearer Token：

```bash
curl 'http://localhost:8080/api/v1/k8s/overview' \
  -H 'Authorization: Bearer <JWT>'
```

Token 临近过期时，鉴权装饰器会自动续期，并在标准响应中返回更新后的 Token 信息。退出登录使用：

```bash
curl -X POST 'http://localhost:8080/api/v1/logout' \
  -H 'Authorization: Bearer <JWT>'
```

管理员密码和 AK/SK 通过 `kubengine app set-password` 设置或轮换。仓库配置文件只保存密码和 SK 的哈希值，文档不提供可直接使用的默认明文密钥。

## 标准响应

受 `auth_with_renew` 保护的接口通常返回统一结构：

```json
{
  "code": 200,
  "message": "操作成功",
  "data": {},
  "new_access_token": null,
  "token_type": "Bearer"
}
```

不同接口的 `data` 结构请在 Swagger UI 中查看。校验失败、鉴权失败和服务异常会使用相应 HTTP 状态码，并返回统一错误响应。

## 当前路由概览

### 基础与认证

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/health` | 服务健康检查 |
| `POST` | `/api/v1/login` | 登录并获取 JWT |
| `POST` | `/api/v1/logout` | 注销当前 JWT |
| `GET` | `/api/v1/protected/unified` | 鉴权连通性检查 |

### SSH 与集群资源

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/ssh/execute-command` | 在单台主机执行命令 |
| `POST` | `/api/v1/ssh/execute-multiple` | 在多台主机执行命令 |
| `POST` | `/api/v1/ssh/upload-file` | 上传文件到远程主机 |
| `POST` | `/api/v1/ssh/download-file` | 从远程主机下载文件 |
| `GET` | `/api/v1/k8s/node` | 获取指定节点信息，要求 `name` 参数 |
| `GET` | `/api/v1/k8s/overview` | 获取集群资源与存储总览 |
| `GET` | `/api/v1/k8s/dashboard/resource/{type}` | 查询资源列表 |
| `GET` | `/api/v1/k8s/dashboard/resourcedetail/{type}/{namespace}/{name}` | 查询资源详情 |
| `GET` | `/api/v1/k8s/dashboard/resourcepod/{type}/{namespace}/{name}` | 查询资源关联 Pod |

### 应用与集群记录

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/app/list` | 获取应用列表 |
| `GET` | `/api/v1/app/get/{app_id}` | 获取应用详情 |
| `POST` | `/api/v1/app/add` | 创建应用 |
| `PUT` | `/api/v1/app/update` | 更新应用 |
| `DELETE` | `/api/v1/app/del/{app_id}` | 删除应用 |
| `POST` | `/api/v1/app/deploy` | 提交应用部署 |
| `GET` | `/api/v1/app/cluster` | 获取集群记录 |
| `GET` | `/api/v1/app/cluster/{cluster_id}` | 获取指定集群 |
| `GET` | `/api/v1/app/clusterInfo/{cluster_id}` | 获取集群 Helm 资源信息 |
| `PUT` | `/api/v1/app/cluster/{cluster_id}/name` | 修改集群名称 |
| `DELETE` | `/api/v1/app/cluster/{cluster_ip}` | 删除集群记录 |

### Harbor 制品

制品接口根路径为 `/api/v1/artifacts`，覆盖以下操作：

- 查询项目和仓库；
- 查询、删除制品和读取 Chart Values；
- 查询、新增和删除制品标签；
- 通过 `POST /api/v1/artifacts/upload/chart` 上传 Chart。

完整路径较长且包含 Harbor 项目、仓库、digest 等路径参数，建议直接通过 Swagger UI 调试。

### 离线镜像导入任务

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/artifacts/image-import-tasks` | 上传离线镜像文件并创建后台任务 |
| `POST` | `/api/v1/artifacts/upload/image` | 上一接口的兼容入口 |
| `GET` | `/api/v1/artifacts/image-import-tasks` | 分页查询任务 |
| `GET` | `/api/v1/artifacts/image-import-tasks/{task_id}` | 查询任务及镜像明细 |
| `POST` | `/api/v1/artifacts/image-import-tasks/{task_id}/retry` | 重试失败镜像 |

上传示例：

```bash
curl -X POST 'http://localhost:8080/api/v1/artifacts/image-import-tasks' \
  -H 'Authorization: Bearer <JWT>' \
  -F 'file=@images.tar'
```

镜像导入在后台执行。服务重启时会恢复未完成任务；可通过列表/详情接口轮询状态，并对失败项发起重试。

### 异步任务与 WebSocket

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/create-resource` | 创建演示异步资源任务并返回 `task_id` |
| `WS` | `/api/v1/ws?token=Bearer%20<JWT>` | 心跳和任务状态通信 |

WebSocket 建连后可发送 `{"action":"ping"}` 进行心跳。Token 查询参数需要包含 `Bearer ` 前缀，并进行 URL 编码。

## 维护说明

新增或修改路由后，应同步更新本页概览。可用以下方式从运行时应用核对真实路由：

```bash
PYTHONPATH=src python - <<'PY'
from web.main import app

for route in app.routes:
    if route.path.startswith('/api/'):
        methods = ','.join(sorted(getattr(route, 'methods', []) or [])) or 'WS'
        print(f'{methods:12} {route.path}')
PY
```
