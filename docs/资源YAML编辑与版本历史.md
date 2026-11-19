# 资源 YAML 编辑与版本历史

资源中心 → 资源详情 → YAML，支持编辑现有资源、校验并预览、确认应用及版本历史。
沿用当前 Kubernetes 身份及 KubeEngine 登录身份，操作人由服务端取登录用户。

## 使用流程

1. 点击「编辑 YAML」，读取最新对象及 resourceVersion。编辑器隐藏 status 和服务端管理字段。
2. 修改 YAML，可填写变更说明，点击「校验并预览差异」。
3. 后端以 `dryRun=All` / `fieldValidation=Strict` 调用 Kubernetes，验证字段、权限、不可变字段及准入规则。
4. 检查新增、删除、修改的字段路径和前后值，点击「确认应用到集群」。
5. 应用后查看版本历史：版本号、操作人、时间、说明、状态、resourceVersion 和各项差异。

使用 Kubernetes 的现有对象更新（PUT），删除 YAML 中的配置字段会提交为删除，不能修改
apiVersion、kind、名称、命名空间、UID、resourceVersion、status 等受保护字段。
适用于资源中心现有 19 类资源；Event 由集群生成，保持只读。Pod 等资源的不可变字段由集群拒绝，
不会自动删除重建。确认应用表示 API Server 已接受配置，控制器的滚动更新等动作仍异步执行。

Kubernetes 协议依据：[API Concepts：Dry-run 与资源版本](https://kubernetes.io/docs/reference/using-api/api-concepts/)。
使用 SDK 配置的 TLS 和身份，不通过 kubectl 或外部 Dashboard；原始 JSON 请求保留 SDK 模型尚不认识的新字段。

## 并发及失败处理

- 编辑基于实际资源 UID 和 resourceVersion，预览及应用都检查；提交 PUT 也携带这些前置条件。
- 资源被修改或同名重建时返回冲突，保留编辑器草稿，不强制覆盖；复制需要保留的草稿后重新载入合并。
- 确认应用前再次 dry-run，校验结果与已预览的摘要不一致时要求重新预览。
- 没有实际配置差异不写资源，也不创建版本。
- 应用请求带幂等标识；重复请求返回已有记录，不重复执行 PUT。
- 版本记录先以 pending 持久化，再写集群，成功后记录实际返回对象；集群明确拒绝标记 failed。
- 连接中断或服务进程退出可能无法确认写入结果，标记 unknown / 保留 pending；此时先检查资源，避免盲目重试。
- 集群写入成功但历史结果更新失败时，已保存的 pending 记录仍存在，界面明确提示核对，不报告为可安全重试的失败。

## 历史范围与备份

历史只记录通过 KubeEngine 发起的 YAML 应用，不监听外部 kubectl、Helm 或控制器修改。
版本按集群地址和 CA 指纹、资源类型、命名空间、名称及 UID 区分；同名重建后从新版本序列开始。
版本包含修改前、提交内容和实际修改后的配置。失败或待确认的版本展示预期配置，不能视为已生效。
没有自动回滚或强制覆盖；可根据历史内容重新编辑、校验并应用。

数据库新表 `resource_yaml_revision` 随 API 启动自动创建。所有 YAML 快照以 Fernet 加密存储，
独立密钥为 `${root_dir}/config/resource-history.key`，自动创建且要求权限 0600、归属服务账号。
备份时同时保留 `${root_dir}/config/sqlite.db` 与该密钥；丢失密钥将无法解密历史。
已存在历史时密钥缺失会阻止新的集群写入，不会创建新密钥覆盖旧历史的加密关系。
历史不会自动清理，应按部署规模监控数据库空间并备份。

Secret 编辑须先显式显示值；默认历史仅显示改动字段，隐藏 data / stringData 及注解值。
查询历史也会重新读取当前资源，以验证 Kubernetes 读取权限；资源删除后旧记录仍保存在数据库，
当前页面不提供跨 UID 或已删除资源的历史检索。浏览器不向 localStorage 保存 YAML 草稿或 Secret 内容。

单个 YAML 最大 2 MiB，单文档，拒绝重复字段、非字符串键、非有限数值和锚点引用；
限制结构深度、字段事件数和每次 2000 项差异。请求体在解析前鉴权并限制为 8 MiB。

## 集群权限

读取及预览需要 get，校验与应用需要 update。现有只读 RBAC 不会自动获得写权限。
可按需使用 [独立 YAML 编辑 RBAC 示例](kubernetes-yaml-editor-rbac.yaml)，或将权限限制到指定命名空间的 Role。
示例不自动应用，不增加 create、delete 或 status 子资源权限。

## API

前缀：`/api/v1/k8s/manifest/{kind}/{namespace}/{name}`，集群资源 namespace 用 `_cluster`。
所有接口要求 Bearer 登录身份并返回标准响应。

| 方法 | 后缀 | 行为 |
| --- | --- | --- |
| GET | `/edit?reveal_secret=false` | 返回可编辑 YAML、uid、resourceVersion、editable、reason |
| POST | `/preview` | Kubernetes dry-run，返回 changes、previewHash、beforeYaml、afterYaml、noChanges |
| POST | `/apply` | 幂等应用并持久化版本，返回 revision、resourceVersion 或待确认 warning |
| GET | `/versions?page=1&page_size=20` | 当前资源 UID 的版本列表，含分页 total |
| GET | `/versions/{id}?reveal_secret=false` | 版本元信息、前后 YAML、提交内容及字段差异 |

预览 body：`{yaml_text, uid, resource_version, comment?, reveal_secret?}`。
应用额外要求 `{request_id, preview_hash}`，分别为最多 64 位字母/数字/下划线/连字符标识、预览的 SHA256。
差异 action 为 add / remove / replace，path 为转义的 JSON Pointer（数组使用下标）。

界面和真实临时资源操作验证记录位于前端 `docs/resource-yaml/`。
