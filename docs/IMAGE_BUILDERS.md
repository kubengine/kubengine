# 自定义 Image Builders 开发指南

本文档将指导您如何创建和集成自定义镜像构建器（Image Builders）到 Kubengine 项目中，使您能够支持新的应用类型或操作系统。

## 概述

Kubengine 使用 image_builders 入口点系统（entry-points）来管理和扩展镜像构建功能。image_builders 是通过插件化架构实现的，允许开发者在不修改核心代码的情况下添加新的构建器。

## 创建自定义 Image Builder

### 1. 创建项目结构

首先，在 `src/builder/image` 目录下创建您的自定义构建器目录结构：

```
src/builder/image/
├── your_custom_builder/           # 您的构建器目录
│   ├── __init__.py               # 模块初始化文件
│   ├── builder.py                # 构建器实现文件
│   ├── config.yaml               # 构建配置文件
│   └── README.md                 # 文档（可选）
```

### 2. 创建构建器实现

在 `builder.py` 文件中实现您的自定义构建器。您需要继承基础构建器类并实现必要的方法。

#### 示例：简单的自定义构建器

```python
"""
自定义镜像构建器

专门用于构建您的应用的镜像构建器。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Any, Optional, Union

from builder.image.base_builder import BuilderOptions
from builder.image.os.kylin_v11 import Builder as KylinV11Builder  # 继承基础构建器
from core.logger import get_logger

logger = get_logger(__name__)


class YourCustomBuilder(KylinV11Builder):
    """您的自定义镜像构建器

    此类扩展了基础的 KylinV11Builder 以提供特定功能。
    """

    # 构建器元数据
    __version__ = "1.0.0"
    __author__ = "Your Name"

    @staticmethod
    def supported_features() -> List[str]:
        """获取构建器支持的功能特性

        Returns:
            List[str]: 支持的特性列表
        """
        parent_features = KylinV11Builder.supported_features()
        custom_features = [
            "your_custom_feature_1",
            "your_custom_feature_2",
            "your_custom_feature_3"
        ]
        return parent_features + custom_features

    def __init__(
        self,
        name: str,
        config_file: Optional[Union[str, Path]] = None,
        options: Optional[BuilderOptions] = None,
        **kwargs: Any
    ):
        """初始化自定义镜像构建器

        Args:
            name: 镜像名称
            config_file: 配置文件路径，默认使用同目录下的config.yaml
            options: 构建器选项对象
            **kwargs: 其他构建参数
        """
        # 设置默认配置文件路径
        default_config = Path(__file__).parent / "config.yaml"
        config_path = config_file or default_config

        # 调用父类初始化
        super().__init__(name, config_path, options, **kwargs)

        logger.info(f"自定义构建器初始化完成: {name}")
        logger.debug(f"配置文件: {config_path}")
        logger.debug(f"支持特性: {self.supported_features()}")

    # 可以重写或添加其他方法来实现自定义功能
    def your_custom_method(self, context):
        """您的自定义方法

        Args:
            context: 构建上下文

        Raises:
            BuilderError: 如果方法执行失败
        """
        logger.info("执行自定义操作")
        # 添加您的自定义代码
```

### 3. 创建配置文件

在 `config.yaml` 文件中定义您的构建配置。配置内容取决于您的构建器功能和需求。

#### 示例配置

```yaml
# 自定义构建器配置

# 基础镜像配置
base_image: "kylin-v11"

# 软件包管理配置
package_manager: "dnf"

# 需要安装的软件包
install_packages:
  - "your-package-1"
  - "your-package-2"
  - "your-package-3"

# DNF 配置选项
setopts:
  - "timeout=300"
  - "retries=3"

# DNS 服务器配置
dns_servers:
  - "8.8.8.8"
  - "114.114.114.114"

# 系统配置
hostname: "your-custom-hostname"

# 需要创建的额外目录
create_directories:
  - "var/your/custom/path"
  - "opt/your/app/directory"
```

### 4. 初始化模块

在 `__init__.py` 文件中添加模块初始化代码：

```python
"""
自定义构建器模块
"""

from .builder import YourCustomBuilder

__all__ = ["YourCustomBuilder"]
```

## 注册 Image Builder

### 在 pyproject.toml 中配置 entry-point

