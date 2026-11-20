# Deployment 运维

本功能仅支持 `apps/v1 Deployment`，复用控制台身份、命名空间授权、SDK 连接、审计和加密 YAML 版本。支持扩缩容、重启、普通容器镜像更新及指定原生 revision 回滚，不提供 HPA 接管、自动回滚、暂停或恢复。详细契约和实现边界见 [Deployment 发布设计](Deployment发布设计.md)。

## 接口与权限

接口前缀为 `/api/v1/k8s/deployments/{namespace}/{name}`，均返回统一响应体。

| 方法和路径 | 权限 | 行为 |
| --- | --- | --- |
| GET `/operations` | resource.read | 当前配置摘要及最近 50 条操作（包含已删除、同名重建目标的 UID） |
| GET `/operations/{operation_id}` | resource.read | 只返回此集群、命名空间和名称对应的操作 |
| POST `/scale` | resource.update | 202 受理扩缩容 |
| POST `/restart` | resource.update | 202 受理滚动重启 |
| GET `/history` | resource.read | 当前普通容器镜像及所属 ReplicaSet 原生历史摘要 |
| POST `/image/preview` | resource.update | 严格 dry-run、镜像差异和 5 分钟预检凭证 |
| POST `/image` | resource.update | 202 受理一次单/多普通容器镜像发布 |
| POST `/rollback/preview` | resource.update | 历史完整 Pod 模板的安全检查、dry-run 和脱敏差异 |
| POST `/rollback` | resource.update | 202 受理指定 revision 的模板回滚 |

写请求共同字段：`request_id`（1–64 位字母数字、下划线或短横线）、`uid`、`resource_version`；扩缩容另有严格整数 `replicas`（0–2147483647）和 `confirm_zero`（缩至 0 必须为 true）。确认弹窗使用读取到的 UID/resourceVersion，提交过期配置返回 409。重启标记由首次受理生成并持久化，重试复用原操作。

镜像预检参数为 `uid`、`resource_version`、`containers: [{name, image}]`，只允许明确选中的普通容器，镜像须含 tag 或 sha256 digest。回滚预检为 `uid`、`resource_version`、字符串 `revision` 和 `replica_set_uid`。受理时在完全相同的参数上增加 `request_id`、`preview_token`。`previewToken` 与 `expiresAt` 来自预检响应；凭证绑定集群、身份、资源版本、全部参数、目标摘要及回滚来源，不可跨操作者转用。修改输入、目标或来源变化、凭证过期须重新预检。服务端拒绝时不回显任意 SDK 错误正文。

401 表示控制台登录失效；403 表示平台/集群授权拒绝；404 表示目标或操作不存在；409 表示过期版本、同名重建、暂停、删除、HPA 管理、已有活动操作或幂等键参数冲突；422 表示参数类型/范围错误；503 表示无法安全记录或读取。已认证请求的上游 401 沿用统一机制转为 502。

平台权限与 Kubernetes RBAC 同时生效。只读用户只可查询授权命名空间。命名空间运维须满足既有工作负载安全规则（包括 ServiceAccount、宿主机与容器安全约束及 Secret 引用权限）。后台写入前重新加载用户、授权绑定和敏感授权，凭据版本变化、停用或撤权终止尚未执行的写入。

## 持久化、并发与恢复

新增独立的 `deployment_operation` 表，建表幂等，不复用或改变应用部署、镜像任务状态。SQLite `BEGIN IMMEDIATE` 串行分配幂等键、活动目标锁和 YAML 版本号。每个集群/namespace/name 同时最多一个未核实操作，UID 不同也不能绕过锁。幂等键绑定集群、目标、发起者与全部请求参数，重试返回原记录，不重复写入。

升级时幂等增加 `release_context` JSON 列，旧记录默认为空对象，不改变旧 scale/restart 恢复逻辑。镜像及回滚与旧动作共用目标锁；回滚来源 revision、ReplicaSet UID 和模板摘要保存在操作上下文，目标完整模板保存在加密版本。无需离线重建数据库或回填旧模板。

