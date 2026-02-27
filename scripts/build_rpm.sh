#!/bin/bash
# KubeEngine RPM 构建脚本
# 用于将项目编译并打包为 RPM 包（Cython 编译模式）

set -e

PROJECT_NAME="kubengine"
VERSION=${VERSION:-"0.1.0"}
RELEASE=${RELEASE:-"1"}
PYTHON_VERSION="3.11"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 检查依赖
check_dependencies() {
    log_info "检查构建依赖..."

    # 检查 Python
    if ! command -v python${PYTHON_VERSION} &> /dev/null; then
        log_error "Python ${PYTHON_VERSION} 未安装"
        exit 1
    fi

    # 检查 rpmbuild
    if ! command -v rpmbuild &> /dev/null; then
        log_warn "rpmbuild 未安装，尝试安装..."
        if command -v dnf &> /dev/null; then
            sudo dnf install -y rpm-build || {
                log_error "安装 rpm-build 失败"
                exit 1
            }
        elif command -v yum &> /dev/null; then
            sudo yum install -y rpm-build || {
                log_error "安装 rpm-build 失败"
                exit 1
            }
        else
            log_error "无法安装 rpm-build，请手动安装"
            exit 1
        fi
    fi

    # 检查 Cython
    if ! python${PYTHON_VERSION} -c "import Cython" 2>/dev/null; then
        log_info "安装 Cython..."
        python${PYTHON_VERSION} -m pip install 'cython>=3.0.0'
    fi

    # 检查 gcc
    if ! command -v gcc &> /dev/null; then
        log_error "gcc 未安装，请先安装 gcc"
        exit 1
    fi

    log_info "构建依赖检查完成（Cython 编译模式）"
}

# 准备构建目录
prepare_build_dir() {
    log_info "准备构建环境..."

    # 创建 rpmbuild 目录结构
    mkdir -p ~/rpmbuild/{BUILD,RPMS,SOURCES,SPECS,SRPMS}

    # 设置 .rpmmacros
    if ! grep -q "%_topdir" ~/.rpmmacros 2>/dev/null; then
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
        log_info "已创建 ~/.rpmmacros"
    fi
}

# 创建源码包
create_source_tarball() {
    log_info "创建源码包..."

    local tarball_name="${PROJECT_NAME}-${VERSION}.tar.gz"

    # 临时目录
    local tmp_dir=$(mktemp -d)
    trap "rm -rf ${tmp_dir}" EXIT

    # 复制项目文件
    local src_dir="${tmp_dir}/${PROJECT_NAME}-${VERSION}"
    mkdir -p "${src_dir}"

    # 复制必要文件
    rsync -av \
        --exclude='*.pyc' \
        --exclude='__pycache__' \
        --exclude='.git' \
        --exclude='.gitignore' \
        --exclude='.pytest_cache' \
        --exclude='*.egg-info' \
        --exclude='.mypy_cache' \
        --exclude='build/' \
        --exclude='dist/' \
        --exclude='*.log' \
        --exclude='logs/' \
        --exclude='*.db' \
        --exclude='.venv' \
        --exclude='venv/' \
        --exclude='env/' \
        --exclude='.continue/' \
        --exclude='.vscode/' \
        ./ "${src_dir}/"

    # 创建 tarball
    tar -czf "${tarball_name}" -C "${tmp_dir}" "${PROJECT_NAME}-${VERSION}"

    # 移动到 SOURCES 目录
    mv "${tarball_name}" ~/rpmbuild/SOURCES/

    log_info "源码包已创建: ${tarball_name}"
}

# 构建 RPM
build_rpm() {
    log_info "构建 RPM 包..."

    # 复制 spec 文件
    cp ${PROJECT_NAME}.spec ~/rpmbuild/SPECS/

    # 构建
    rpmbuild -ba ~/rpmbuild/SPECS/${PROJECT_NAME}.spec

    log_info "RPM 构建完成！"
    log_info "RPM 包位置:"
    find ~/rpmbuild/RPMS -name "${PROJECT_NAME}-*.rpm" -exec ls -lh {} \;
}

# 清理
cleanup() {
    log_info "清理临时文件..."
    # 保留构建的文件，仅清理临时目录
}

# 主流程
main() {
    log_info "开始构建 ${PROJECT_NAME} RPM 包..."

    check_dependencies
    prepare_build_dir
    create_source_tarball
    build_rpm
    cleanup

    log_info "构建完成！"
}

# 运行主流程
main "$@"