在项目根目录下的 `pyproject.toml` 文件中，添加以下内容到 `[project.entry-points."image_builders"]` 部分：

```toml
[project.entry-points."image_builders"]
your-custom-builder = "builder.image.your_custom_builder.builder:YourCustomBuilder"
```

**格式说明：**
- `your-custom-builder`: 是您的构建器在 CLI 中使用的名称（支持连字符）
- `builder.image.your_custom_builder.builder`: 是模块导入路径（使用下划线）
- `YourCustomBuilder`: 是您在 Python 文件中定义的类名

### 在 setup.py 中配置（如果使用）

如果您正在使用 `setup.py` 文件，还需要在 `entry_points` 部分添加类似的配置：

```python
    "entry_points": {
        "image_builders": [
            "your-custom-builder = builder.image.your_custom_builder.builder:YourCustomBuilder",
        ],
    },
```

## 验证您的 Image Builder

### 1. 安装项目

确保您的项目已正确安装：

```bash
# 使用开发模式安装
pip install -e .[dev]
```

### 2. 验证构建器是否被识别

运行以下命令来验证您的构建器是否被正确识别：

```bash
kubengine image list-apps
```

如果配置正确，您的新构建器应该会出现在列表中。

### 3. 测试构建过程

使用您的新构建器测试镜像构建：

```bash
kubengine image build your-custom-builder -v 1.0.0
```

## 高级功能

### 继承其他构建器

您可以继承其他已有的构建器来扩展功能，而不是从头开始创建：

```python
from builder.image.redis.builder import RedisBuilder

class YourCustomRedisBuilder(RedisBuilder):
    """扩展 Redis 构建器的自定义构建器"""

    # 您的自定义实现
```

### 支持多版本

您可以在构建器中添加版本支持检查：

```python
    def get_supported_versions(self) -> List[str]:
        """获取支持的版本列表"""
        return ["1.0.0", "1.0.1", "1.1.0"]

    def validate_version(self, version: str) -> bool:
        """验证版本是否被支持"""
        return version in self.get_supported_versions()
```

### 自定义配置验证

您可以添加自定义的配置验证逻辑：

```python
    def _validate_config(self, config: ConfigDict) -> None:
        """验证配置文件"""
        super()._validate_config(config)

        # 您的自定义验证逻辑
        if not config.get("your_required_config"):
            raise ConfigurationError("缺少 required_config 配置项")
```

## 调试 Image Builders

### 1. 启用调试日志

您可以通过设置日志级别来调试构建器：

```bash
kubengine image build your-custom-builder -v 1.0.0 --debug
```

### 2. 在代码中添加调试信息

在您的构建器代码中添加调试信息：

```python
from core.logger import get_logger

logger = get_logger(__name__)

# 在关键位置添加调试日志
logger.debug("执行操作之前")
logger.info("正在执行操作")
logger.warning("可能存在的问题")
logger.error("错误发生")
```

### 3. 使用交互式调试

使用 Python 调试器进行交互式调试：

```python
import pdb; pdb.set_trace()
```

## 最佳实践

### 1. 遵循项目架构

保持与项目现有代码风格和架构的一致性。

### 2. 实现最小接口

只实现您需要的功能，遵循开闭原则（对扩展开放，对修改关闭）。

### 3. 文档化您的构建器

为您的构建器创建文档，解释其用途、特性和配置选项。

### 4. 添加测试

为您的构建器添加单元测试，确保其功能正常。

### 5. 考虑性能

优化您的构建器以提高构建速度和减少资源消耗。

## 常见问题

### 1. 构建器未被识别

- 确保您已正确添加到 pyproject.toml 和 setup.py 文件中
- 确保您的模块和类名正确
- 重新安装项目以重新加载入口点

### 2. 导入错误

- 确保您的模块结构正确
- 确保依赖项已正确安装
- 检查 Python 路径配置

### 3. 权限问题

- 确保您有足够的权限执行操作
- 检查文件权限和所有权

## 结论

通过遵循本文档，您可以创建和集成自定义的 Image Builder，使 Kubengine 支持新的应用类型或操作系统。这是一个强大的扩展机制，允许项目在保持核心代码不变的同时不断增长。

如果您有任何问题或需要进一步的帮助，请查看项目的 [API 文档](API.md) 或 [CLI 文档](CLI.md)。