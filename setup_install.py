"""
KubeEngine 安装脚本

用于从源码或 tarball 安装，不依赖 Cython。
"""

import os
import sys
from pathlib import Path
from setuptools import find_packages, setup

# 确保我们在正确的目录
if Path("src").exists():
    os.chdir(sys.path[0])


def read_file(file_path: str) -> str:
    """读取文件内容"""
    here = Path(__file__).parent
    with open(here / file_path, encoding="utf-8") as f:
        return f.read()


def get_version() -> str:
    """从配置中获取版本号"""
    # 尝试从 web/main.py 获取
    main_py = Path("src/web/main.py")
    if main_py.exists():
        content = read_file("src/web/main.py")
        for line in content.split("\n"):
            if "version=" in line and '"' in line:
                start = line.index('"') + 1
                end = line.index('"', start)
                return line[start:end]
    return "1.0.0"


# 长描述
long_description = ""
if Path("README.md").exists():
    long_description = read_file("README.md")

# 构建 config
setup(
    name="kubengine",
    version=get_version(),
    author="duanzt",
    author_email="duanziteng@gmail.com",
    description="Kubernetes management platform for Kylin OS",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/kubengine/kubengine",
    license="Apache-2.0",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    include_package_data=True,
    install_requires=[
        "fastapi>=0.121.3",
        "uvicorn[standard]>=0.38.0",
        "sqlalchemy>=2.0.45",
        "pydantic>=2.0.0",
        "click>=8.0.0",
        "kubernetes>=34.1.0",
        "asyncssh>=2.21.1",
        "pyinfra>=3.5.1",
        "requests>=2.32.5",
        "websockets>=15.0.1",
        "python-multipart>=0.0.20",
        "rich>=14.3.1",
        "pyyaml>=6.0",
        "toml>=0.10.0",
    ],
    entry_points={
        "console_scripts": [
            "kubengine=cli.app:cli",
            "kubengine_k8s=cli.k8s:cli"
        ],
    },
    python_requires=">=3.11",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Intended Audience :: System Administrators",
        "License :: OSI Approved :: Apache Software License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Topic :: System :: Systems Administration",
    ],
    zip_safe=False,
)
