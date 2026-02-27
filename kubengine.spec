# RPM spec 文件用于 KubeEngine 项目
# 使用方法（Cython 编译模式）：
#   1. 准备源码包: tar -czf kubengine-0.1.0.tar.gz --exclude='*.pyc' --exclude='__pycache__' .
#   2. 移动源码包: mv kubengine-0.1.0.tar.gz ~/rpmbuild/SOURCES/
#   3. 复制 spec 文件: cp kubengine.spec ~/rpmbuild/SPECS/
#   4. 构建 RPM: rpmbuild -ba ~/rpmbuild/SPECS/kubengine.spec
#
# 注意：使用 Cython 编译模式，将 Python 代码编译为 C 扩展以提升性能和保护源码

%global project_name kubengine
%global version 0.1.0
%global release 1
%global python311 python3.11
%global kubengine_dir /opt/%{project_name}

Name:           %{project_name}
Version:        %{version}
Release:        %{release}%{?dist}
Summary:        Kubernetes management platform for Kylin OS
License:        Apache-2.0
URL:            https://github.com/kubengine/kubengine
Source0:        %{project_name}-%{version}.tar.gz

# 禁用 debuginfo 包（Python 包不需要调试符号）
%global debug_package %{nil}

# 禁用 brp-rpath 检查（Python Cython 扩展不需要 rpath）
%define __brp_rpath %{nil}

# 禁用自动依赖检测（Python 依赖通过 pip 安装）
AutoReq: no

# 系统要求（Cython 编译模式）
BuildRequires:  python3-devel >= 3.11.0
BuildRequires:  python3-setuptools
BuildRequires:  python3-pip
BuildRequires:  gcc
BuildRequires:  systemd

Requires:       python3.11

# 描述信息
%description
KubeEngine is an enterprise-grade container cloud platform optimized for Kylin Server V11.
It provides comprehensive Kubernetes cluster management, automated deployment, and visual
operation interface for cloud-native applications in the domestic ecosystem.

%prep
%setup -n %{project_name}-%{version}

%build
# 清理环境
unset PYTHONPATH
export PATH=/usr/bin:/usr/local/bin

# 安装 Cython（通过 pip，兼容性更好）
%{python311} -m pip install --no-cache-dir --user 'cython>=3.0.0' 2>/dev/null || true

# 暂时重命名 pyproject.toml 以禁用现代构建后端，使 setup.py 的 cmdclass 生效
mv pyproject.toml pyproject.toml.bak

# 使用 setup.py 编译 Python C 扩展
CFLAGS="%{optflags}" %{python311} setup.py build

# 恢复 pyproject.toml（用于 %files 阶段检查等）
mv pyproject.toml.bak pyproject.toml

%install
rm -rf %{buildroot}

# 清理环境
unset PYTHONPATH
export PATH=/usr/bin:/usr/local/bin

# 确保 Cython 已安装
%{python311} -m pip install --no-cache-dir --user 'cython>=3.0.0' 2>/dev/null || true

# 暂时重命名 pyproject.toml 以禁用现代构建后端，使 setup.py 的 cmdclass 生效
mv pyproject.toml pyproject.toml.bak

# 使用 setup.py 直接安装到系统 Python 3.11 的 site-packages
CFLAGS="%{optflags}" %{python311} setup.py install --root %{buildroot} --prefix /usr

# 恢复 pyproject.toml（用于 %files 阶段检查等）
mv pyproject.toml.bak pyproject.toml

# 清理不需要的文件 - 只保留编译后的 .so 文件和必要的 Python 文件
# 删除 Cython 生成的 C 代码文件
find %{buildroot}/usr/lib64/python3.11/site-packages -name "*.c" -type f -delete

# 删除已编译模块对应的原始 Python 源文件（保留 __init__.py 和未编译的模块）
# 对于已编译为 .so 的模块，删除对应的 .py 文件
for so_file in $(find %{buildroot}/usr/lib64/python3.11/site-packages -name "*.so"); do
    # 找到对应的 .py 文件
    py_file="${so_file%.cpython-311-x86_64-linux-gnu.so}.py"
    if [ -f "$py_file" ] && [ "$(basename "$py_file")" != "__init__.py" ]; then
        rm -f "$py_file"
    fi
