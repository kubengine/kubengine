#!/bin/bash
# KubeEngine pip 包构建脚本
# 构建 wheel (.whl) 和源码包 (tar.gz)

set -e

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 项目根目录
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

# 版本号（从 pyproject.toml 读取）
VERSION=$(grep "^version = " pyproject.toml | sed 's/version = "\(.*\)"/\1/')
PACKAGE_NAME="kubengine"

echo -e "${GREEN}=====================================${NC}"
echo -e "${GREEN}KubeEngine pip 包构建${NC}"
echo -e "${GREEN}=====================================${NC}"
echo -e "项目名称: ${PACKAGE_NAME}"
echo -e "版本号: ${VERSION}"
echo -e "工作目录: ${PROJECT_ROOT}"
echo ""

# 清理旧的构建产物
echo -e "${YELLOW}[1/5] 清理旧的构建产物...${NC}"
rm -rf build/ dist/ *.egg-info src/*.egg-info
find src -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find src -type f -name "*.pyc" -delete 2>/dev/null || true
find src -type f -name "*.so" -delete 2>/dev/null || true
echo -e "${GREEN}✓ 清理完成${NC}"
echo ""

# 检查构建工具
echo -e "${YELLOW}[2/5] 检查构建工具...${NC}"

# 检查 Python 版本
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
PYTHON_MAJOR=$(echo $PYTHON_VERSION | cut -d. -f1)
PYTHON_MINOR=$(echo $PYTHON_VERSION | cut -d. -f2)

if [ "$PYTHON_MAJOR" -lt 3 ] || ([ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 11 ]); then
    echo -e "${RED}✗ 需要 Python 3.11+，当前版本: $PYTHON_VERSION${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Python 版本: $PYTHON_VERSION${NC}"

# 安装构建依赖
echo "安装构建工具..."
pip install --upgrade build wheel setuptools 2>&1 | grep -E "(Successfully|already|Requirement)" || true
echo -e "${GREEN}✓ 构建工具就绪${NC}"
echo ""

# 构建包
echo -e "${YELLOW}[3/5] 构建 Python 包...${NC}"
echo -e "构建模式: ${BUILD_MODE:-标准模式（无Cython编译）}"
echo ""

# 使用现代 Python 构建工具
python -m build

echo ""
echo -e "${GREEN}✓ 构建完成${NC}"
echo ""

# 显示构建结果
echo -e "${YELLOW}[4/5] 构建产物:${NC}"
ls -lh dist/
echo ""

# 验证包
echo -e "${YELLOW}[5/5] 验证包完整性...${NC}"

# 检查 wheel 包
WHL_FILE=$(find dist -name "*.whl" | head -1)
if [ -n "$WHL_FILE" ]; then
    echo -e "${GREEN}✓ Wheel 包: $WHL_FILE${NC}"
    # 使用 twine 验证
    if command -v twine &> /dev/null; then
        twine check "$WHL_FILE" 2>&1 | grep -E "(Checking|WARNING|ERROR)" || true
    fi
else
    echo -e "${YELLOW}⚠ 未找到 wheel 包${NC}"
fi

# 检查源码包
TAR_FILE=$(find dist -name "*.tar.gz" | head -1)
if [ -n "$TAR_FILE" ]; then
    echo -e "${GREEN}✓ 源码包: $TAR_FILE${NC}"
    if command -v twine &> /dev/null; then
        twine check "$TAR_FILE" 2>&1 | grep -E "(Checking|WARNING|ERROR)" || true
    fi
else
    echo -e "${YELLOW}⚠ 未找到源码包${NC}"
fi

echo ""
echo -e "${GREEN}=====================================${NC}"
echo -e "${GREEN}构建完成！${NC}"
echo -e "${GREEN}=====================================${NC}"
echo ""
echo -e "安装方法:"
echo -e "  # 使用 pip 直接安装"
echo -e "  ${YELLOW}pip install dist/${PACKAGE_NAME}-${VERSION}-py3-none-any.whl${NC}"
echo ""
echo -e "  # 或安装源码包"
echo -e "  ${YELLOW}pip install dist/${PACKAGE_NAME}-${VERSION}.tar.gz${NC}"
echo ""
echo -e "测试安装:"
echo -e "  ${YELLOW}pip install -e .${NC}"
echo ""
echo -e "发布到 PyPI (需要 twine):"
echo -e "  ${YELLOW}twine upload dist/*${NC}"
echo ""
