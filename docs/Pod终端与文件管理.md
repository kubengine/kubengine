# Pod 终端与文件管理

资源中心的 Pod 详情新增「终端」和「文件管理」标签页。旧 Pod 详情页也提供这两个入口。
操作前可选择容器；主容器、运行中的 Init / 临时容器均可选择，已停止的容器无法 exec。
所有命令和文件操作都在所选容器内，以容器自身的运行身份执行。

## 交互终端

- 支持 Bash、sh 和 PowerShell，点击「连接终端」建立会话，支持持续交互输入、粘贴、Ctrl+C、清屏和窗口尺寸调整。
- Linux PowerShell 使用 `pwsh`；Windows 使用 `powershell.exe`，需对应程序已安装在容器镜像中。缺少 Shell 时显示明确提示，不自动修改镜像。
- 使用本地打包的 xterm.js，没有运行时 CDN。终端输出不作为 HTML 执行。
- WebSocket 以首条 JSON 消息鉴权，JWT 不放在 URL 中。重新登录/退出后更新或关闭会话，服务端检查会话撤销和有效期。
- 手动断开、退出标签页、切换容器会关闭当前 exec。无输入 10 分钟或连接满 1 小时后需重新连接；断线不自动重启 Shell。

## 文件管理

支持绝对路径导航、上级目录、文件名称筛选、分页，显示类型、大小、修改时间与权限/属性。
Linux 默认从 `/` 浏览，Windows 默认从 `C:\` 浏览。

| 操作 | 行为 |
| --- | --- |
| 新建文件 | 创建空文件，已有同名目标时拒绝 |
| 新建文件夹 | 在当前目录创建子目录 |
| 上传 | 支持多文件顺序上传，大小上限读取后端配置（默认 100 MiB）；默认不覆盖，可显式勾选覆盖同名文件 |
| 下载 | 下载普通文件，保持二进制字节，大小上限与上传一致 |
| 重命名 | 修改当前目录中的名称，不覆盖已有目标 |
| 删除文件 | 确认后删除指定文件 |
| 删除文件夹 | 默认只允许空目录；勾选「递归删除文件夹及全部内容」后删除非空目录 |
| 符号链接 | 展示或删除链接本身；下载、覆盖需使用目标实际路径；递归删除不跟随目录内部的链接 |

文件上传先写同目录临时文件，完整校验后安装到目标名称，传输失败清理临时文件。
服务端拒绝空路径、根目录修改和非法新名称；目录一次最多返回 2000 项，超过限制提示进入更具体的子目录。

Linux 文件管理需要容器具备 `sh`、`stat`、`base64`、`head`、`mktemp` 及常用文件命令（GNU / 当前 BusyBox）。
Windows 文件管理通过 PowerShell 与 .NET 文件 API 执行。只读挂载或容器无权限时保留实际错误。
极简 distroless 镜像不含这些工具时，不能直接使用 exec 文件管理。

## 文件传输配置

在实际使用的 `config/application.yaml` 中配置（也可通过 `KUBEENGINE_CONFIG` 指定配置文件）：

```yaml
security:
  uploads:
    pod_file_bytes: 104857600  # 单文件上传/下载上限，字节，默认 100 MiB
    pod_timeout_seconds: 300  # 每段传输超时，秒，默认 300
```

两项均须为正整数；未配置时使用上述默认值。修改后重启 `kubengine-api`，刷新文件管理页面生效。
例如 1 GiB 对应 `pod_file_bytes: 1073741824`，30 分钟对应 `pod_timeout_seconds: 1800`。
合并到已有 `security.uploads` 节点，保留其他配置，不要重复创建同名 YAML 节点。

大小限制同时用于 HTTP 上传接收、容器上传、下载前检查和下载过程检查。
`pod_timeout_seconds` 分别限制浏览器到 API 的文件接收、API 到容器的写入以及容器文件下载；
上传的两个阶段各自计时。目录接口返回 `maxFileBytes` 和 `transferTimeoutSeconds`，前端据此
显示限制、校验大小和设置请求超时（上传为两段超时之和加 60 秒，下载为一段超时加 60 秒）。
Pod 文件接收不再使用镜像上传的 `timeout_seconds`，并发与磁盘预留仍共用 `concurrency` / `reserve_bytes`。

## 身份与权限

终端及文件 API 均要求 KubeEngine 现有 Bearer 登录身份。文件上传在 multipart 解析前鉴权并限制输入大小。
上传并发沿用现有上传槽位，文件接口不绕过全局并发限制。

Kubernetes 身份还需读取 Pod/Node，并获得 `pods/exec` 的 `get/create` 权限。
只读角色本身不增加写权限；按需参考独立的
[kubernetes-pod-operations-rbac.yaml](kubernetes-pod-operations-rbac.yaml)。部署到当前环境时沿用现有 kubeconfig，未自动应用此 RBAC 清单。

API 见 [接口文档](API.md#pod-终端与文件管理接口)。

## 验证边界

真实 Linux Pod 中验证了 sh / Bash 交互输入和输出、目录/文件创建、特殊文件名、二进制上传下载的 SHA256、
重命名、删除、非空目录确认及临时文件清理。另验证了容器未安装 pwsh 时的错误处理。

现场集群为 Linux，Windows / PowerShell 文件脚本提供编码、参数与路径分支测试，尚无 Windows Pod 实机验证。
浏览器截图与操作验证记录位于前端 `docs/pod-operations/`。