done

# 创建目录结构
mkdir -p %{buildroot}%{kubengine_dir}
mkdir -p %{buildroot}%{kubengine_dir}/config
mkdir -p %{buildroot}%{_localstatedir}/lib/%{project_name}
mkdir -p %{buildroot}%{_localstatedir}/log/%{project_name}
mkdir -p %{buildroot}%{_unitdir}

# 复制配置文件到 /opt/kubengine/config（如果存在）
if [ -f config/application.yaml ]; then
    install -p -D -m 644 config/application.yaml %{buildroot}%{kubengine_dir}/config/application.yaml
fi

# 静态文件已通过 Python 包自动打包（src/web/static）
# 此处保留用于根目录的额外静态资源（如徽章、logo）
if [ -d static ]; then
    mkdir -p %{buildroot}%{kubengine_dir}/static
    cp -a static/* %{buildroot}%{kubengine_dir}/static/ 2>/dev/null || true
fi

# 创建 systemd 服务文件（使用系统 Python 3.11）
cat > %{buildroot}%{_unitdir}/kubengine-api.service << 'EOF'
[Unit]
Description=KubeEngine API Service
After=network.target

[Service]
Type=simple
User=root
Environment="PATH=/usr/bin:/usr/local/bin"
ExecStart=kubengine app run
ExecReload=/bin/kill -HUP $MAINPID
KillMode=process-group
TimeoutStopSec=10
KillSignal=SIGTERM
FinalKillSignal=SIGKILL
RemainAfterExit=no
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# 设置权限
chmod 755 %{buildroot}%{kubengine_dir}
chmod 644 %{buildroot}%{kubengine_dir}/config/application.yaml

%files
%doc README.md
%license LICENSE.txt
%{_bindir}/kubengine
%{_bindir}/kubengine_k8s
/usr/lib64/python3.11/site-packages/builder
/usr/lib64/python3.11/site-packages/cli
/usr/lib64/python3.11/site-packages/core
/usr/lib64/python3.11/site-packages/infra
/usr/lib64/python3.11/site-packages/web
/usr/lib64/python3.11/site-packages/kubengine-0.1.0-py3.11.egg-info
%dir /opt/kubengine
%dir /opt/kubengine/config
%config(noreplace) /opt/kubengine/config/application.yaml
/opt/kubengine/static/*
%{_unitdir}/kubengine-api.service
%dir %attr(0755,root,root) %{_localstatedir}/lib/%{project_name}
%dir %attr(0755,root,root) %{_localstatedir}/log/%{project_name}

%post
echo "=========================================="
echo "Installing Python dependencies"
echo "=========================================="

# 使用系统 pip 安装依赖到全局 Python 3.11
%{python311} -m pip install --no-cache-dir --quiet \
    'fastapi>=0.121.3' \
    'uvicorn[standard]>=0.38.0' \
    'sqlalchemy>=2.0.45' \
    'kubernetes>=34.1.0' \
    'asyncssh>=2.21.1' \
    'pyinfra>=3.5.1' \
    'requests>=2.32.5' \
    'websockets>=15.0.1' \
    'python-multipart>=0.0.20' \
    'rich>=14.3.1' \
    'PyYAML>=6.0.3' \
    'toml>=0.10.2' \
    'jwt>=1.4.0' \
    'click>=8.0.0' \
    2>/dev/null || true

echo "Installation completed"
echo "=========================================="

# 初始化数据库
if [ ! -f %{_localstatedir}/lib/%{project_name}/kubekylin.db ]; then
    %{python311} -m cli.app init-data 2>/dev/null || true
fi

# 重新加载 systemd
systemctl daemon-reload &>/dev/null || true

%postun
if [ $1 -eq 1 ]; then
    # 卸载后重启服务
    systemctl try-restart kubengine-api.service &>/dev/null || true
fi

%changelog
* %{lua: print(os.date("%a %b %d %Y"))} duanzt <duanziteng@gmail.com> - %{version}-%{release}
- Initial RPM release for KubeEngine
- Support for Kylin Server V11 (x86_64)
- Automated Kubernetes cluster deployment
- Web-based management interface
