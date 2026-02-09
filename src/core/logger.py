import logging
from pathlib import Path
from logging.handlers import TimedRotatingFileHandler
import sys
from typing import Optional

from core.config.application import Application
# 新增：导入Rich日志处理器和控制台类型（适配rich）
from rich.console import Console


class GlobalLoggerManager:
    _instance: Optional["GlobalLoggerManager"] = None
    _configured: bool = False

    def __new__(cls):
        """单例模式：确保全局只有一个管理器实例"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def setup(
        self,
        level: Optional[str] = None,
        log_file: Optional[str] = None,
        console_output: Optional[bool] = None,
        rich_console: Optional[Console] = None  # 新增：支持传入外部Rich Console实例
    ) -> None:
        """
        配置全局日志（幂等操作：多次调用仅生效一次）
        :param rich_console: 外部Rich Console实例，用于统一日志/进度条输出载体
        """
        if self._configured:
            logging.getLogger(__name__).debug("全局日志已配置，跳过重复初始化")
            return

        # 1. 重置根Logger
        root_logger = logging.getLogger()
        root_logger.setLevel(
            getattr(logging, level or Application.LOGGER_CONFIG.LEVEL.upper()))
        root_logger.handlers.clear()

        # 2. 创建格式化器（保留原有配置，RichHandler兼容原生格式）
        formatter = logging.Formatter(
            fmt=Application.LOGGER_CONFIG.FORMAT, datefmt=Application.LOGGER_CONFIG.DATE_FORMAT)

        # 3. 配置控制台输出【核心修改：替换为RichHandler，适配rich生态，移除stream参数】
        # if console_output:
        #     # 优先使用传入的外部Rich Console（与CLI的progress/console统一，避免输出冲突）
        #     console = rich_console or Console()
        #     # 创建Rich日志处理器（美化输出+兼容rich控制台，适配低版本Rich）
        #     console_handler = RichHandler(
        #         console=console,
        #         show_time=False,  # 不显示时间戳
        #         show_level=False,  # 不显示日志级别
        #         show_path=False,  # 隐藏文件路径（简化CLI输出，可根据需要开启）
        #         rich_tracebacks=False,
        #         enable_link_path=False,  # 关闭路径链接（避免多余样式）
        #         log_time_format="%Y-%m-%d %H:%M:%S",  # 兼容低版本Rich的时间格式参数
        #     )
        #     console_handler.setFormatter(formatter)
        #     root_logger.addHandler(console_handler)
        # 3. 配置控制台输出
        if console_output:
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setFormatter(formatter)
            root_logger.addHandler(console_handler)

        # 4. 配置文件输出（完全保留原有轮转逻辑，不做任何修改）
        if log_file:
            self._setup_file_handler(root_logger, formatter, log_file)

        # 5. 管控第三方库日志级别（完全保留原有降噪逻辑）
        self._setup_third_party_loggers()

        # 标记配置完成
        self._configured = True
        root_logger.info(
            f"全局日志初始化完成 | 级别: {level or Application.LOGGER_CONFIG.LEVEL} | "
            f"日志文件: {log_file or '无'} | 轮转: {Application.LOGGER_CONFIG.ROTATE_ENABLE}"
        )

    def _setup_file_handler(self, root_logger: logging.Logger, formatter: logging.Formatter, log_file: str):
        """配置文件处理器（支持轮转）【完全保留原有代码】"""
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        if Application.LOGGER_CONFIG.ROTATE_ENABLE:
            file_handler = TimedRotatingFileHandler(
                filename=log_path,
                when=Application.LOGGER_CONFIG.ROTATE_WHEN,
                backupCount=Application.LOGGER_CONFIG.ROTATE_BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.suffix = "%Y-%m-%d.log"
        else:
            file_handler = logging.FileHandler(
                log_path, mode="a", encoding="utf-8")

        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    def _setup_third_party_loggers(self):
        """设置第三方库日志级别【完全保留原有代码】"""
        for logger_name, level in Application.LOGGER_CONFIG.THIRD_PARTY_LOG_LEVELS.items():
            third_logger = logging.getLogger(logger_name)
            third_logger.setLevel(getattr(logging, level.upper()))
            third_logger.propagate = False


def setup_fastapi_logging(
    level: str = "INFO",
    log_file: Optional[str] = None,
    console_output: bool = True
) -> None:
    """FastAPI场景快捷配置【完全保留原有代码】"""
    GlobalLoggerManager().setup(
        level, log_file or f"{Application.ROOT_DIR}/logs/web.log", console_output)


def setup_cli_logging(
    level: str = "DEBUG",
    log_file: Optional[str] = None,
    console_output: Optional[bool] = True,
    rich_console: Optional[Console] = None  # 新增：透传Rich Console实例到setup方法
) -> None:
    """CLI场景快捷配置（调试级别、轮转）【新增rich_console参数】"""
    GlobalLoggerManager().setup(
        level,
        log_file or f"{Application.ROOT_DIR}/logs/cli.log",
        console_output,
        rich_console=rich_console  # 透传外部Console
    )


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """快捷获取Logger实例【完全保留原有代码】"""
    return logging.getLogger(name)
