# KubeEngine pip 安装指南

本指南说明如何使用 pip 从 wheel 包或源码包安装 KubeEngine。

## 目录

- [快速开始](#快速开始)
- [构建 pip 包](#构建-pip-包)
- [安装方式](#安装方式)
- [验证安装](#验证安装)
- [卸载](#卸载)
- [与 RPM 安装的区别](#与-rpm-安装的区别)

## 快速开始

### 方式一：从预构建的 wheel 包安装（推荐）

```bash
# 安装 wheel 包
pip install dist/kubengine-0.1.0-py3-none-any.whl

# 或从远程 URL 安装
pip install https://example.com/packages/kubengine-0.1.0-py3-none-any.whl
```

### 方式二：从源码包安装

```bash
# 安装源码包（会自动构建）
pip install dist/kubengine-0.1.0.tar.gz
```

### 方式三：开发模式安装

```bash
# 从源码目录安装（可编辑模式，代码修改立即生效）
pip install -e .
```

## 构建 pip 包

### 前置要求

- Python 3.11+
- pip 和 setuptools

### 安装构建工具

```bash
pip install --upgrade build wheel setuptools twine
```

### 使用构建脚本（推荐）

```bash
# 运行构建脚本
./scripts/build_pip.sh
```

构建完成后，在 `dist/` 目录下会生成：
- `kubengine-0.1.0-py3-none-any.whl` - Wheel 包
- `kubengine-0.1.0.tar.gz` - 源码包

### 手动构建

```bash
# 清理旧构建
rm -rf build/ dist/ *.egg-info

# 构建包
python -m build

# 或使用 setup.py（传统方式）
python setup.py sdist bdist_wheel
```

## 安装方式

### 1. 标准安装

```bash
# 安装到系统 Python
pip install kubengine-0.1.0-py3-none-any.whl

# 或指定安装路径
pip install --target /opt/kubengine/lib kubengine-0.1.0-py3-none-any.whl
```

### 2. 虚拟环境安装（推荐）

```bash
# 创建虚拟环境
python3.11 -m venv /opt/kubengine/venv

# 激活虚拟环境
source /opt/kubengine/venv/bin/activate

# 安装包
pip install dist/kubengine-0.1.0-py3-none-any.whl
```

### 3. 用户级安装

```bash
# 安装到用户目录（不需要 sudo）
pip install --user kubengine-0.1.0-py3-none-any.whl
```

### 4. 带依赖的安装

```bash
# 安装主包 + 开发依赖
pip install "kubengine[dev]"

# 安装主包 + Cython 依赖
pip install "kubengine[cython]"
```

### 5. 离线安装

```bash
# 下载包和依赖到目录
pip download -d ./packages kubengine

# 在离线环境安装
pip install --no-index --find-links=./packages kubengine
```

## 安装后的文件结构

```
~/.local/lib/python3.11/site-packages/       # 用户级安装
/usr/lib/python3.11/site-packages/            # 系统级安装
/opt/kubengine/venv/lib/python3.11/site-packages/  # 虚拟环境安装
└── kubengine/                                # Python 包
    ├── __init__.py
    ├── cli/
    ├── core/
    ├── web/
    └── ...

~/.local/bin/                                 # CLI 工具位置
├── kubengine                                 # 主命令行工具
└── kubengine_k8s                             # Kubernetes 部署专用工具
```

## 配置文件说明

pip 安装后需要手动创建配置文件。KubeEngine 支持灵活的配置文件路径：

**配置文件查找顺序（优先级从高到低）**：

1. **环境变量** `KUBEENGINE_CONFIG` - 指定任意路径的配置文件
2. **RPM 安装路径** `/etc/kubengine/application.yaml` - 生产环境
3. **开发环境路径** `/opt/kubengine/config/application.yaml` - 开发调试
4. **相对路径** `./config/application.yaml` - 本地开发

### 创建配置文件

```bash
# 创建配置目录（如果使用 RPM 路径）
sudo mkdir -p /etc/kubengine

# 从源码复制配置模板
sudo cp /path/to/kubengine/config/application.yaml /etc/kubengine/

# 或创建自定义配置文件
cat > /etc/kubengine/application.yaml << 'EOF'
root_dir: /opt/kubengine
domain: kubengine.io
tls:
  root_dir: /opt/kubengine/config/certs
auth:
  jwt:
    algorithm: HS256
  token:
    expire_minutes: 30
    renew_threshold_minutes: 5
  users:
    admin:
      password_hash: $2b$12$...
      ak: AKXXXXXXXX
      sk_hash: $2b$12$...
EOF

# 设置权限
sudo chmod 644 /etc/kubengine/application.yaml
```

### 使用环境变量指定配置

```bash
# 临时指定配置文件
export KUBEENGINE_CONFIG=/path/to/custom/config.yaml
kubengine app run

# 永久设置（添加到 ~/.bashrc 或 /etc/environment）
echo 'export KUBEENGINE_CONFIG=/etc/kubengine/application.yaml' >> ~/.bashrc
```

## 验证安装

### 1. 检查包信息

```bash
# 查看已安装的包
pip show kubengine

# 输出示例：
# Name: kubengine
# Version: 0.1.0
# Location: /usr/lib/python3.11/site-packages
```

### 2. 测试 CLI 工具

```bash
# 查看主命令帮助
kubengine --help

# 测试应用管理命令
kubengine app --help

# 测试集群管理命令
kubengine cluster configure-cluster --help
kubengine cluster execute-cmd --help

# 测试镜像构建命令
kubengine image build --help
kubengine image list-apps --help

# 测试 Kubernetes 部署专用命令
kubengine_k8s --help
kubengine_k8s deploy --help
kubengine_k8s config --help
kubengine_k8s reset-state --help
```

### 3. 测试导入

```bash
python3.11 -c "from cli.app import cli; print('导入成功')"
```

### 4. 验证依赖

```bash
# 检查所有依赖是否安装
pip check

# 查看依赖树
pip install pipdeptree
pipdeptree -p kubengine
```

## 卸载

```bash
# 卸载包
pip uninstall kubengine

# 清理缓存
pip cache purge

# 如果是虚拟环境安装，直接删除虚拟环境
rm -rf /opt/kubengine/venv
```

## 升级

```bash
# 从新版本包升级
pip install --upgrade kubengine-0.2.0-py3-none-any.whl

# 或强制重装
pip install --upgrade --force-reinstall kubengine-0.1.0-py3-none-any.whl
```

## 发布到 PyPI

### 1. 注册 PyPI 账号

访问 https://pypi.org/account/register/ 注册账号

### 2. 安装发布工具

```bash
pip install twine
```

### 3. 配置认证

创建 `~/.pypirc`：

```ini
[pypi]
username = __token__
password = pypi-xxxxxx...
```

或使用环境变量：

```bash
export TWINE_USERNAME=__token__
export TWINE_PASSWORD=pypi-xxxxxx...
```

### 4. 构建并上传

```bash
# 构建包
./scripts/build_pip.sh

# 检查包
twine check dist/*

# 上传到 PyPI（生产环境）
twine upload dist/*

# 或上传到 TestPyPI（测试环境）
twine upload --repository testpypi dist/*
```

### 5. 从 PyPI 安装

```bash
# 从 PyPI 安装
pip install kubengine

# 从 TestPyPI 安装
pip install --index-url https://test.pypi.org/simple/ kubengine
```

## 与 RPM 安装的区别

| 特性 | pip 安装 | RPM 安装 |
|------|---------|----------|
| **安装位置** | site-packages 或虚拟环境 | `/usr/lib/python3.11/site-packages/` |
| **配置文件** | 需手动创建 | `/etc/kubengine/` |
| **数据库** | 需手动初始化 | `/var/lib/kubengine/` |
| **日志** | 需手动配置 | `/var/log/kubengine/` |
| **systemd 服务** | 需手动配置 | 自动配置 |
| **依赖管理** | pip 自动处理 | RPM/dnf 处理 |
| **升级回滚** | pip install --upgrade | rpm/dnf 升级 |
| **适用场景** | 开发环境、虚拟环境 | 生产环境、系统集成 |

## 配置 systemd 服务（pip 安装后）

pip 安装后需要手动配置 systemd 服务：

```bash
# 创建服务文件
sudo cat > /etc/systemd/system/kubengine-api.service << 'EOF'
[Unit]
Description=KubeEngine API Server
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/var/lib/kubengine
Environment="PATH=/opt/kubengine/venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=/opt/kubengine/venv/bin/kubengine app run --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# 重载 systemd
sudo systemctl daemon-reload

# 启动服务
sudo systemctl start kubengine-api
sudo systemctl enable kubengine-api
```

## 常见问题

### Q1: 找不到 kubengine 命令

**原因**: PATH 未包含安装目录

**解决**:
```bash
# 如果是用户级安装，添加到 PATH
export PATH="$HOME/.local/bin:$PATH"

# 或使用完整路径
~/.local/bin/kubengine --help
```

### Q2: 权限错误

**解决**:
```bash
# 使用虚拟环境（推荐）
python3.11 -m venv /opt/kubengine/venv
source /opt/kubengine/venv/bin/activate
pip install dist/kubengine-0.1.0-py3-none-any.whl
```

### Q3: 依赖冲突

**解决**:
```bash
# 创建独立的虚拟环境
python3.11 -m venv /opt/kubengine/venv
source /opt/kubengine/venv/bin/activate
pip install --upgrade pip setuptools
pip install dist/kubengine-0.1.0-py3-none-any.whl
```

### Q4: 找不到配置文件

**解决**:
```bash
# 创建配置目录
sudo mkdir -p /etc/kubengine

# 复制默认配置
sudo cp config/application.yaml /etc/kubengine/

# 创建必要目录
sudo mkdir -p /var/lib/kubengine
sudo mkdir -p /var/log/kubengine
```

## 参考资料

- [Python 打包指南](https://packaging.python.org/)
- [pip 文档](https://pip.pypa.io/)
- [PyPI 发布指南](https://pypi.org/help/)
