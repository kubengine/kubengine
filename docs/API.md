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
| `/api/v1/ready` | 数据库、任务存储和镜像 worker 就绪检查 |

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

用户首次从实际配置幂等迁入数据库，之后数据库为唯一认证来源，修改旧配置不会覆盖或重新启用用户。管理员密码通过 `kubengine app set-password` 轮换，只修改数据库中的启用 admin；密码仅保存哈希。旧 AK/SK 签名协议继续关闭；API 调用请使用 Bearer Token。

JWT 使用 `${root_dir}/config/jwt-signing.key` 中独立生成的私钥（权限 0600），不再复用 TLS CA 私钥。身份迁移后已有令牌失效，需要重新登录。登出记录保存在数据库中，对同一安装的多个 API 进程和重启生效；停用和密码变更撤销全部会话，重新启用不会恢复旧令牌。角色与授权变更在下一次请求生效，权限不足返回 403 并保持登录。此文件属于运行数据，不得加入发布包。

当前为 RBAC 版本：普通用户可在授权命名空间内按角色读取/修改普通资源；Secret 明文、终端与文件操作分别授权。
应用、制品、基础设施、集群级管理与用户管理仍仅限管理员，操作统一进入审计。
权限矩阵、迁移与回滚要求见[用户权限与审计设计](用户权限与审计设计.md)。

WebSocket 保持现有 `token=Bearer ...` 查询参数接入方式，每次广播和回复发送前重新验证会话，客户端消息处理前也进行验证。空闲连接每 30 秒复核，失效时以 1008 关闭。升级步骤及任务恢复边界见[安全边界与资源管理修复说明](安全边界与资源管理修复-2026-09-28.md)。

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
| `GET` | `/api/v1/ready` | 服务依赖就绪检查；异常时返回 HTTP 503 |
| `POST` | `/api/v1/login` | 登录并获取 JWT |
| `POST` | `/api/v1/logout` | 注销当前 JWT |
| `GET` | `/api/v1/protected/unified` | 鉴权连通性检查 |

### 用户与授权

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/me` | 当前身份、配置授权及当前有效能力 |
| `POST` | `/api/v1/me/password` | `current_password`、`password`；成功后撤销本人全部会话 |
| `GET` | `/api/v1/roles` | 管理员查询三个内置角色与四个敏感权限 |
| `GET` | `/api/v1/users` | 管理员分页查询；page 从 1 开始，page_size 为 1～100 |
| `POST` | `/api/v1/users` | 创建用户：username、password、role、namespaces、sensitive_permissions |
| `PUT` | `/api/v1/users/{user_id}` | enabled、role、namespaces、sensitive_permissions、revision；版本冲突或撤销最后管理员返回 409 |
| `POST` | `/api/v1/users/{user_id}/password` | 管理员重置密码，撤销目标用户全部会话 |

role 可选 `platform_admin`、`namespace_operator`、`readonly`；默认 readonly、空范围及空敏感授权。
敏感权限为 `secret.read`、`pod.terminal`、`pod.files.read`、`pod.files.write`。
密码至少 12 字符、UTF-8 不超过 72 字节；现有配置密码哈希迁移不改变。
用户 ID 和用户名不可修改，不提供删除接口。查询不返回密码/旧 SK 哈希与凭据版本。

### SSH 与集群资源

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/ssh/execute-command` | 在单台主机执行命令 |
| `POST` | `/api/v1/ssh/execute-multiple` | 在多台主机执行命令 |
| `POST` | `/api/v1/ssh/upload-file` | 上传文件到远程主机 |
| `POST` | `/api/v1/ssh/download-file` | 从远程主机下载文件 |
| `GET` | `/api/v1/k8s/node` | 获取指定节点信息，要求 `name` 参数 |
| `GET` | `/api/v1/k8s/overview` | 获取集群资源与存储总览 |
| `GET` | `/api/v1/k8s/resource/{type}` | 查询资源列表 |
| `GET` | `/api/v1/k8s/resourcedetail/{type}/{namespace}/{name}` | 查询资源详情 |
| `GET` | `/api/v1/k8s/resourcepod/{type}/{namespace}/{name}` | 查询资源关联 Pod |