受理事务同时保存操作、专用执行审计和加密版本快照。HTTP 审计关联操作和版本；执行审计包含 UID、请求标识、动作和副本变更摘要的操作关联，不包含模板、环境变量或凭据。完整配置只写加密版本。数据库和历史密钥不可用时不发起写入。

统一 `kubengine-image-worker` 每次领取一个短步骤，使用独立租约/所有者和数据库条件更新隔离陈旧 worker，观察步骤复用统一心跳每 30 秒续租（租期 180 秒）。写入前持久化 `writing` 阶段，再调用 SDK；任何曾进入此阶段的操作恢复后都只读核实，绝不自动重放写入。扩缩容用 `/scale`，重启只合并模板注解 `kubengine.io/restartedAt`；两者均携带 UID/resourceVersion 前置条件。写入拒绝和响应丢失分别记录，不能将连接超时报告成“可安全重试”。

镜像发布和回滚使用 JSON Patch，先 test UID/resourceVersion，再仅 replace `/spec/template`。执行前重新授权、检查来源历史并执行同样的严格 dry-run，结果摘要必须与受理时一致。原生 revision 注解由 Kubernetes 控制器维护。HPA 不阻止模板发布，补丁不写副本数；观察期间 HPA 或其他入口再改 spec 会明确标记为被后续变更替代，不把外部操作算成本次成功。

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

更新镜像时先勾选普通容器、填写目标引用，再“执行预检”，核对当前/目标镜像及 UID、版本和策略，最后“确认发布”。一次可更新多个容器，只产生一次模板变更。不提供 initContainer 快捷镜像编辑和自动仓库授权，命名空间运维用户可直接填写已有访问权限的镜像引用。dry-run 成功不证明镜像存在、可拉取或安全；镜像拉取失败会在健康观察中显示白名单诊断及 Pod/日志入口。

原生历史仅包含 controller ownerReference 的 API 版本、类型、名称和 UID 均属于当前 Deployment 的 ReplicaSet。缺失/重复 revision、删除中、模板相同等均给出不可回滚原因；已清理的历史不能恢复，不调整 revisionHistoryLimit，也不将旧 revision 标为稳定版本。选择“预览回滚”后，完整历史模板会按当前权限及安全策略校验；界面仅显示镜像值，其余模板差异隐藏值。回滚保留当前副本数、selector、策略及其他非模板配置，不恢复 ConfigMap、Secret、PVC 或业务数据，也不保证可变标签仍是当时的镜像内容。回滚会生成新的平台版本与一次新的发布观察。

平台 YAML 版本号、Kubernetes 原生 `deployment.kubernetes.io/revision` 分开显示。版本状态描述写入结果，操作状态描述健康观察，二者可能不同。备份应同时保存 SQLite 数据库、`config/resource-history.key`、身份密钥和配置；恢复后启动 API 与统一 worker，验证健康和命名空间权限。不得为验证重启业务 Deployment；使用模拟 SDK 或明确授权的临时资源。

