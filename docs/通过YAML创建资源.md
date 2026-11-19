# 通过 YAML 创建 Kubernetes 资源

在「资源中心」点击「通过 YAML 创建」，上传 UTF-8 编码的 `.yaml` / `.yml`
文件，或直接粘贴资源清单。支持单资源、`---` 分隔的多文档以及 Kubernetes `List`，
每批最多 20 个资源，YAML 总大小不超过 2 MiB。

1. 选择默认命名空间。清单中的 `metadata.namespace` 优先；集群级资源不使用此默认值。
2. 可填写创建说明，随后点击「集群预检」。后台根据 `apiVersion` / `kind` 发现集群 API，
   使用 Kubernetes SDK 的 `dryRun=All`、`fieldValidation=Strict` 检查创建请求。
3. 检查每一项结果；可展开查看服务器默认化后的 YAML，Secret 值保持隐藏。
4. 预检通过后点击「创建资源」。逐项结果包含创建成功、失败或结果待核实。
   已创建的资源可点击「查看资源」，并在版本历史中看到初始版本。

创建仅使用 POST，不覆盖已有资源。同名资源返回冲突，可进入已有资源详情编辑 YAML。
支持 Kubernetes API 发现中允许 create 的资源，包括已安装 CRD 的自定义资源；
自定义类型当前没有资源中心详情入口，其创建记录仍保存在加密版本表中。

清单必须提供明确的 `metadata.name`，不支持 `generateName`。导出的 YAML 请先移除
`status`、UID、resourceVersion 等服务端字段。默认命名空间与单个资源的命名空间均需合法。

## 批量结果与重试

多资源创建不是事务，不会撤销已经成功创建的资源。预检有失败项时不能提交。
若清单中包含新的 Namespace，后台优先创建它；依赖该命名空间的项目在提交阶段再进行
服务端预检，并明确标记为等待命名空间。CRD 应先单独创建，待 API 注册就绪后再创建其实例。

预检凭证有效期 10 分钟，绑定操作者、集群、清单、默认命名空间与创建说明，修改任何内容
都需重新预检。创建请求携带 `request_id`，后端先在 SQLite 中预留记录再调用集群，
相同请求的并发或重复提交返回原有结果，成功、失败和未完成的项目都不会自动重放。

网络中断可能发生在集群已接受创建之后，因此返回“结果待核实”时，应先查看集群中的资源。
不要直接以新的请求标识重复创建。后端会保留 pending / unknown 记录；明确成功后按新资源 UID
记录初始版本，快照使用现有 `resource-history.key` 加密。Secret 错误不回显提交值。

## 权限

控制台登录并不替代 Kubernetes RBAC。服务端连接凭据需要 API 发现的读取权限，
以及目标资源的 `create` 权限（dry-run 也检查创建权限）。版本历史读取还需要资源 `get` 权限。
可参考 `kubernetes-yaml-creator-rbac.yaml`，按需缩小到命名空间 Role，或补充自定义资源规则。
权限示例仅供管理员手动配置，应用程序不会自动授予权限。

## API

所有接口要求 Bearer 身份，沿用 `{code, message, data, request_id}` 标准响应：

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| POST | `/api/v1/k8s/resources/preview` | YAML 批量创建预检 |
| POST | `/api/v1/k8s/resources/create` | 执行预检后的创建 |

预检 body：`{yaml_text, namespace?: "default", comment?: ""}`。
返回 `data: {items, valid, previewToken}`；`valid: false` 时凭证为空。
创建额外提交 `{preview_token, request_id}`，返回 `{items, created, total, complete}`。
批量检查/执行完成时 HTTP 为 200，逐项结果以 `items[].status` 表示；
语法、大小、凭证、认证等导致整批无法处理时使用标准 4xx / 5xx 错误响应。

每个结果包含 `index, apiVersion, kind, name, namespace, status, message?`。
预检状态为 `validated / deferred / failed`；提交状态为 `created / failed / unknown`。
成功提交结果另含 `revisionId, version, uid`，重复请求标记 `replayed: true`。
