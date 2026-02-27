"""
KubeEngine setup.py

支持 Cython 编译的构建配置。

注意：
- 项目元数据优先从 pyproject.toml 读取（现代构建）
- 如果 pyproject.toml 不存在，使用本文件的元数据（RPM 构建）
"""

from pathlib import Path
from typing import Any
from setuptools import Extension, find_packages, setup
from setuptools.command.build_ext import build_ext
from setuptools.command.develop import develop

try:
    from Cython.Build import cythonize  # type: ignore
    CYTHON_INSTALLED = True
except ImportError:
    CYTHON_INSTALLED = False  # type: ignore

# 项目元数据（当 pyproject.toml 不存在时使用）
_METADATA: dict[str, Any] = {
    "name": "kubengine",
    "version": "0.1.0",
    "description": "Kubernetes management platform for Kylin OS",
    "long_description": open("README.md").read() if Path("README.md").exists() else "",
    "long_description_content_type": "text/markdown",
    "author": "duanzt",
    "author_email": "duanziteng@gmail.com",
    "url": "https://github.com/kubengine/kubengine",
    "license": "Apache-2.0",
    "python_requires": ">=3.11",
    "entry_points": {
        "console_scripts": [
            "kubengine=cli.app:cli",
            "kubengine_k8s=cli.k8s:cli",
        ],
        "image_builders": [
            "kylin-v11 = builder.image.os.kylin_v11:Builder",
            "kubectl = builder.image.kubectl.builder:Builder",
            "os-shell = builder.image.os_shell.builder:Builder",
            "redis = builder.image.redis.builder:RedisBuilder",
            "redis-sentinel = builder.image.redis.builder:SentinelBuilder",
            "redis-exporter = builder.image.redis.exporter_builder:Builder",
        ],
    },
}


class CythonExtension(build_ext):
    """自定义构建命令，支持 Cython 编译"""

    def build_extensions(self):
        # Cython 编译在 ext_modules 阶段已经通过 cythonize() 完成
        # 这里只需调用标准的 build_ext 流程
        build_ext.build_extensions(self)


class CustomDevelop(develop):
    """自定义开发安装命令"""

    def run(self):
        # 先运行 build_ext 编译扩展
        self.run_command("build_ext")
        develop.run(self)


def get_extensions() -> list[Extension]:
    """
    获取需要编译的 Cython 扩展列表

    注意：web 包被排除在 Cython 编译之外，因为它使用 Pydantic v2，
    而 Pydantic v2 与 Cython 不兼容（Cython 会剥离类型注解）。

    Returns:
        Extension 对象列表
    """
    extensions: list[Extension] = []

    # 如果安装了 Cython，添加所有需要编译的模块
    if CYTHON_INSTALLED:
        # 收集所有 Python 包
        packages = find_packages(where="src")
        for package in packages:
            # 排除 web 包及其子包（Pydantic v2 与 Cython 不兼容）
            if package == "web" or package.startswith("web."):
                print(f"跳过包 '{package}'（使用 Pydantic v2，不兼容 Cython）")
                continue
            # 排除 cli 包（Click 装饰器需要保留原始 docstring）
            if package == "cli" or package.startswith("cli."):
                print(f"跳过包 '{package}'（Click 装饰器需要保留 docstring）")
                continue
            # 排除 infra 包
            if package == "infra" or package.startswith("infra."):
                print(f"跳过包 '{package}'")
                continue

            # 找到包对应的目录
            package_dir = Path("src") / package.replace(".", "/")
            if not package_dir.exists():
                continue

            # 收集该包下的所有 .py 文件
            py_files = list(package_dir.rglob("*.py"))

            for py_file in py_files:
                # 跳过 __init__.py 和测试文件
                if (
                    py_file.name == "__init__.py" or "_test" in py_file.stem or "test_" in py_file.stem
                ):
                    continue

                # 创建 Extension
                module_path = py_file.relative_to("src")
                module_name = str(module_path.with_suffix("")
                                  ).replace("/", ".")

                ext = Extension(
                    module_name,
                    [str(py_file)],
                    include_dirs=["src"],
                    define_macros=[
                        ("NPY_NO_DEPRECATED_API", "NPY_1_7_API_VERSION")],
                )
                extensions.append(ext)

    return extensions


# 构建 config - 合并元数据和构建配置
# 获取所有包，并手动添加 find_packages() 可能遗漏的包
all_packages = find_packages(where="src")
# 手动添加包含非 Python 文件的包
if "builder.image.rootfs" not in all_packages:
    all_packages.append("builder.image.rootfs")

config: dict[str, Any] = {
    # 指定包的位置和包名（setuptools 需要）
    "package_dir": {"": "src"},
    "packages": all_packages,
    # 包含包数据文件（静态文件、配置文件等）
    "include_package_data": True,
    "package_data": {
        # Builder 配置文件
        "builder.image.os": ["*.yaml"],
        "builder.image.kubectl": ["*.yaml"],
        "builder.image.os_shell": ["*.yaml"],
        "builder.image.redis": ["*.yaml"],
        # Builder rootfs 静态文件（包含二进制文件和脚本）
        "builder.image.rootfs": ["**/*"],
        # Web 静态文件
        "web": ["static/**/*"],
    },
    # 添加元数据（如果 pyproject.toml 不存在）
    **_METADATA,
}

if CYTHON_INSTALLED:
    extensions = get_extensions()
    print(f"正在编译 {len(extensions)} 个 Cython 扩展...")
    config["ext_modules"] = cythonize(  # type: ignore
        extensions,
        compiler_directives={
            "language_level": "3",
            "embedsignature": True,
            "annotation_typing": False,  # 允许 dict 子类（如 ConfigDict）
        },
        annotate=True,
    )
    config["cmdclass"] = {
        "build_ext": CythonExtension,
        "develop": CustomDevelop,
    }

if __name__ == "__main__":
    setup(**config)  # type: ignore