SDK 34–36 的 PATCH 字典由 REST 层选择 strategic merge，列表保持 JSON Patch；SDK 37 的序列化器显式指定相应 Content-Type，保留模板其他字段。兼容实现依据 [上游 34.1.0 传输源码](https://github.com/kubernetes-client/python/blob/v34.1.0/kubernetes/client/rest.py)，实际安装版本 35 和 37 的传输路径均已纳入自动测试。

## 隔离验收与升级

后端使用 `env/bin/python -m pytest -q`，测试配置、数据库及 SDK 均隔离。前端执行 `npm run test:request`、`node_modules/.bin/tsc --noEmit`、相关 ESLint/Stylelint 和 `npm run build`。浏览器验收服务只监听本机，显式使用构建产物和模拟 SDK，不连接工作区真实集群：

```bash
# 后端目录，临时数据库和密钥由测试配置创建，退出后清理。
env/bin/python tests/deployment_release_browser_server.py --static-dir ../dashboard/dist
# 前端目录，用已安装的 playwright-cli 创建独立会话。
playwright-cli -s=deployment-release open http://127.0.0.1:18080/api/v1/health
playwright-cli -s=deployment-release run-code --filename=tests/deploymentReleases.browser.cjs
playwright-cli -s=deployment-release run-code --filename=tests/deploymentReleaseFailures.browser.cjs
playwright-cli -s=deployment-release run-code --filename=tests/deploymentReleaseAccessibility.browser.cjs
playwright-cli -s=deployment-release close
```

发布前备份数据库、历史密钥、身份/TLS 密钥及配置；构建产物替换 `src/web/static` 后逐文件校验。对既定 API 和统一 worker 服务执行升级重启后，检查 `/api/v1/health`、`/api/v1/ready` 和三角色权限。真实集群验证仅使用明确授权的临时 Deployment，结束后清理。未知结果记录仍需用原请求编号只读核实，禁止手工清锁或重放写入。

升级期间先停止旧 API 受理并停止旧 worker，再完成一致性备份和代码/产物更新；启动新版 worker（执行幂等迁移）后再开放新版 API。新动作必须由新版 worker 处理，避免旧进程将未知动作按重启解释。新版 worker 可恢复旧 scale/restart 记录；代码回退前需停止新动作受理并核实队列，不让旧 worker 接管新动作记录。

2026-11-20 隔离交付验收：后端完整回归 676 项、请求策略回归 34 项、前端类型/规范/生产构建、三角色浏览器主流程及异常流程均通过；移动端竖屏/横屏、减少动画、焦点循环及关闭返回也已验证。静态产物 93 个文件逐文件 SHA-256 一致。上述功能验收使用临时数据库与模拟 SDK；目标环境部署核验见下文。

## 172.31.57.23 部署记录（2026-11-20）

已按用户指令部署到 `172.31.57.23`，安装目录为 `/opt/kubengine`，沿用既有
`kubengine-api` 和 `kubengine-image-worker` systemd 服务及虚拟环境，未更换依赖、
配置或密钥。发布编号为 `deployment-release-20261120T005227Z`。

- 目标机 Kubernetes SDK 为 35.0.0；候选代码在目标机通过发布、旧运维及 YAML
  相关隔离回归 129 项。测试断言分别覆盖旧 SDK 的 REST 传输和 37 的序列化路径。
- 数据库恢复副本经过完整性检查和两次幂等迁移，18 张表的原有数据保持一致。
- 停止旧 API 和 worker 后备份代码、静态产物、实际配置、数据库、身份/TLS/历史
  密钥及服务定义；备份及校验清单位于
  `/opt/kubengine/.deployments/deployment-release-20261120T005227Z/backup`，
  所在发布目录权限为 0700。21 个配置和密钥文件的内容及原权限均保持不变。
- 发布文件共 340 个；静态文件 93 个均与前端构建一致，旧静态文件已移除。
  新版 worker 和 API 已启动，机内及工作站访问 `/api/v1/health`、`/api/v1/ready`
  均正常，实际 HTTP 返回的首页及主脚本也与发布清单一致。
- 使用现有三个角色创建短期验收会话，验证身份、获授权原生历史、只读写入拒绝、
  命名空间边界、无变化拒绝、缺失回滚来源冲突及未登录拒绝；会话在结束后全部撤销，
  未修改用户密码、角色或命名空间绑定。
- 管理员通过实际集群严格服务端镜像预检；检查前后业务 Deployment UID 和 spec
  摘要不变。原有 3 条 scale/restart 操作仍可读取，验收没有新增操作或 YAML 版本。
  所检查的业务目标当前仅有一个原生历史，实际发布及回滚写入由隔离 SDK 验收覆盖，
  本次未对业务工作负载执行发布、回滚、扩缩容或重启。

远端受限发布目录中保留 `deployment-result.json`、`api-verification.json`、源包
校验清单及恢复副本预演结果。部署验收完成后，工作区临时计划与根目录 AGENTS
入口已按约定清理。
