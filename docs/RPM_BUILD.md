# KubeEngine RPM 构建指南

本指南说明如何将 KubeEngine 项目编译并打包为 RPM 包，适用于 Kylin Server V11 系统。

## 目录

- [方案概述](#方案概述)
- [环境准备](#环境准备)
- [快速构建](#快速构建)
- [详细说明](#详细说明)
- [验证安装](#验证安装)
- [常见问题](#常见问题)

## 方案概述

KubeEngine RPM 使用 **Cython 编译模式**：

**安装方式**：使用 Cython 将 Python 代码编译为 C 扩展（`.so` 文件）
**适合场景**：生产环境、性能优化、代码保护
**优点**：
- 提升执行效率 2-10 倍
- 保护源代码，防止反编译
- 编译后的二进制文件更难篡改

```
┌─────────────────────────────────────────────────────────────┐
│                    RPM 构建流程（Cython 编译模式）              │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  1. 源码准备                                                 │
│     ┌──────────────────────────────────────────────┐        │
│     │  复制项目文件到临时目录                        │        │
│     │  排除: __pycache__, *.pyc, .git, logs 等       │        │
│     └──────────────────────────────────────────────┘        │
│                          ↓                                  │
│  2. 创建 tar.gz 源码包                                       │
│     ┌──────────────────────────────────────────────┐        │
│     │  tar -czf kubengine-0.1.0.tar.gz               │        │
│     │  放置: ~/rpmbuild/SOURCES/                      │        │
│     └──────────────────────────────────────────────┘        │
│                          ↓                                  │
│  3. rpmbuild 构建                                            │
│     ┌──────────────────────────────────────────────┐        │
│     │  rpmbuild -ba kubengine.spec                   │        │
│     │  ├─ 解压源码                                     │        │
│     │  ├─ 安装 Cython 和编译依赖                       │        │
│     │  ├─ setup.py build (编译为 C 扩展)              │        │
│     │  ├─ setup.py install                           │        │
│     │  ├─ 复制配置文件                                 │        │
│     │  ├─ 创建 systemd 服务                          │        │
│     │  └─ 打包为 RPM                                  │        │
│     └──────────────────────────────────────────────┘        │
│                          ↓                                  │
│  4. RPM 输出                                                │
│     ~/rpmbuild/RPMS/x86_64/kubengine-0.1.0-1.el8.x86_64.rpm   │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## 环境准备

### 系统要求
- **操作系统**: Kylin Server V11 (x86_64)
- **Python**: 3.11+
- **磁盘空间**: 至少 2GB 可用空间

### 安装构建依赖

```bash
# 安装构建工具
sudo dnf install -y rpm-build

# 安装 Python 3.11 和开发工具
sudo dnf install -y python311 python311-devel python311-pip gcc systemd

# 安装 Cython（用于编译 Python 代码为 C 扩展）
python3.11 -m pip install 'cython>=3.0.0'
```

**注意**：Cython 编译需要 gcc 编译器，用于将生成的 C 代码编译为 `.so` 共享库。

### 配置 rpmbuild

首次使用需要配置 rpmbuild 目录结构：

```bash
# 创建 .rpmmacros 文件
cat > ~/.rpmmacros << 'EOF'
%_topdir %(echo $HOME)/rpmbuild
%_builddir %{_topdir}/BUILD
%_rpmdir %{_topdir}/RPMS
%_sourcedir %{_topdir}/SOURCES
%_specdir %{_topdir}/SPECS
%_srcrpmdir %{_topdir}/SRPMS
%buildrootdir %{_topdir}/BUILDROOT
%_tmppath %{_topdir}/TMP
%_make_install_cmd  install
EOF

# 创建目录结构
mkdir -p ~/rpmbuild/{BUILD,RPMS,SOURCES,SPECS,SRPMS}
```

## 快速构建

### 方法一：使用构建脚本（推荐）

```bash
# 进入项目目录
cd /opt/kubengine

# 运行构建脚本
./scripts/build_rpm.sh
```

### 方法二：手动构建

```bash
# 1. 准备源码包
tar -czf kubengine-0.1.0.tar.gz \
    --exclude='*.pyc' \
    --exclude='__pycache__' \
    --exclude='.git' \
    --exclude='.pytest_cache' \
    --exclude='*.egg-info' \
    --exclude='.mypy_cache' \
    --exclude='build/' \
    --exclude='dist/' \
    --exclude='*.log' \
    --exclude='logs/' \
    --exclude='*.db' \
    .

# 2. 移动源码包
mv kubengine-0.1.0.tar.gz ~/rpmbuild/SOURCES/

# 3. 复制 spec 文件
cp kubengine.spec ~/rpmbuild/SPECS/

# 4. 构建 RPM
rpmbuild -ba ~/rpmbuild/SPECS/kubengine.spec

# 5. 查找生成的 RPM
find ~/rpmbuild/RPMS -name "kubengine-*.rpm"
```

## 详细说明

### 项目文件

| 文件 | 说明 |
|------|------|
| [`kubengine.spec`](kubengine.spec) | RPM spec 文件，定义打包规则 |
| [`setup.py`](setup.py) | Cython 编译配置（当前使用） |
| [`setup_install.py`](setup_install.py) | 标准安装配置（备用） |
| [`scripts/cython_compile.py`](scripts/cython_compile.py) | 独立 Cython 编译脚本 |
| [`scripts/build_rpm.sh`](scripts/build_rpm.sh) | RPM 构建脚本 |

### Spec 文件配置

`kubengine.spec` 主要配置项：

```spec
Name:           kubengine
Version:        0.1.0
Release:        1%{?dist}
BuildRequires:  python311-devel, python311-pip, gcc, systemd
Requires:       python311, python311-pip, sqlite
```

**构建过程**：
1. `%build` 阶段：通过 pip 安装 Cython，然后执行 `setup.py build` 编译
2. `%install` 阶段：执行 `setup.py install` 安装编译后的 `.so` 文件
3. `%post` 阶段：通过 pip 安装运行时依赖

**版本管理**: 修改 `VERSION` 环境变量或直接修改 spec 文件中的版本号。

### 安装 RPM 后的文件结构

```
/usr/
├── bin/
│   ├── kubengine                          # 主 CLI 工具
│   └── kubengine_k8s                      # Kubernetes 部署专用 CLI 工具
├── lib/python3.11/site-packages/
│   └── kubengine/                         # Python 包
└── share/kubengine/
    └── static/                             # 静态资源

/etc/kubengine/
└── application.yaml                        # 配置文件

/var/lib/kubengine/
└── kubekylin.db                           # SQLite 数据库

/var/log/kubengine/
└── *.log                                  # 日志文件

/usr/lib/systemd/system/
└── kubengine-api.service                   # systemd 服务
```

### 配置文件说明

KubeEngine 支持灵活的配置文件路径，自动适配多种安装方式：

**配置文件查找顺序（优先级从高到低）**：

1. **环境变量** `KUBEENGINE_CONFIG` - 指定任意路径的配置文件
2. **RPM 安装路径** `/etc/kubengine/application.yaml` - 生产环境
3. **开发环境路径** `/opt/kubengine/config/application.yaml` - 开发调试
4. **相对路径** `./config/application.yaml` - 本地开发

**示例**：
```bash
# RPM 安装后，配置文件位于 /etc/kubengine/
cat /etc/kubengine/application.yaml

# 开发环境，配置文件位于项目目录
cat /opt/kubengine/config/application.yaml

# 使用环境变量指定自定义配置文件
export KUBEENGINE_CONFIG=/path/to/custom/config.yaml
kubengine app run
```

**注意**：RPM 升级时，配置文件使用 `%config(noreplace)` 标记，不会被覆盖。用户修改的配置会保留为 `.rpmnew` 或 `.rpmsave` 文件。

## 验证安装

### 1. 检查 RPM 包内容

```bash
# 查看 RPM 包信息
rpm -qpi ~/rpmbuild/RPMS/x86_64/kubengine-0.1.0-1.el8.x86_64.rpm

# 查看 RPM 包包含的文件
rpm -qpl ~/rpmbuild/RPMS/x86_64/kubengine-0.1.0-1.el8.x86_64.rpm
```

### 2. 安装 RPM

```bash
# 安装 RPM 包
sudo rpm -ivh ~/rpmbuild/RPMS/x86_64/kubengine-0.1.0-1.el8.x86_64.rpm

# 或使用 dnf/yum
sudo dnf install -y ~/rpmbuild/RPMS/x86_64/kubengine-0.1.0-1.el8.x86_64.rpm
```

### 3. 验证服务

```bash
# 检查服务状态
systemctl status kubengine-api

# 启动服务
systemctl start kubengine-api

# 设置开机自启
systemctl enable kubengine-api

# 查看服务日志
journalctl -u kubengine-api -f
```

### 4. 验证功能

```bash
# 测试 CLI 工具（注意：命令名称已改为 kubengine）
kubengine --help

# 测试 API 服务
curl http://localhost:8080/api/v1/health

# 检查数据库
ls -la /var/lib/kubengine/kubekylin.db
```

### 5. 默认管理员账户

| 项目 | 值 |
|------|-----|
| 用户名 | `admin` |
| 默认密码 | `Admin@123` |
| AK（访问密钥 ID） | `AK8F60249C` |
| SK（密钥） | `SK17F1B276797F4957` |

> ⚠️ **安全警告**：生产环境安装后请立即修改默认密码！

**修改管理员密码**：

```bash
kubengine app set-password
```

**使用默认账户登录 API**：

```bash
# 获取访问令牌
curl -X POST "http://localhost:8080/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "admin",
    "password": "Admin@123"
  }'
```

## 常见问题

### Q1: 构建时提示缺少依赖

**错误**: `error: Failed build dependencies`

**解决**:
```bash
# 安装 Python 依赖
sudo dnf install -y python311-fastapi python311-uvicorn ...
```

### Q2: Cython 编译失败

**错误**: `compilation error` 或 `Cython not found`

**解决**:
```bash
# 检查 Cython 是否正确安装
python3.11 -c "import Cython; print(Cython.__version__)"

# 如果未安装或版本过低
python3.11 -m pip install --upgrade 'cython>=3.0.0'

# 检查 gcc 编译器是否可用
gcc --version

# 如果缺少 gcc
sudo dnf install -y gcc

# 清理后重新构建
rm -rf ~/rpmbuild/BUILD/*
./scripts/build_rpm.sh
```

### Q3: RPM 安装后服务无法启动

**错误**: `Failed to start kubengine-api.service`

**解决**:
```bash
# 查看详细日志
journalctl -u kubengine-api -n 50

# 常见问题：
# 1. 数据库权限：chown -R root:root /var/lib/kubengine
# 2. 配置文件：检查 /etc/kubengine/application.yaml
# 3. 端口占用：netstat -tunlp | grep 8080
```

### Q4: Python 依赖冲突

**错误**: `ModuleNotFoundError: No module named 'xxx'`

**解决**:
```bash
# 创建虚拟环境
python3.11 -m venv /opt/kubengine/venv
source /opt/kubengine/venv/bin/activate
pip install -r requirements.txt

# 修改 systemd 服务的 ExecStart
# ExecStart=/opt/kubengine/venv/bin/python -m cli.app run ...
```

### Q5: 卸载 RPM 后残留文件

**解决**:
```bash
# 卸载 RPM
sudo dnf remove kubengine

# 手动清理残留文件
sudo rm -rf /etc/kubengine
sudo rm -rf /var/lib/kubengine
sudo rm -rf /var/log/kubengine
sudo rm -rf /usr/share/kubengine
```

## CLI 命令使用

安装 RPM 后，您可以使用两个 CLI 工具：

### 1. 主命令：kubengine

用于应用管理、集群管理和镜像构建：

```bash
# 应用管理
kubengine app run --host 0.0.0.0 --port 8080    # 启动服务
kubengine app set-password                         # 设置管理员密码
kubengine app init-data                             # 初始化数据

# 集群管理
kubengine cluster configure-cluster ...          # 配置集群
kubengine cluster execute-cmd ...                 # 执行命令

# 镜像构建
kubengine image build redis -v 7.2               # 构建镜像
kubengine image list-apps                         # 列出应用
```

### 2. Kubernetes 部署专用命令：kubengine_k8s

专门用于 Kubernetes 部署的独立命令行工具，提供更强大的功能：

```bash
# 查看帮助
kubengine_k8s --help

# 显示配置
kubengine_k8s config --show

# 验证配置
kubengine_k8s config --validate

# 部署 Kubernetes 集群
kubengine_k8s deploy --deploy-src /path/to/offline-files

# 详细输出日志
kubengine_k8s deploy --deploy-src /path/to/offline-files -vvv

# 重置部署状态
kubengine_k8s reset-state --force
```

## 日志配置

### 日志文件位置

| 日志类型 | 文件路径 | 说明 |
|---------|---------|------|
| K8s 部署日志 | `/var/log/kubengine/k8s_cli.log` | K8s 集群部署日志 |
| 镜像构建日志 | `/var/log/kubengine/images_cli.log` | 镜像构建日志 |
| 应用日志 | `/var/log/kubengine/app.log` | 应用运行日志 |

### 查看日志

```bash
# 查看特定日志
tail -f /var/log/kubengine/k8s_cli.log

# 查看所有日志文件
ls -la /var/log/kubengine/

# 使用 journalctl 查看服务日志
journalctl -u kubengine-api -f
```

**注意**: CLI 命令执行时不会在控制台输出日志，所有日志都会记录到对应的日志文件中。

## 进阶配置

### 自定义安装路径

修改 `kubengine.spec` 中的 `Prefix`:

```spec
# 安装到 /opt/kubengine
Prefix: /opt/kubengine

# 更新 systemd 服务文件中的路径
ExecStart=/usr/bin/python3.11 -m cli.app run ...
WorkingDirectory=/opt/kubengine/share/kubengine
```

### 修改服务监听端口

在 `/etc/kubengine/application.yaml` 中添加：

```yaml
server:
  host: 0.0.0.0
  port: 9090  # 修改为其他端口
```

更新 systemd 服务文件：

```bash
ExecStart=/usr/bin/python3.11 -m cli.app run --port 9090
```

### 启用多进程模式

```bash
# 修改服务文件
ExecStart=/usr/bin/python3.11 -m cli.app run --workers 4
```

## 技术细节

### Cython 编译原理

1. **源码分析**: Cython 分析 Python 代码，提取类型信息
2. **C 代码生成**: 将 Python 代码转换为 C 代码
3. **编译扩展**: 使用 GCC 编译为 `.so` 共享库
4. **性能提升**: 编译后的代码运行速度提升 2-10 倍

### RPM 打包优势

1. **依赖管理**: 自动声明和管理 Python 依赖
2. **文件管理**: 统一的文件安装路径标准
3. **系统集成**: systemd 服务、logrotate 日志轮转
4. **版本控制**: 支持升级、回滚、卸载
5. **批量部署**: 支持 dnf/yum 批量安装

## 参考资料

- [RPM Packaging Guide](https://rpm-packaging-guide.github.io/)
- [Cython Documentation](https://cython.readthedocs.io/)
- [Kylin OS 官方文档](http://www.kylinos.cn/)

## 联系方式

如有问题请通过以下方式联系：
- **Email**: duanziteng@gmail.com
- **GitHub**: https://github.com/kubengine/kubengine/issues
