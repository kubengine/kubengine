# Deployment 运维

本功能仅支持 `apps/v1 Deployment`，复用控制台身份、命名空间授权、SDK 连接、审计和加密 YAML 版本。快捷操作不提供 HPA 接管、回滚、暂停或恢复。

## 接口与权限

接口前缀为 `/api/v1/k8s/deployments/{namespace}/{name}`，均返回统一响应体。

| 方法和路径 | 权限 | 行为 |
| --- | --- | --- |
| GET `/operations` | resource.read | 当前配置摘要及最近 50 条操作（包含已删除、同名重建目标的 UID） |
| GET `/operations/{operation_id}` | resource.read | 只返回此集群、命名空间和名称对应的操作 |
| POST `/scale` | resource.update | 202 受理扩缩容 |
| POST `/restart` | resource.update | 202 受理滚动重启 |

写请求共同字段：`request_id`（1–64 位字母数字、下划线或短横线）、`uid`、`resource_version`；扩缩容另有严格整数 `replicas`（0–2147483647）和 `confirm_zero`（缩至 0 必须为 true）。确认弹窗使用读取到的 UID/resourceVersion，提交过期配置返回 409。重启标记由首次受理生成并持久化，重试复用原操作。

401 表示控制台登录失效；403 表示平台/集群授权拒绝；404 表示目标或操作不存在；409 表示过期版本、同名重建、暂停、删除、HPA 管理、已有活动操作或幂等键参数冲突；422 表示参数类型/范围错误；503 表示无法安全记录或读取。已认证请求的上游 401 沿用统一机制转为 502。

平台权限与 Kubernetes RBAC 同时生效。只读用户只可查询授权命名空间。命名空间运维须满足既有工作负载安全规则（包括 ServiceAccount、宿主机与容器安全约束及 Secret 引用权限）。后台写入前重新加载用户、授权绑定和敏感授权，凭据版本变化、停用或撤权终止尚未执行的写入。

## 持久化、并发与恢复

新增独立的 `deployment_operation` 表，建表幂等，不复用或改变应用部署、镜像任务状态。SQLite `BEGIN IMMEDIATE` 串行分配幂等键、活动目标锁和 YAML 版本号。每个集群/namespace/name 同时最多一个未核实操作，UID 不同也不能绕过锁。幂等键绑定集群、目标、发起者与全部请求参数，重试返回原记录，不重复写入。

受理事务同时保存操作、专用执行审计和加密版本快照。HTTP 审计关联操作和版本；执行审计包含 UID、请求标识、动作和副本变更摘要的操作关联，不包含模板、环境变量或凭据。完整配置只写加密版本。数据库和历史密钥不可用时不发起写入。

统一 `kubengine-image-worker` 每次领取一个短步骤，使用独立租约/所有者和数据库条件更新隔离陈旧 worker，观察步骤复用统一心跳每 30 秒续租（租期 180 秒）。写入前持久化 `writing` 阶段，再调用 SDK；任何曾进入此阶段的操作恢复后都只读核实，绝不自动重放写入。扩缩容用 `/scale`，重启只合并模板注解 `kubengine.io/restartedAt`；两者均携带 UID/resourceVersion 前置条件。写入拒绝和响应丢失分别记录，不能将连接超时报告成“可安全重试”。

| 状态 | 语义 |
| --- | --- |
| pending | 已受理，等待 worker |
| running | 写入或健康观察中；写入是否接受另看 writeStatus |
| succeeded | 身份、配置、generation 和健康条件都满足 |
| failed | 集群明确拒绝或本次发布进度明确失败 |
| timed_out | 超过观察期限或写入结果不确定，继续只读核实并保留目标锁 |
| superseded | 删除、同名重建或后续配置修改替代本操作 |
| permission_revoked | 发起者失效或执行前撤权，未重放写入 |

已发出写入之后的撤权只停止以发起者权限访问集群，保留待核实记录和目标锁；重新获权后恢复只读观察。操作查看者使用自己的 resource.read 权限，不能获得通用任务接口或参数。进度查询在目标删除时仍返回操作记录；集群拒绝读取时返回明确错误。

## 完成判断与诊断

预期 generation 为写入前 generation + 1（目标副本相同返回 409，不发起空操作）。只允许同 UID、预期 spec 摘要、精确目标 generation，正常 resourceVersion/status 更新不视作替代；更高 generation 或其他配置视作 superseded。响应丢失时同样使用这些条件核实，未变化的旧配置只表示尚未核实，不能再次写入。

`observedGeneration >= targetGeneration` 后，目标、updated、ready、available、total 副本均须与目标一致；归属于此 UID 的 ReplicaSet 实际副本总和须符合目标，旧模板 ReplicaSet 副本必须为 0（包含 terminatingReplicas）。重启到 0 只确认配置已应用且无残留副本，不表示发生 Pod 替换。模板匹配时忽略控制器添加的 `pod-template-hash` 标签。Deployment 暂停、删除中拒绝操作；按其 RollingUpdate/Recreate 策略执行，可能中断服务。

Deployment `ProgressDeadlineExceeded` 或 `ReplicaFailure=True` 为明确失败。仅输出白名单诊断代码（镜像拉取、调度、配额、探针、容器崩溃），不返回事件 message、环境变量或任意 SDK 错误正文。通过归属此 Deployment 的 ReplicaSet/Pod UID 筛选关联事件，页面链接到已有 Pod、事件和日志入口。诊断权限不足不伪装为成功。

配置 `deployment_operations.observation_seconds` 默认 600 秒、最小 30 秒、最大 86400 秒，从受理时开始计算。排队等待超过期限而尚未进入写入阶段时，明确记为 failed（未执行），不会过期后再发起写入。此期限独立于 Deployment 的 progressDeadlineSeconds；先出现集群明确进度失败则 failed，先超时则 timed_out。超时不回滚、不重试写入，worker 继续只读核实；若一直无法确认，管理员根据请求编号、审计、加密 YAML 版本及集群对象排查，不提供自动清锁/重放按钮。

## 使用与运维

资源中心的 Deployment 详情及原有 Deployment 详情均有运维卡片。扩缩容先确认 HPA 和副本数，缩至 0 需额外确认停服；重启确认展示更新策略。受理提示与最终成功分开；刷新或离开后从服务端记录恢复进度，网络错误不会创建新操作。动态撤权及 403 停止旧的敏感展示；暂时轮询错误以内联方式提示，401 仍走统一登录处理。

平台 YAML 版本号、Kubernetes 原生 `deployment.kubernetes.io/revision` 分开显示。版本状态描述写入结果，操作状态描述健康观察，二者可能不同。备份应同时保存 SQLite 数据库、`config/resource-history.key`、身份密钥和配置；恢复后启动 API 与统一 worker，验证健康和命名空间权限。不得为验证重启业务 Deployment；使用模拟 SDK 或明确授权的临时资源。

SDK 34 的 PATCH 字典由 SDK 传输层选择 strategic merge；SDK 37 显式指定相同 Content-Type，保留模板其他字段。兼容实现依据 [上游 34.1.0 传输源码](https://github.com/kubernetes-client/python/blob/v34.1.0/kubernetes/client/rest.py)，实际安装版本 37 的序列化路径已纳入自动测试。
