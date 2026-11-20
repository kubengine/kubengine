# Deployment 镜像发布与原生回滚设计

本期在既有 Deployment 运维状态机上增加 `image`、`rollback`，仅支持
`apps/v1 Deployment`。操作、执行审计及加密 YAML 版本在同一受理事务创建，
与 scale/restart 共用目标锁、租约和只读恢复机制。

## 接口契约

前缀 `/api/v1/k8s/deployments/{namespace}/{name}`，使用既有响应封装。

| 方法 / 路径 | 平台权限 | 结果 |
| --- | --- | --- |
| GET `/history` | resource.read | 当前 UID、resourceVersion、普通容器和原生 ReplicaSet 历史摘要 |
| POST `/image/preview` | resource.update | 严格服务端 dry-run 后的安全差异及 5 分钟预检凭证 |
| POST `/image` | resource.update | 202，返回既有 operation、revision、task_id |
| POST `/rollback/preview` | resource.update | 完整目标 Pod 模板安全校验、dry-run、脱敏差异及预检凭证 |
| POST `/rollback` | resource.update | 202，与其他动作一致的持久化受理 |
| GET `/operations`、`/operations/{id}` | resource.read | 沿用现有进度与恢复接口 |

预检共有字段 `uid`、`resource_version`。镜像参数 `containers` 为非空的
`[{name, image}]`，按名称匹配一个或多个普通容器，拒绝重复、未知名称、无变化及
未明确指定 tag/digest 的引用。回滚参数为字符串 `revision` 与 `replica_set_uid`。
受理请求额外包含 `request_id` 和 `preview_token`。平台 YAML 版本编号不能用作 revision。
预检返回 `changes`（路径、动作、前后值、redacted）、`expiresAt`、`previewToken`、
目标 UID/resourceVersion、策略和副本数。只有镜像路径返回值，其余模板差异隐藏值。
不返回完整模板、环境变量、注解正文或凭据。

凭证使用现有历史密钥认证加密，绑定集群、操作者/凭据版本、目标名称、UID、
resourceVersion、动作及全部业务参数、目标 spec 摘要；回滚同时绑定来源 ReplicaSet
UID、revision、模板摘要。输入、身份、目标或历史变化及过期均返回 409，必须重新预检。
重复请求先检查持久化幂等键：原参数返回原操作，改参返回 409；重试不会因凭证过期重放写入。

## 历史与模板

按页读取命名空间 ReplicaSet。只有唯一 controller ownerReference 同时匹配
`apps/v1`、`Deployment`、名称及当前 Deployment UID 的对象才属于当前历史。
缺失/无效/重复 revision、正在删除或与当前模板相同的历史返回不可回滚原因。
不存在的 revision 不生成替代模板，不更改 revisionHistoryLimit，不标记“稳定版本”。

回滚从源 Pod 模板移除 `pod-template-hash` 与 metadata 服务端生成字段，保留用户
标签、注解和完整 Pod spec；重新检查 initContainers、ServiceAccount、卷及 Secret
引用权限。普通容器镜像快捷编辑不支持 initContainers。当前副本数、selector、
更新策略及其他非模板配置保留。dry-run 和执行都使用同一 JSON Patch：先 test UID
和 resourceVersion，再 replace `/spec/template`，避免合并补丁遗留已删除字段。
Kubernetes 控制器负责维护原生 revision，不写 Deployment/ReplicaSet revision 注解。

## 持久化与兼容

`deployment_operation` 新增 `release_context JSON NOT NULL DEFAULT '{}'`，通过幂等
迁移为旧表补列。只保存来源 revision、ReplicaSet UID/摘要、模板改动路径与镜像摘要；
完整目标模板复用 ResourceRevision 的加密快照，不新增明文模板列。旧 scale/restart
记录为空上下文，仍按原规则解释。执行前重新读取目标和来源，复核当前授权、
安全策略和 dry-run 摘要；任何历史清理、目标版本变化或准入结果变化均终止写入。

进入 writing 后不重放；响应丢失、进程重启和审计收尾失败均恢复只读核实。
成功要求同 UID、精确 spec 摘要和目标 generation，并完成控制器及新旧副本健康观察。
外部修改（包括 HPA 在观察期间修改副本数）会标记被后续变更替代，不归功于本次操作。
HPA 不阻止镜像/回滚操作，补丁不写 replicas；scale 仍遵守 HPA 冲突规则。

## 权限与边界

只读用户能查看授权命名空间的历史/进度；运维用户可预检和受理；管理员沿用既有权限。
后端同时检查 Deployment 和 ReplicaSet 的平台读取权限及 SDK 实际读取权限。
后台写入前重新载入用户和绑定。预检、受理及执行审计不记录模板和任意 SDK 错误正文。
镜像先手工填写，不扩张 artifact.manage，也不引入外部字体/CDN。

dry-run 不证明镜像存在、可拉取或安全。回滚只恢复模板，不恢复 ConfigMap、Secret、
PVC、数据或可变标签当时的镜像内容。历史保留受 Kubernetes 清理影响。
