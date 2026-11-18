# Kubernetes Dashboard 核心查看功能迁移

## 巡检依据

2026-11-18 使用项目 `playwright-cli` 技能登录现有
`https://dashboard.dawnlight.kubengine.io/`（dashboard-web 1.7.0），
只读核对了导航、全部命名空间 Pod 列表、Pod 详情与日志查看器、事件入口及存储类页面。
登录 token 从本地文件读取，没有写入前端代码、文档或构建产物。

主要缺口是 KubeEngine 原来的资源查看入口依赖已注册应用集群，无法直接浏览系统命名空间，
也没有独立的日志、事件和原始资源 YAML 工作区。本次按日常巡检与排障频率筛选迁移范围。

## 已迁移范围

| 核心功能 | KubeEngine 入口与行为 |
| --- | --- |
| 全集群资源浏览 | 左侧「资源中心」，可查看所有命名空间及不属于 KubeEngine 应用的资源 |
| 工作负载 | Pod、Deployment、StatefulSet、DaemonSet、ReplicaSet、Job、CronJob、HPA，共 8 类 |
| 服务与网络 | Service、Ingress、IngressClass、NetworkPolicy；Ingress 后端服务可跳转；Service 展示 EndpointSlice 地址、端口及就绪状态 |
| 配置与存储 | ConfigMap、Secret、PVC、PV、StorageClass；挂载来源、PVC/PV、存储类相互跳转 |
| 集群与事件 | 节点、命名空间、事件；节点条件、规格和状态；保留原有节点监控入口 |
| 筛选与分页 | 命名空间、名称包含匹配、Kubernetes 标签表达式、名称或创建时间排序、分页；条件保存在 URL，支持刷新与分享链接 |
| 状态与指标 | Pod 等待原因（如 CrashLoopBackOff）、重启数、CPU/内存当前使用量；工作负载就绪副本；指标缺失显示「—」 |
| 资源详情 | 元数据、标签注解、Owner、运行条件、规格状态；Pod 容器、Init/临时容器、命令参数、环境变量、探针与卷挂载 |
| 关联 Pod | 工作负载、Service 按真实 selector 查询；节点按 spec.nodeName；CronJob 按 CronJob→Job→Pod 的 Owner UID 查找 |
| 容器日志 | 主容器、Init/临时容器选择；当前/上一次容器日志；时间范围、200–2000 行、时间戳、5 秒刷新、当前内容过滤及下载 |
| 资源事件 | 详情按资源 UID 查询，按最近发生时间降序分页；全局事件可筛选 Warning / Normal，关联对象可跳转 |
| 原始 YAML | 原始 Kubernetes 对象的只读查看、复制、下载；省略体积较大的 managedFields |
| Secret 查看 | 默认在服务端隐藏 data、stringData 和注解值；显式点击后才读取原值，切换资源重新隐藏；原值保留 base64 表示 |
| 既有页面联动 | 应用资源详情增加资源中心、事件、YAML、Pod 日志入口；保留原有监控和应用管理流程 |

资源列表默认手动刷新，可开启 15 秒刷新。日志使用有界轮询而非长连接流式跟随，
每次最多 2000 行、1 MiB；退出页面或关闭刷新后取消请求与定时器。
文本筛选只影响显示；下载保留当前容器、时间范围和行数查询返回的原始文本。

## 保留范围

本次聚焦资源查看与排障。RBAC 管理、CRD/任意自定义资源浏览、旧版
ReplicationController 专用页面、在线编辑/删除、扩缩容、终端 exec 与资源创建
不属于本轮迁移。现有 KubeEngine 应用发布流程继续使用自己的入口。

不卸载服务器上现有的 Kubernetes Dashboard；迁移代码不通过它获取任何资源。
旧 `/k8s/dashboard/...` API 只保留给历史静态包兼容，新前端使用 SDK 接口。

## SDK 与权限

全部资源、事件、EndpointSlice 与日志来自 Kubernetes Python SDK，CPU/内存来自
Metrics API。只读身份权限示例见 [kubernetes-sdk-rbac.yaml](kubernetes-sdk-rbac.yaml)，
新增 `batch` Jobs/CronJobs、`autoscaling` HPA、IngressClass/NetworkPolicy 和
`pods/log` 的读取权限。该示例未自动应用到任何集群。

API 参数和错误行为见 [API 文档](API.md#资源中心只读排障接口)。

## 验证

- 后端完整回归：369 项通过；资源 SDK 专项：75 项通过。
- 专项覆盖新增资源的 SDK 与命名空间分派、selector、Owner UID 关联、事件排序分页、
  Secret 隐藏/显示、日志大小/行数范围、Init/临时容器、上次日志错误及 API 鉴权。
- 使用独立临时配置、数据库和 token kubeconfig 启动本地开发 API；浏览器通过开发前端
  实际读取原集群数据，未改动线上 API 或 Kubernetes 资源。
- 20 类资源接口均通过真实集群验证；日志与 YAML 下载、Secret 隐藏/显式显示及切换后隐藏、
  Service 端点与关联 Pod、503 后重试恢复、空列表及 375/768/1024/1440px 布局通过验证。
- 前端生产构建成功，85 个静态文件同步至后端 `src/web/static`，SHA256 全部一致；
  未更新线上服务，原 Kubernetes Dashboard 保留。
