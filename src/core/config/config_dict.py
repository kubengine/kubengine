import json
import threading
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, Dict, List, Optional, Type, TypeVar, Union, cast
import yaml

# ===================== 新增：TOML 兼容导入 =====================
# 读取：优先用Python 3.11+内置tomllib，否则用第三方toml库
# 写入：依赖第三方toml库（tomllib无dump功能）
try:
    import tomllib  # Python 3.11+ 内置（仅读取）
except ImportError:
    tomllib = None

try:
    import toml  # 第三方库（支持读写），需安装：pip install toml
except ImportError:
    if tomllib is None:
        raise ImportError(
            "处理TOML文件需要安装toml库：pip install toml\nPython 3.11+仅内置读取功能，写入仍需安装toml库")
    toml = None

# 泛型类型（用于类型提示）
T = TypeVar("T", bound="ConfigDict")
K = TypeVar("K")
V = TypeVar("V")


# 单例锁（线程安全）
_SINGLETON_LOCK: threading.Lock = threading.Lock()
_global_config: Optional["ConfigDict"] = None

# 缓存字典：file_path -> ConfigDict实例
_FILE_CACHE: Dict[str, "ConfigDict"] = {}
_CACHE_LOCK: threading.Lock = threading.Lock()


class ConfigDict(dict[str, Any], MutableMapping[str, Any]):
    """增强型配置字典类（核心：递归配置合并，新增TOML支持）

    核心特性：
    1. 基础：属性式访问、默认值+类型校验、JSON/YAML/TOML加载/保存、只读模式、嵌套配置
    2. 新增：merge() 递归合并配置（支持多源、嵌套、灵活的列表处理）
    3. 新增单例 + 懒加载
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__()
        # 只读模式标记
        self._frozen: bool = False
        # 初始化配置（支持字典/关键字参数）
        init_dict: Dict[str, Any] = dict(*args, **kwargs)
        # 嵌套转换：子字典自动转为ConfigDict
        for key, value in init_dict.items():
            self.__setitem__(key, value)

    def __getattr__(self, key: str) -> Any:
        """属性式读取（config.key → config['key']）

        Args:
            key: 配置项键名

        Returns:
            配置值

        Raises:
            AttributeError: 配置项不存在时抛出
        """
        try:
            return self[key]
        except KeyError:
            pass

    def __setattr__(self, key: str, value: Any) -> None:
        """属性式赋值（config.key = value → config['key'] = value）

        Args:
            key: 配置项键名
            value: 配置值
        """
        # 私有属性直接赋值（如_frozen）
        if key.startswith("_"):
            super().__setattr__(key, value)
        else:
            self[key] = value

    def _process_list_item(self, item: Any) -> Any:
        """处理列表项，递归转换字典为ConfigDict

        Args:
            item: 要处理的列表项

        Returns:
            处理后的列表项
        """
        if isinstance(item, dict) and not isinstance(item, ConfigDict):
            return ConfigDict(item)
        return item

    def __setitem__(self, key: str, value: Any) -> None:
        """重写赋值逻辑：嵌套字典自动转为ConfigDict + 只读校验

        Args:
            key: 配置项键名
            value: 配置值

        Raises:
            RuntimeError: 配置已冻结时抛出
        """
        # 只读模式禁止修改
        if self._frozen:
            raise RuntimeError(f"配置已冻结，禁止修改项 '{key}'")

        # 嵌套转换：子字典→ConfigDict，子列表→元素递归转换
        if isinstance(value, dict) and not isinstance(value, ConfigDict):
            value = ConfigDict(value)
        elif isinstance(value, list):
            # 明确转换为 List[Any] 类型
            list_value = cast(List[Any], value)
            processed_list: List[Any] = []
            for item in list_value:  # 现在 item 类型明确为 Any
                if isinstance(item, dict) and not isinstance(item, ConfigDict):
                    processed_list.append(ConfigDict(item))
                else:
                    processed_list.append(item)
            value = processed_list

        super().__setitem__(key, value)

    # 重写 dict 基类方法以兼容所有重载
    # type: ignore[override]
    def get(self, key: str, default: V | None = None) -> V | None:
        """获取配置值，支持默认值

        Args:
            key: 配置项键名
            default: 默认值

        Returns:
            配置值或默认值
        """
        return super().get(key, default)

    def pop(self, key: str, default: V = ...) -> V:  # type: ignore[override]
        """移除并返回指定键的值

        Args:
            key: 要移除的键
            default: 如果键不存在时的默认值

        Returns:
            被移除的值

        Raises:
            KeyError: 键不存在且未提供默认值时抛出
        """
        # 只读模式禁止修改
        if self._frozen:
            raise RuntimeError(f"配置已冻结，禁止移除项 '{key}'")

        result = super().pop(key, default)
        # 如果返回的是字典，转换为ConfigDict
        if isinstance(result, dict) and not isinstance(result, ConfigDict):
            return cast(V, ConfigDict(result))
        return cast(V, result)

    def popitem(self) -> tuple[str, Any]:  # type: ignore[override]
        """移除并返回最后一个键值对

        Returns:
            被移除的键值对

        Raises:
            KeyError: 字典为空时抛出
        """
        # 只读模式禁止修改
        if self._frozen:
            raise RuntimeError("配置已冻结，禁止移除项")

        key, value = super().popitem()
        # 如果值是字典，转换为ConfigDict
        if isinstance(value, dict) and not isinstance(value, ConfigDict):
            value = ConfigDict(value)
        return key, value

    # type: ignore[override]
    def setdefault(self, key: str, default: V | None = None) -> V | None:
        """设置默认值

        Args:
            key: 键名
            default: 默认值

        Returns:
            键对应的值
        """
        # 只读模式禁止修改
        if self._frozen:
            raise RuntimeError(f"配置已冻结，禁止设置项 '{key}'")

        if key not in self:
            self[key] = default
        return cast(V | None, self[key])

    # type: ignore[override]
    def update(self, *args: Any, **kwargs: Any) -> None:
        """更新字典

        Args:
            *args: 位置参数（字典或键值对序列）
            **kwargs: 关键字参数
        """
        # 只读模式禁止修改
        if self._frozen:
            raise RuntimeError("配置已冻结，禁止更新")

        # 处理位置参数
        if args:
            other = args[0]
            if hasattr(other, "keys"):
                for key in other:
                    self[key] = other[key]
            else:
                for key, value in other:
                    self[key] = value

        # 处理关键字参数
        for key, value in kwargs.items():
            self[key] = value

    def clear(self) -> None:  # type: ignore[override]
        """清空字典"""
        # 只读模式禁止修改
        if self._frozen:
            raise RuntimeError("配置已冻结，禁止清空")

        super().clear()

    @classmethod
    def _find_config_file(cls) -> Optional[str]:
        """查找配置文件路径（支持多种安装方式）

        查找顺序（优先级从高到低）：
        1. 环境变量 KUBEENGINE_CONFIG 指定的路径
        2. /opt/kubengine/config/application.yaml（默认安装路径）
        3. ./config/application.yaml（相对路径，用于开发调试）

        Returns:
            找到的配置文件路径，如果都不存在返回 None
        """
        import os

        # 1. 优先使用环境变量
        env_config = os.getenv("KUBEENGINE_CONFIG")
        if env_config and Path(env_config).exists():
            return env_config

        # 2. 默认安装路径（生产环境）
        default_config = "/opt/kubengine/config/application.yaml"
        if Path(default_config).exists():
            return default_config

        # 3. 相对路径（开发调试）
        relative_config = "./config/application.yaml"
        if Path(relative_config).exists():
            return relative_config

        # 如果都不存在，返回默认路径（用于初始化）
        return default_config

    @classmethod
    def get_instance(cls: Type[T]) -> T:
        """获取全局单例配置对象，支持懒加载

        Returns:
            T: 全局唯一的 ConfigDict 实例

        Raises:
            FileNotFoundError: 配置文件不存在时抛出
        """
        global _global_config
        if _global_config is None:
            with _SINGLETON_LOCK:
                if _global_config is None:
                    config_path = cls._find_config_file()
                    if config_path is None or not Path(config_path).exists():
                        raise FileNotFoundError(
                            "配置文件不存在，请检查以下路径：\n"
                            "  - /opt/kubengine/config/application.yaml (默认安装路径)\n"
                            "  - ./config/application.yaml (相对路径)\n"
                            "  或设置环境变量 KUBEENGINE_CONFIG 指定配置文件路径"
                        )
                    _global_config = cast(
                        Optional["ConfigDict"], cls.load_from_file(config_path))
        return cast(T, _global_config)

    def get_with_default(
        self,
        key: str,
        default: Any,
        value_type: Optional[Type[Any]] = None,
        allow_none: bool = False
    ) -> Any:
        """带默认值和类型校验的获取方法

        Args:
            key: 配置项键名
            default: 默认值（key不存在时返回）
            value_type: 期望的类型（如str/int，None不校验）
            allow_none: 是否允许值为None（仅当value_type指定时生效）

        Returns:
            配置值（或默认值）

        Raises:
            TypeError: 类型不匹配时抛出
        """
        value = self.get(key, default)

        # 类型校验
        if value_type is not None:
            if value is None:
                if not allow_none:
                    raise TypeError(
                        f"配置项 '{key}' 不允许为None（期望类型：{value_type.__name__}）")
            elif not isinstance(value, value_type):
                raise TypeError(
                    f"配置项 '{key}' 类型错误：期望 {value_type.__name__}，实际 {type(value).__name__}"
                )

        return value

    def validate_required_keys(self, required_keys: List[str]) -> bool:
        """校验必填配置项是否存在

        Args:
            required_keys: 必填键名列表

        Returns:
            校验通过返回True

        Raises:
            ValueError: 缺失必填项时抛出
        """
        missing_keys = [key for key in required_keys if key not in self]
        if missing_keys:
            raise ValueError(f"缺失必填配置项：{', '.join(missing_keys)}")
        return True

    def freeze(self) -> None:
        """冻结配置（只读模式）"""
        self._frozen = True

    def thaw(self) -> None:
        """解冻配置（可写模式）"""
        self._frozen = False

    @classmethod
    def load_from_file(cls: Type[T], file_path: str, format: Optional[str] = None) -> T:
        """从文件加载配置（支持JSON/YAML/TOML）

        Args:
            file_path: 配置文件路径
            format: 文件格式（json/yaml/toml，None自动识别）

        Returns:
            ConfigDict实例

        Raises:
            ValueError: 不支持的文件格式
            FileNotFoundError: 文件不存在
        """
        # 缓存命中检查
        with _CACHE_LOCK:
            if file_path in _FILE_CACHE:
                return cast(T, _FILE_CACHE[file_path])

        # 自动识别格式
        if format is None:
            file_ext = Path(file_path).suffix.lower()
            if file_ext == ".json":
                format = "json"
            elif file_ext in (".yaml", ".yml"):
                format = "yaml"
            elif file_ext == ".toml":
                format = "toml"
            else:
                raise ValueError(f"无法识别文件格式：{file_path}（仅支持json/yaml/toml）")

        # 读取文件并解析（核心修复段）
        try:
            if format == "json":
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            elif format == "yaml":
                with open(file_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
            elif format == "toml":
                if tomllib is not None:
                    with open(file_path, "rb") as f:
                        data = tomllib.load(f)
                else:
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = toml.load(f)  # type: ignore
            else:
                raise ValueError(f"不支持的文件格式：{format}")
        except FileNotFoundError as e:
            raise FileNotFoundError(f"配置文件不存在：{file_path}") from e

        config = cls(data)

        # 缓存结果
        with _CACHE_LOCK:
            _FILE_CACHE[file_path] = config

        return config

    def save_to_file(self, file_path: str, format: Optional[str] = None, indent: int = 4) -> None:
        """保存配置到文件（支持JSON/YAML/TOML）

        Args:
            file_path: 保存路径
            format: 文件格式（json/yaml/toml，None自动识别）
            indent: 缩进（仅JSON生效）

        Raises:
            ValueError: 不支持的文件格式
            ImportError: 保存TOML时缺少依赖库
        """
        # ===================== 新增：TOML 格式识别 =====================
        # 自动识别格式
        if format is None:
            file_ext = Path(file_path).suffix.lower()
            if file_ext == ".json":
                format = "json"
            elif file_ext in (".yaml", ".yml"):
                format = "yaml"
            elif file_ext == ".toml":
                format = "toml"
            else:
                raise ValueError(f"无法识别文件格式：{file_path}（仅支持json/yaml/toml）")

        # 转换为原生dict（避免嵌套ConfigDict序列化问题）
        def to_raw_dict(obj: Any) -> Any:
            if isinstance(obj, ConfigDict):
                return {k: to_raw_dict(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                list_value = cast(List[Any], obj)
                return [to_raw_dict(item) for item in list_value]
            else:
                return obj

        raw_data = to_raw_dict(self)

        # ===================== 新增：TOML 写入逻辑 =====================
        # 写入文件
        with open(file_path, "w", encoding="utf-8") as f:
            if format == "json":
                json.dump(raw_data, f, ensure_ascii=False, indent=indent)
            elif format == "yaml":
                yaml.safe_dump(raw_data, f, allow_unicode=True,
                               sort_keys=False)
            elif format == "toml":
                # TOML写入依赖第三方toml库
                if toml is None:
                    raise ImportError("保存TOML文件需要安装toml库：pip install toml")
                toml.dump(raw_data, f)
            else:
                raise ValueError(f"不支持的文件格式：{format}")

    def merge(
        self,
        *config_sources: Union[Dict[str, Any], "ConfigDict"],
        extend_lists: bool = False,
        overwrite: bool = True
    ) -> "ConfigDict":
        """递归合并配置（支持多源、嵌套、灵活的列表处理）

        Args:
            *config_sources: 要合并的配置源（可传多个，优先级：后传入 > 先传入 > 原有配置）
            extend_lists: 列表处理模式：True=扩展（保留原有+新增），False=替换（默认）
            overwrite: 非字典/列表项是否覆盖：True=新值覆盖旧值（默认），False=保留旧值

        Returns:
            合并后的自身实例（支持链式调用）
        """
        def _recursive_merge(target: "ConfigDict", source: Union[Dict[str, Any], "ConfigDict"]) -> None:
            """内部递归合并逻辑

            Args:
                target: 目标配置对象
                source: 源配置对象
            """
            for key, value in source.items():
                # 目标存在该键，且双方都是字典/ConfigDict → 递归合并
                if (key in target and isinstance(target[key], (dict, ConfigDict)) and isinstance(value, (dict, ConfigDict))):
                    # 确保目标值是ConfigDict（统一类型）
                    if not isinstance(target[key], ConfigDict):
                        target[key] = ConfigDict(
                            cast(Dict[str, Any], target[key]))
                    # 递归合并子配置
                    _recursive_merge(target[key], cast(
                        "ConfigDict", cast(List[Any], value)))
                # 目标存在该键，且双方都是列表 → 扩展/替换
                elif (key in target and isinstance(target[key], list) and isinstance(value, list)):
                    if extend_lists:
                        # 扩展列表：去重（可选）+ 合并
                        combined_list = list(
                            target[key]) + list(cast(List[Any], value))
                        target[key] = list(dict.fromkeys(combined_list))
                    else:
                        # 替换列表（默认）
                        target[key] = value
                # 其他情况：覆盖/保留
                else:
                    if overwrite:
                        # 覆盖：新值替换旧值（自动转换嵌套字典为ConfigDict）
                        if isinstance(value, dict) and not isinstance(value, ConfigDict):
                            target[key] = ConfigDict(value)
                        else:
                            target[key] = value
                    # 不覆盖：保留原有值，跳过

        # 遍历所有配置源，依次合并（后传入的优先级更高）
        for source in config_sources:
            # 统一转换为ConfigDict（兼容普通dict）
            source_config: ConfigDict
            if not isinstance(source, ConfigDict):
                source_config = ConfigDict(source)
            else:
                source_config = source
            _recursive_merge(self, source_config)

        return self  # 链式调用支持（如 config.merge(a).merge(b)）

    def __repr__(self) -> str:
        """自定义打印格式

        Returns:
            格式化的字符串表示
        """
        return f"ConfigDict({super().__repr__()})"
