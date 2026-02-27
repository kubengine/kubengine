"""
Cython 编译脚本

将 Python 代码编译为 C 扩展，提高性能并保护源码。
"""

import shutil
import subprocess
import sys
from pathlib import Path


def build_cython(
    source_dir: str = "src",
    build_dir: str = "build",
    exclude_patterns: list[str] | None = None,
) -> None:
    """
    使用 Cython 编译 Python 代码

    Args:
        source_dir: 源代码目录
        build_dir: 构建输出目录
        exclude_patterns: 要排除的文件模式列表
    """
    if exclude_patterns is None:
        exclude_patterns = [
            "*_test.py",
            "test_*.py",
            "conftest.py",
            "__init__.py",
            "*/migrations/*",
        ]

    source_path = Path(source_dir)
    build_path = Path(build_dir)

    # 创建构建目录
    build_path.mkdir(parents=True, exist_ok=True)

    print("开始 Cython 编译...")
    print(f"源目录: {source_path.absolute()}")
    print(f"构建目录: {build_path.absolute()}")

    # 收集所有 Python 文件
    py_files: list[Path] = []
    for pattern in ["**/*.py"]:
        py_files.extend(source_path.rglob(pattern))

    # 过滤排除的文件
    included_files: list[Path] = []
    for py_file in py_files:
        rel_path = py_file.relative_to(source_path)
        should_exclude = False

        for pattern in exclude_patterns:
            if rel_path.match(pattern):
                should_exclude = True
                break

        if not should_exclude:
            included_files.append(py_file)

    print(f"找到 {len(included_files)} 个 Python 文件需要编译")

    # 编译每个文件
    success_count = 0
    fail_count = 0

    for py_file in included_files:
        rel_path = py_file.relative_to(source_path)
        try:
            # 运行 cythonize
            cmd = [
                sys.executable,
                "-m",
                "cython",
                str(py_file),
                "-3",
                f"--output-file={build_path / rel_path.with_suffix('.c')}",
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
            )

            if result.returncode == 0:
                success_count += 1
                print(f"✓ {rel_path}")
            else:
                fail_count += 1
                print(f"✗ {rel_path}: {result.stderr}")

        except Exception as e:
            fail_count += 1
            print(f"✗ {rel_path}: {e}")

    print(f"\n编译完成: {success_count} 成功, {fail_count} 失败")

    # 生成编译后的源代码分发包
    create_sdist(str(build_path), source_dir)


def create_sdist(build_dir: str, source_dir: str) -> None:
    """
    创建包含 C 代码的源代码分发包

    Args:
        build_dir: 构建目录
        source_dir: 源代码目录
    """
    import tarfile

    build_path = Path(build_dir)
    sdist_path = build_path / "sdist"

    sdist_path.mkdir(parents=True, exist_ok=True)

    # 复制原始源代码
    src_sdist = sdist_path / source_dir
    shutil.copytree(source_dir, src_sdist, ignore=shutil.ignore_patterns(
        "__pycache__",
        "*.pyc",
        ".pytest_cache",
        "*.egg-info",
        ".mypy_cache",
        "*.db",
        "*.log",
    ))

    # 复制编译后的 C 文件
    c_files = list(build_path.rglob("*.c"))
    for c_file in c_files:
        rel_path = c_file.relative_to(build_path)
        dest_file = sdist_path / rel_path
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(c_file, dest_file)

    # 创建 tar.gz 包
    zip_path = build_path / "kubengine-cython.tar.gz"
    with tarfile.open(zip_path, "w:gz") as tar:
        tar.add(sdist_path, arcname="kubengine")

    print(f"\n源代码分发包已创建: {zip_path.absolute()}")


def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description="Cython 编译工具")
    parser.add_argument(
        "--source-dir",
        default="src",
        help="源代码目录 (默认: src)",
    )
    parser.add_argument(
        "--build-dir",
        default="build",
        help="构建输出目录 (默认: build)",
    )

    args = parser.parse_args()

    build_cython(args.source_dir, args.build_dir)


if __name__ == "__main__":
    main()