### 应用与集群记录

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/app/list` | 获取应用列表 |
| `GET` | `/api/v1/app/get/{app_id}` | 获取应用详情 |
| `POST` | `/api/v1/app/add` | 创建应用 |
| `PUT` | `/api/v1/app/update` | 更新应用 |
| `DELETE` | `/api/v1/app/del/{app_id}` | 删除应用 |
| `POST` | `/api/v1/app/deploy` | 提交应用部署 |
| `POST` | `/api/v1/app/cluster/{cluster_id}/retry` | 重试现有应用部署，复用当前任务意图 |
| `GET` | `/api/v1/app/cluster` | 获取集群记录 |
| `GET` | `/api/v1/app/cluster/{cluster_id}` | 获取指定集群 |
| `GET` | `/api/v1/app/clusterInfo/{cluster_id}` | 获取集群 Helm 资源信息 |
| `PUT` | `/api/v1/app/cluster/{cluster_id}/name` | 修改集群名称 |
| `DELETE` | `/api/v1/app/cluster/{cluster_ip}` | 删除集群记录 |

部署提交支持 `Idempotency-Key` 请求头（1–128 位字母、数字及 `._:-`）。同一用户使用同一键与相同参数重复提交时返回原应用；参数冲突返回 409，原应用已删除返回 410。省略该头保持旧版每次创建的行为。失败部署使用上表的 retry 接口；清理失败继续调用 DELETE 重试。应用与镜像任务均由 `kubengine app task-worker` 执行，原 `image-worker` 命令保持兼容。恢复边界和升级要求见[统一任务恢复与部署幂等](统一任务恢复与部署幂等-2026-09-28.md)。

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


### Kubernetes SDK 连接与监控

Kubernetes 资源接口通过官方 Python SDK 直连 API Server，不再读取
`config/admin-user.token` 或访问 `dashboard.<domain>`。默认部署流程已移除
Kubernetes Dashboard，离线包也不再需要它的镜像和 Chart。

连接配置依次为：显式传入的 kubeconfig、`KUBECONFIG` 环境变量、Pod 内
ServiceAccount、`/etc/kubernetes/admin.conf`、SDK 默认的 `~/.kube/config`。
TLS 校验沿用 kubeconfig / ServiceAccount 配置，不关闭证书校验。
后端进程的集群身份需要读取节点、Pod、工作负载、Service、Ingress、
EndpointSlice、ConfigMap、Secret、PV、PVC、StorageClass、事件的权限；
监控需要读取 `metrics.k8s.io` 的 nodes / pods。Pod 内部署可参考
[`kubernetes-sdk-rbac.yaml`](kubernetes-sdk-rbac.yaml)，在 Pod 上设置
`serviceAccountName: kubengine-reader`。该示例只授予 get/list，
无需 Dashboard 的管理令牌。
该示例仅覆盖工作台资源读取；应用发布等写入操作仍需使用现有部署身份及其权限。

资源列表仍返回 `listMeta.totalItems` 及对应的资源数组；名称按包含关系过滤，
先排序再分页。关联 Pod 使用资源真实的 matchLabels / matchExpressions，
而非猜测 `app=<资源名称>` 标签。Pod 资源分配包括 init / sidecar 和 overhead，
已结束的 Pod 不计入节点分配；集群 Pod 容量为各节点之和。

CPU、内存使用量由 Metrics Server 的 Metrics API 提供。Metrics API 不提供
历史查询；趋势仅保存当前进程读取的最近 30 分钟实际采样，每个资源最多 60
个点、最多 2048 个资源。重启后重新积累，多 worker 各自维护缓存，不伪造历史。
监控不可用时资源查询仍可使用，趋势为空并在 `errors` 中说明原因；缺失的当前
使用量为 null。未授权、资源不存在和连接失败分别保留对应错误。

旧 `/api/v1/k8s/dashboard/...` 路径作为兼容别名保留，以支持已有前端静态包；
它们只调用 SDK，不再连接任何 Dashboard 服务。新客户端使用表中的无
`dashboard` 前缀路径。Pod/Service 关联查询及 Deployment 的新 ReplicaSet
查询也保留兼容响应结构。存储容量继续从独立的 Longhorn 服务查询。

### 资源中心排障接口

资源列表新增 Job、CronJob、HPA（`horizontalpodautoscaler` / `hpa`）、
IngressClass、NetworkPolicy、Event，共支持 20 类资源。
`GET /api/v1/k8s/resource/{type}` 继续支持 `namespace`、`name`、`page`、
`page_size`，并新增以下可选参数：

| 参数 | 用途 |
| --- | --- |
| `label_selector` | Kubernetes 标签选择器，如 `app=nginx`、`app in (api,web)` |
| `field_selector` | Kubernetes 字段选择器，如事件 `type=Warning`、Pod `spec.nodeName=worker01` |
| `sort_by` | `a,name`、`d,name`、`a,namespace`、`d,namespace`、`a,creationTimestamp`、`d,creationTimestamp`；默认最新创建优先 |

选择器由 Kubernetes API 解释；非法表达式保留 API 错误。过滤先于分页，
`listMeta.totalItems` 是过滤后的总数。字段支持范围取决于资源对应的 Kubernetes API。

以下接口均要求现有 Bearer 登录认证，返回标准 `{code,message,data}` 结构。
集群级资源的路径 namespace 可填写 `_cluster`，后端不会将它传给 namespaced API。

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| `GET` | `/api/v1/k8s/manifest/{type}/{namespace}/{name}` | 返回 `manifest`、`yaml`、`redacted`；Secret 默认隐藏 data/stringData/注解值，显式 `reveal_secret=true` 才返回原值 |
| `GET` | `/api/v1/k8s/events/{type}/{namespace}/{name}` | 按资源 UID 查询事件，按最近发生时间降序后分页；参数 `page`、`page_size` |
| `GET` | `/api/v1/k8s/pods/{namespace}/{name}/logs` | 返回 `logs`、`container`、`previous`、`limitBytes`、`truncated`；只读取 Pod 所属容器 |

日志参数：`container`（省略时选择第一个主容器）、`tail_lines`（默认 200，1–2000）、
`since_seconds`（可选，1–604800）、`previous`（默认 false）、`timestamps`（默认 true）。
日志每次最多 1 MiB，不开启 `follow`。参数越界由 HTTP 422 或 SDK 客户端 400 拒绝，
容器不属于 Pod 返回 400；缺失上次日志、无权限等保留 Kubernetes 原始错误，不伪装为空日志。

原有 `resourcepod` 接口新增节点、Job、CronJob 的关联 Pod 查询。
节点按实际调度字段查询；CronJob 以 Owner UID 逐层关联 Job 与 Pod，不猜测标签。
事件或指标读取受限时在 `errors` 中说明，其他资源信息仍然可看。

完整迁移范围见 [Dashboard 迁移清单](kubernetes-dashboard迁移清单.md)。

### 资源 YAML 编辑与版本历史

在 `/api/v1/k8s/manifest/{type}/{namespace}/{name}` 后增加以下接口，均要求 Bearer 登录身份。
现有 YAML 查看接口保持原样。编辑仅更新当前 UID 的既有对象，Event 保持只读。

| 方法 | 后缀 | 用途 |
| --- | --- | --- |
| GET | `/edit?reveal_secret=false` | 可编辑 YAML、UID、resourceVersion 及是否可编辑 |
| POST | `/preview` | dry-run 校验，返回字段差异与 previewHash，不写集群 |
| POST | `/apply` | 校验 resourceVersion / previewHash 后应用，记录操作人及加密快照 |
| GET | `/versions?page=1&page_size=20` | 当前资源版本列表 |
| GET | `/versions/{id}?reveal_secret=false` | 修改前后、提交 YAML 及新增/删除/修改字段 |

预览 JSON：`{yaml_text, uid, resource_version, comment?, reveal_secret?}`；应用另需
`{request_id, preview_hash}`。请求标识用于幂等，版本状态为 pending / applied / failed / unknown。
冲突不强制覆盖，结果未知不自动重放；Secret 历史默认隐藏值。
历史只记录 KubeEngine YAML 应用，未订阅外部修改；详见 [编辑与版本说明](资源YAML编辑与版本历史.md)。

### Pod 终端与文件管理接口

均使用现有 Bearer 登录身份，集群身份需具备 `pods/exec` 的 `get/create` 权限。
容器由 `container` 查询参数指定。文件 JSON API 沿用标准响应结构，下载返回二进制响应。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `WS` | `/api/v1/k8s/pods/{namespace}/{name}/terminal?container=...&shell=sh` | 交互终端，shell 可为 `sh`、`bash`、`powershell` |
| `GET` | `/api/v1/k8s/pods/{namespace}/{name}/files?container=...&path=...` | 返回当前绝对路径、entries、windows、maxFileBytes、transferTimeoutSeconds |
| `POST` | `/api/v1/k8s/pods/{namespace}/{name}/files/operations?container=...` | JSON `{action,path,name?,recursive?}`；action 为 mkdir、touch、rename、delete |
| `POST` | `/api/v1/k8s/pods/{namespace}/{name}/files/upload?container=...` | multipart `file`、目录 `path`、`overwrite`（默认 false） |
| `GET` | `/api/v1/k8s/pods/{namespace}/{name}/files/download?container=...&path=...` | 普通文件二进制下载 |

终端首先发送 `{"type":"auth","token":"Bearer <JWT>"}`，不将 JWT 放在 URL。
收到 `{"type":"ready"}` 后可发送 `{"type":"input","data":"..."}`；
尺寸消息为 `{"type":"resize","cols":100,"rows":30}`。
输出消息为 `{"type":"output","encoding":"base64","data":"..."}`，解码后将原始字节传给终端，
避免 UTF-8 字符在跨帧时损坏；退出为 `{"type":"exit","code":0}`，
错误为 `{"type":"error","message":"..."}`。续签 token 后可再次发送 auth 消息，身份不可更换。
会话撤销/过期会关闭连接；退出页面不会自动重连执行 Shell。

单文件上传、下载上限读取 `security.uploads.pod_file_bytes`（默认 100 MiB），每段传输超时读取
`security.uploads.pod_timeout_seconds`（默认 300 秒）。目录接口返回当前有效限制，前端据此校验和设置超时。
上传失败不安装不完整目标文件；已有目标默认返回 409，显式 overwrite 才替换。
删除根目录、非法名称返回 400，非空目录未经递归确认返回 409；保留 Kubernetes / 容器权限错误。
详情和环境要求见 [Pod 终端与文件管理](Pod终端与文件管理.md)。

## HTTP 状态与异常响应

认证装饰器包装的 JSON 接口保留 `{code, message, data}`，HTTP 状态同时表达实际结果：
成功为 2xx，校验/权限/不存在/冲突等错误为对应 4xx，内部与上游错误为 5xx。
调用方不能仅检查 HTTP 200 或仅接受业务 `code: 200`。原有业务元组无需修改调用方式。
包装的 JSON 若业务码为 204/205，HTTP 使用 200 保留响应体；原始文件响应不包装。

错误响应可包含以下字段（空字段可省略，`data` 不保证存在）：

```json
{
  "code": 422,
  "message": "参数校验失败: name",
  "error_code": "VALIDATION_ERROR",
  "request_id": "9cf182315c4341998c0b8b0f6d65d9f7",
  "details": [{"loc": ["body", "name"], "type": "missing", "msg": "Field required"}]
}
```

- `error_code` 是稳定分类，如 `AUTH_REQUIRED`、`FORBIDDEN`、`CONFLICT`、
  `PAYLOAD_TOO_LARGE`、`RATE_LIMITED`、`INTERNAL_ERROR`、`UPSTREAM_ERROR`。
- 参数校验仅返回字段位置、错误类型和说明，不回显请求输入或 Pydantic 的原始上下文。
- 请求编号同时放在响应 `X-Request-ID` 与错误 JSON 的 `request_id`，可传入合法
  `X-Request-ID` 关联日志；未捕获异常对外仅返回通用消息和编号。
- HTTP 异常保留 `WWW-Authenticate`、`Retry-After` 等响应头；错误响应也可通过
  `X-New-Access-Token` / `X-Token-Type` 续签会话，正常 JSON 继续支持原续签字段。
- HTTP 401 表示控制台身份无效。业务处理中的上游认证失败转换为 HTTP 502、
  `error_code: UPSTREAM_AUTH_ERROR`、`details.upstream_status: 401`，不应让前端退出。
- 上游 HTTP 连接异常为 502，超时为 504；代理 HTML 和内部异常详情不会直接发给用户。
- 登录成功仍返回令牌对象，健康检查与文件下载保持其专用响应结构。

前端提示归属与请求策略见前端工程 `docs/REQUEST_ERRORS.md`。

应用配置/集群配置的详情在记录不存在时返回 404；Helm 部署资源读取失败返回 502。
部署表单缺少名称、Chart 或版本时返回 422。它们不再以成功响应或内部错误表示。

## 通过 YAML 创建资源

资源中心支持上传或粘贴 YAML，在集群预检后逐项创建；同名资源不覆盖。

| 方法 | 路径 | 功能 |
| --- | --- | --- |
| POST | `/api/v1/k8s/resources/preview` | 多文档 / List 预检，返回逐项结果和预检凭证 |
| POST | `/api/v1/k8s/resources/create` | 使用预检凭证与请求标识创建资源，返回逐项状态和初始版本 |

支持集群发现的可创建资源，每批最多 20 个、2 MiB；已有资源编辑及版本接口保持不变。
详见 [通过 YAML 创建资源](通过YAML创建资源.md)。


### 操作审计

`GET /api/v1/audit-events` 仅平台管理员可用。支持 page、page_size、username、actor_id、
action、namespace、resource_type、resource_name、result、request_id、task_id、batch_id，
以及 Unix 秒 since/until；返回 items 与 total。没有编辑或删除审计的 HTTP 接口。

写操作不能建立审计时返回 503 且不执行；审计收尾失败返回
`AUDIT_FINALIZE_UNCERTAIN`、审计编号与结果待核实说明，先核对状态再决定后续操作。

完整操作说明、归档命令及工作负载准入边界见[使用指南](用户权限与操作审计使用指南.md)。
