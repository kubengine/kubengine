"""Infrastructure executor for deploying infrastructure files.

This module provides a high-level wrapper for executing infrastructure
deployment files using PyInfra with proper error handling and logging.
"""

import asyncio
from contextvars import copy_context
import importlib.util
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union, cast

from pyinfra import logger as pyinfra_logger
from pyinfra.api.config import Config
from pyinfra.api.state import BaseStateCallback, State, StateStage
from pyinfra.api.connect import connect_all
from pyinfra.api.inventory import Inventory
from pyinfra.api.operations import run_ops
from pyinfra.context import ctx_config, ctx_inventory, ctx_state, ctx_host
from pyinfra_cli.prints import print_results  # type: ignore
from core.logger import bind_log_context, get_logger, log_lifecycle_event

logger = get_logger(__name__)
pyinfra_logger.setLevel(logger.level)  # 对齐PyInfra日志级别


class InfraLifecycleCallback(BaseStateCallback):
    """将 PyInfra 的主机操作回调转换为统一生命周期事件。"""

    def __init__(self) -> None:
        self._operation_started_at: Dict[Tuple[str, str], float] = {}

    @staticmethod
    def _operation_name(state: State, op_hash: str) -> str:
        op_meta = state.get_op_meta(op_hash)
        names = sorted(str(name) for name in getattr(op_meta, "names", set()))
        return ", ".join(names) or f"operation_{str(op_hash)[:8]}"

    def operation_host_start(self, state: State, host: Any, op_hash: str) -> None:
        host_name = str(host)
        operation = self._operation_name(state, op_hash)
        self._operation_started_at[(host_name, op_hash)] = time.monotonic()
        with bind_log_context(stage="execute", host=host_name, operation=operation):
            log_lifecycle_event(logger, "operation_start")

    def _operation_end(
        self,
        state: State,
        host: Any,
        op_hash: str,
        *,
        status: str,
        retry_count: int,
    ) -> None:
        host_name = str(host)
        operation = self._operation_name(state, op_hash)
        started_at = self._operation_started_at.pop((host_name, op_hash), None)
        duration_ms = (
            round((time.monotonic() - started_at) * 1000)
            if started_at is not None
            else None
        )
        level = {
            "success": logging.INFO,
            "ignored_error": logging.WARNING,
        }.get(status, logging.ERROR)
        with bind_log_context(stage="execute", host=host_name, operation=operation):
            log_lifecycle_event(
                logger,
                "operation_end",
                level=level,
                status=status,
                duration_ms=duration_ms,
                retry_count=retry_count,
            )

    def operation_host_success(
        self, state: State, host: Any, op_hash: str, retry_count: int = 0
    ) -> None:
        self._operation_end(
            state, host, op_hash, status="success", retry_count=retry_count
        )

    def operation_host_error(
        self,
        state: State,
        host: Any,
        op_hash: str,
        retry_count: int = 0,
        max_retries: int = 0,
    ) -> None:
        op_data = state.get_op_data_for_host(host, op_hash)
        ignore_errors = bool(
            getattr(op_data, "global_arguments", {}).get("_ignore_errors", False)
        )
        self._operation_end(
            state,
            host,
            op_hash,
            status="ignored_error" if ignore_errors else "failed",
            retry_count=retry_count,
        )

    def operation_host_retry(
        self,
        state: State,
        host: Any,
        op_hash: str,
        retry_num: int,
        max_retries: int,
    ) -> None:
        host_name = str(host)
        operation = self._operation_name(state, op_hash)
        with bind_log_context(stage="execute", host=host_name, operation=operation):
            log_lifecycle_event(
                logger,
                "operation_retry",
                level=logging.WARNING,
                retry_num=retry_num,
                max_retries=max_retries,
            )


@dataclass
class InfraExecutionConfig:
    """Configuration for infrastructure execution.

    Attributes:
        parallel: Number of parallel executions
        connect_timeout: Connection timeout in seconds
        fail_percent: Failure percentage threshold
        verbosity: Verbosity level (0-3)
        check_for_changes: 是否让 pyinfra 在操作注册阶段预检变更与否
        op_timeout: 未显式指定 _timeout 的操作的默认超时（秒）
    """

    parallel: int = 5
    connect_timeout: int = 10
    fail_percent: int = 0
    verbosity: int = 1
    # True 时 pyinfra 会在每个操作注册时逐台取 fact 判断变更，等于每台多一轮
    # SSH 往返；而这里的注册是逐台串行的，实测 containerd 阶段 29 台要多花
    # 十几分钟且全程无输出。默认关闭（等价 pyinfra -y），变更与否在执行阶段
    # 照样会打印 Success / No changes。
    check_for_changes: bool = False
    # 兜底超时：pyinfra 的 _timeout 默认是 None（永不超时），于是任何一条
    # 远端命令卡住都会让部署无声地停在那儿（实测遇到过 7 分钟）。传输类操作
    # 已在 infra 脚本里按文件大小显式给了 _timeout，这个默认值管住剩下的：
    # files.put / helm / kubectl / kubeadm / systemd 等。取 1 小时是纯失控
    # 保险，正常单条命令远用不到。
    op_timeout: int = 3600
    fail_fast: bool = True  # 为True时，一个文件失败立即停止；为False时继续执行其他文件


@dataclass
class HostOperationResult:
    """Detailed result of a single operation on a host.

    Attributes:
        operation_name: Name/identifier of the operation
        success: Whether the operation succeeded
        changed: Whether the operation made changes
        skipped: 操作是否不适用于当前主机而被跳过
        output: Raw output from the operation
        error: Error message if operation failed
    """

    operation_name: str
    success: bool = False
    changed: bool = False
    skipped: bool = False
    output: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def dict(self) -> Dict[str, Any]:
        """Convert operation result to dictionary for JSON serialization."""
        return {
            "operation_name": self.operation_name,
            "success": self.success,
            "changed": self.changed,
            "skipped": self.skipped,
            "output": self.output,
            "error": self.error,
            "status": (
                "skipped" if self.skipped else "success" if self.success else "failed"
            ),
        }


@dataclass
class HostExecutionResult:
    """Detailed execution result for a single host.

    Attributes:
        hostname: Target host IP/hostname
        connected: Whether the host was successfully connected to
        execution_start_time: Timestamp of execution start (Unix seconds)
        execution_end_time: Timestamp of execution end (Unix seconds)
        operations: Dictionary of operation results (key: operation name)
        total_operations: 当前主机纳入统计的操作总数
        successful_operations: Number of successful operations
        failed_operations: Number of failed operations
        changed_operations: Number of operations that made changes
        skipped_operations: 不适用于当前主机的跳过操作数
        error: Top-level error (e.g., connection failure)
    """

    hostname: str
    connected: bool = False
    execution_start_time: float = 0.0
    execution_end_time: float = 0.0
    operations: Dict[str, HostOperationResult] = field(default_factory=dict)
    total_operations: int = 0
    successful_operations: int = 0
    failed_operations: int = 0
    changed_operations: int = 0
    skipped_operations: int = 0
    error: Optional[str] = None

    @property
    def execution_duration(self) -> float:
        """Calculate total execution duration for the host (seconds)."""
        return max(0.0, self.execution_end_time - self.execution_start_time)

    @property
    def success(self) -> bool:
        """Determine if host execution was successful (no errors/failures)."""
        return self.connected and self.failed_operations == 0 and self.error is None

    def dict(self) -> Dict[str, Any]:
        """Convert host result to dictionary for JSON serialization."""
        executed_operations = self.successful_operations + self.failed_operations
        return {
            "hostname": self.hostname,
            "connected": self.connected,
            "execution_start_time": self.execution_start_time,
            "execution_end_time": self.execution_end_time,
            "execution_duration": self.execution_duration,
            "total_operations": self.total_operations,
            "successful_operations": self.successful_operations,
            "failed_operations": self.failed_operations,
            "changed_operations": self.changed_operations,
            "skipped_operations": self.skipped_operations,
            "success": self.success,
            "error": self.error,
            "operations": {
                op_name: operation.dict()
                for op_name, operation in self.operations.items()
            },
            "summary": {
                "success_rate": (
                    self.successful_operations / executed_operations * 100
                    if executed_operations > 0
                    else 0.0
                ),
                "failure_rate": (
                    self.failed_operations / executed_operations * 100
                    if executed_operations > 0
                    else 0.0
                ),
                "change_rate": (
                    self.changed_operations / executed_operations * 100
                    if executed_operations > 0
                    else 0.0
                ),
                "skip_rate": (
                    self.skipped_operations / self.total_operations * 100
                    if self.total_operations > 0
                    else 0.0
                ),
            },
        }


@dataclass
class InfraExecutionResult:
    """Enhanced result of infrastructure execution (cross-host summary).

    Attributes:
        success: Overall execution success (all hosts succeeded)
        execution_start_time: Global execution start timestamp (Unix seconds)
        execution_end_time: Global execution end timestamp (Unix seconds)
        host_results: Detailed results per host (key: IP/hostname)
        total_hosts: Total number of target hosts
        connected_hosts: Number of hosts successfully connected to
        successful_hosts: Number of hosts with full execution success
        failed_hosts: Number of hosts with execution failures/errors
        changed_hosts: Number of hosts with at least one changed operation
        global_error: Top-level execution error (e.g., invalid file path)
    """

    success: bool = False
    execution_start_time: float = 0.0
    execution_end_time: float = 0.0
    host_results: Dict[str, HostExecutionResult] = field(default_factory=dict)
    total_hosts: int = 0
    connected_hosts: int = 0
    successful_hosts: int = 0
    failed_hosts: int = 0
    changed_hosts: int = 0
    global_error: Optional[str] = None

    @property
    def execution_duration(self) -> float:
        """Calculate total global execution duration (seconds)."""
        return max(0.0, self.execution_end_time - self.execution_start_time)

    def get_host_result(self, hostname: str) -> Optional[HostExecutionResult]:
        """Get detailed result for a specific host (convenience method)."""
        return self.host_results.get(hostname)

    def get_failed_hosts(self) -> List[str]:
        """Get list of hostnames with execution failures."""
        return [h for h, res in self.host_results.items() if not res.success]

    def get_changed_hosts(self) -> List[str]:
        """Get list of hostnames with at least one changed operation."""
        return [h for h, res in self.host_results.items() if res.changed_operations > 0]

    def get_connection_failures(self) -> List[str]:
        """Get list of hostnames that failed to connect."""
        return [h for h, res in self.host_results.items() if not res.connected]

    def dict(self) -> Dict[str, Any]:
        """Convert result to dictionary for JSON serialization."""
        return {
            "success": self.success,
            "execution_start_time": self.execution_start_time,
            "execution_end_time": self.execution_end_time,
            "execution_duration": self.execution_duration,
            "total_hosts": self.total_hosts,
            "connected_hosts": self.connected_hosts,
            "successful_hosts": self.successful_hosts,
            "failed_hosts": self.failed_hosts,
            "changed_hosts": self.changed_hosts,
            "global_error": self.global_error,
            "host_results": {
                hostname: host_result.dict()
                for hostname, host_result in self.host_results.items()
            },
            "summary": {
                "failed_hosts_list": self.get_failed_hosts(),
                "changed_hosts_list": self.get_changed_hosts(),
                "connection_failures_list": self.get_connection_failures(),
                "success_rate": (
                    self.successful_hosts / self.total_hosts * 100
                    if self.total_hosts > 0
                    else 0.0
                ),
                "connection_rate": (
                    self.connected_hosts / self.total_hosts * 100
                    if self.total_hosts > 0
                    else 0.0
                ),
            },
        }

    def to_json(self, indent: Optional[int] = 2) -> str:
        """Convert result to JSON string."""
        import json

        return json.dumps(self.dict(), indent=indent, ensure_ascii=False)


class InfraFileExecutor:
    """Executor for infrastructure deployment files (simplified input: IP list + dynamic shared_data)."""

    def __init__(self, config: Optional[InfraExecutionConfig] = None) -> None:
        """Initialize infrastructure file executor.

        Args:
            config: Execution configuration, defaults to sensible defaults
        """
        self.config = config or InfraExecutionConfig()
        self._state: Optional[State] = None

    def execute_file(
        self,
        infra_file_path: Union[str, Path],
        host_ips: List[str],  # 直接接收IP字符串列表
        shared_data: Optional[Dict[str, Any]] = None,  # 共享数据
        target_groups: Optional[Dict[str, Tuple[list[str], Dict[str, Any]]]] = None,
    ) -> InfraExecutionResult:
        """Execute infrastructure deployment file (enhanced result return).

        Args:
            infra_file_path: Path to infrastructure Python file
            host_ips: List of target host IPs (str)
            shared_data: Dynamic connection config per host (key=IP, value=connection params)
                         示例: {"172.31.57.21": {"ssh_key": "~/.ssh/id_rsa", "ssh_user": "root"}}
            target_groups: Optional host group definitions (key=group name, value=IP list)

        Returns:
            InfraExecutionResult with detailed cross-host and per-host metrics
        """
        component_name = Path(infra_file_path).stem

        # 初始化全局结果
        result = InfraExecutionResult(
            execution_start_time=time.time(),
            total_hosts=len(host_ips),
            host_results={ip: HostExecutionResult(hostname=ip) for ip in host_ips},
        )

        # 初始化shared_data（默认空字典，避免None）
        shared_data = shared_data or {}

        with bind_log_context(component=component_name):
            log_lifecycle_event(
                logger,
                "component_start",
                component_file=str(infra_file_path),
                hosts_total=len(host_ips),
            )
            for host_ip, host_result in result.host_results.items():
                host_result.execution_start_time = result.execution_start_time
                with bind_log_context(host=host_ip):
                    log_lifecycle_event(logger, "host_start")

            try:
                # 验证文件存在
                file_path = Path(infra_file_path)
                if not file_path.exists():
                    raise FileNotFoundError(
                        f"Infrastructure file not found: {infra_file_path}"
                    )
                if not file_path.is_file():
                    raise IsADirectoryError(f"Path is not a file: {infra_file_path}")

                # 初始化执行环境（适配IP列表+shared_data）
                self._setup_execution_environment(
                    file_path, host_ips, shared_data, target_groups
                )

                # 执行部署并收集详细结果
                self._execute_deployment(file_path, result, shared_data)

            except KeyboardInterrupt:
                result.global_error = "Infrastructure execution interrupted"
                result.success = False
                raise
            except Exception as e:
                error_msg = f"Infrastructure execution failed: {str(e)}"
                logger.error(error_msg, exc_info=True)
                result.global_error = error_msg
                result.success = False

            finally:
                # 收尾工作：计算汇总指标、清理环境
                result.execution_end_time = time.time()
                for host_result in result.host_results.values():
                    if host_result.execution_end_time <= 0:
                        host_result.execution_end_time = result.execution_end_time
                self._calculate_summary_metrics(result)
                self._cleanup_execution()
                self._log_host_end_events(result)
                component_status = (
                    "interrupted"
                    if result.global_error == "Infrastructure execution interrupted"
                    else "success" if result.success else "failed"
                )
                log_lifecycle_event(
                    logger,
                    "component_end",
                    level=logging.INFO if result.success else logging.ERROR,
                    status=component_status,
                    duration_ms=round(result.execution_duration * 1000),
                    hosts_total=result.total_hosts,
                    hosts_successful=result.successful_hosts,
                    hosts_failed=result.failed_hosts,
                    operations_total=sum(
                        host.total_operations for host in result.host_results.values()
                    ),
                    operations_failed=sum(
                        host.failed_operations for host in result.host_results.values()
                    ),
                    error=result.global_error,
                )

        return result

    def _log_host_end_events(self, result: InfraExecutionResult) -> None:
        """记录每台目标主机的最终状态与操作汇总。"""
        for host_ip, host_result in result.host_results.items():
            if (
                host_result.connected
                and host_result.total_operations > 0
                and host_result.skipped_operations == host_result.total_operations
            ):
                status = "skipped"
            else:
                status = "success" if host_result.success else "failed"
            with bind_log_context(host=host_ip):
                log_lifecycle_event(
                    logger,
                    "host_end",
                    level=(
                        logging.INFO
                        if status in {"success", "skipped"}
                        else logging.ERROR
                    ),
                    status=status,
                    duration_ms=round(host_result.execution_duration * 1000),
                    connected=host_result.connected,
                    operations_total=host_result.total_operations,
                    operations_successful=host_result.successful_operations,
                    operations_failed=host_result.failed_operations,
                    operations_changed=host_result.changed_operations,
                    operations_skipped=host_result.skipped_operations,
                    error=host_result.error,
                )

    def execute_file_async(
        self,
        infra_file_path: Union[str, Path],
        host_ips: List[str],
        shared_data: Optional[Dict[str, Dict[str, Any]]] = None,
        target_groups: Optional[Dict[str, Tuple[list[str], Dict[str, Any]]]] = None,
    ) -> InfraExecutionResult:
        """Execute infrastructure file asynchronously (preserves enhanced results)."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        try:
            return loop.run_until_complete(
                self._execute_file_async_wrapper(
                    infra_file_path, host_ips, shared_data, target_groups
                )
            )
        finally:
            loop.close()

    async def _execute_file_async_wrapper(
        self,
        infra_file_path: Union[str, Path],
        host_ips: List[str],
        shared_data: Optional[Dict[str, Dict[str, Any]]],
        target_groups: Optional[Dict[str, Tuple[list[str], Dict[str, Any]]]],
    ) -> InfraExecutionResult:
        """Async wrapper for file execution (preserves sync logic)."""
        return await asyncio.to_thread(
            self.execute_file, infra_file_path, host_ips, shared_data, target_groups
        )

    def _setup_execution_environment(
        self,
        infra_file_path: Path,
        host_ips: List[str],
        shared_data: Dict[str, Dict[str, Any]],
        target_groups: Optional[Dict[str, Tuple[list[str], Dict[str, Any]]]],
    ) -> None:
        """Setup PyInfra execution environment (适配IP列表+dynamic shared_data)."""
        # 将文件目录加入sys.path
        infra_directory = str(infra_file_path.parent)
        if infra_directory not in sys.path:
            sys.path.insert(0, infra_directory)

        # 初始化PyInfra状态
        # 注意：pyinfra 的 check_for_changes 表示"注册操作时先取 fact 预判变更"，
        # 不是 dry-run；默认关闭，见 InfraExecutionConfig.check_for_changes。
        self._state = State(check_for_changes=bool(self.config.check_for_changes))
        self._state.cwd = str(infra_file_path.parent)
        ctx_state.set(self._state)  # type: ignore

        # 配置PyInfra
        config: "Config" = Config()  # type: ignore[no-untyped-call]
        config.PARALLEL = self.config.parallel
        config.CONNECT_TIMEOUT = self.config.connect_timeout
        config.FAIL_PERCENT = self.config.fail_percent
        ctx_config.set(config)  # type: ignore

        # 准备inventory数据（核心：IP列表 + 动态shared_data）
        # shared_data 是发给所有主机的公共数据（master_ip、deploy_src 等），
        # pyinfra 会把其中的键当作 host.data 的属性；_timeout 放在这里就变成
        # 所有操作的默认超时（单条操作显式传 _timeout 仍然优先生效）。
        inventory_data: Dict[str, Any] = dict(shared_data)
        inventory_data.setdefault("_timeout", self.config.op_timeout)
        inventory = Inventory(  # type: ignore[no-untyped-call]
            (host_ips, inventory_data),  # 直接传入IP列表和动态连接配置
            **(target_groups or {}),
        )
        ctx_inventory.set(inventory)  # type: ignore

        # 初始化state
        self._state.init(inventory, config)  # type: ignore
        self._state.add_callback_handler(InfraLifecycleCallback())
        self._enable_greenlet_context_propagation()

    def _enable_greenlet_context_propagation(self) -> None:
        """让 PyInfra 创建的 gevent greenlet 继承提交时的日志上下文。"""
        if not self._state or self._state.pool is None:
            return

        original_spawn = self._state.pool.spawn

        def spawn_with_context(
            function: Callable[..., Any], *args: Any, **kwargs: Any
        ) -> Any:
            context = copy_context()
            return original_spawn(context.run, function, *args, **kwargs)

        self._state.pool.spawn = spawn_with_context  # type: ignore[method-assign]

    def _execute_deployment(
        self,
        infra_file_path: Path,
        result: InfraExecutionResult,
        shared_data: Dict[str, Dict[str, Any]],
    ) -> None:
        """Execute deployment and populate enhanced result object (适配IP列表)."""
        if not self._state:
            raise RuntimeError("Execution environment not properly initialized")

        # 1. 连接所有主机
        with bind_log_context(stage="connect"):
            logger.info("--> Connecting to hosts...")
            self._state.set_stage(StateStage.Connect)
            connect_all(self._state)

        # 标记连接状态到结果
        for ip, host_result in result.host_results.items():
            host_result.connected = ip in [
                str(host) for host in self._state.activated_hosts
            ]
            if not host_result.connected:
                host_result.error = (
                    "Failed to connect to host (check SSH config in shared_data)"
                )
                with bind_log_context(stage="connect", host=ip):
                    logger.warning(f"Host {ip} failed to connect")

        # 检查是否有可用主机
        if not self._state.activated_hosts:
            raise RuntimeError("No hosts were successfully connected")

        # 2. 加载部署文件
        logger.info(f"--> Loading infrastructure file: {infra_file_path}")
        self._state.set_stage(StateStage.Prepare)
        module_name = infra_file_path.stem

        spec = importlib.util.spec_from_file_location(module_name, str(infra_file_path))
        if not spec or not spec.loader:
            raise RuntimeError(f"Failed to load module spec for {infra_file_path}")
        module = importlib.util.module_from_spec(spec)

        # 3. 遍历激活的主机执行部署
        logger.info(f"--> Found {len(self._state.activated_hosts)} activated hosts")
        for activated_host in self._state.activated_hosts:
            host_ip = str(activated_host)
            host = self._state.inventory.hosts.get(host_ip)
            if not host:
                logger.warning(f"Host '{host_ip}' not found in inventory")
                continue

            # 获取当前主机的结果对象
            host_result = result.host_results[host_ip]
            host_result.connected = True

            try:
                with bind_log_context(stage="prepare", host=host_ip):
                    logger.info(f"--> Executing operations for host: {host_ip}")
                    # 绑定主机上下文并执行部署文件
                    ctx_host.set(host)  # type: ignore
                    spec.loader.exec_module(module)

            except SystemExit as e:
                # 部署文件通过 exit(0) 表示该主机跳过当前部署（如 worker 跳过 keepalived）
                # exit code 为 0 视为正常跳过；非 0 视为错误
                if e.code == 0:
                    logger.info(f"--> Host {host_ip} skipped this deployment")
                else:
                    error_msg = f"Module exited with code {e.code} on host {host_ip}"
                    logger.error(error_msg)
                    host_result.error = error_msg
                continue
            except Exception as e:
                error_msg = f"Failed to execute module on host {host_ip}: {str(e)}"
                logger.error(error_msg)
                host_result.error = error_msg
                continue
            finally:
                ctx_host.reset()  # 重置上下文

        # 4. 执行PyInfra操作
        with bind_log_context(stage="execute"):
            logger.info("--> Beginning deployment operations...")
            self._state.set_stage(StateStage.Execute)
            run_ops(self._state)

            # 5. 收集每个主机的操作结果
            self._collect_operation_results(result)

            # 6. 打印汇总结果
            self._state.set_stage(StateStage.Disconnect)
            print_results(self._state)
            logger.info("--> Deployment completed")

    def _collect_operation_results(self, result: InfraExecutionResult) -> None:
        """通过 PyInfra 3.6 公共状态接口采集操作结果。

        PyInfra 将主机级计数存放在 ``state.results``，实际操作结果位于
        ``state.get_op_data_for_host(...).operation_meta``。旧实现把
        ``state.results[host]`` 当作映射读取，导致 PyInfra 3.6 下操作数始终为零。
        这里采用严格采集策略：接口不兼容或操作结果不完整时明确判定为失败，
        避免将结果未知的部署误报为空操作成功。
        """
        if not self._state:
            return

        raw_get_op_order = getattr(self._state, "get_op_order", None)
        raw_get_op_meta = getattr(self._state, "get_op_meta", None)
        raw_get_op_data = getattr(self._state, "get_op_data_for_host", None)
        if not all(
            callable(method)
            for method in (raw_get_op_order, raw_get_op_meta, raw_get_op_data)
        ):
            raise RuntimeError(
                "Unsupported PyInfra state API: operation results cannot be collected"
            )

        get_op_order = cast(Callable[[], Any], raw_get_op_order)
        get_op_meta = cast(Callable[[str], Any], raw_get_op_meta)
        get_op_data = cast(Callable[[Any, str], Any], raw_get_op_data)

        try:
            op_order = list(get_op_order())
        except Exception as exc:
            raise RuntimeError(
                f"Unable to read PyInfra operation order: {exc}"
            ) from exc

        def operation_name(op_hash: str) -> str:
            state_op_meta = get_op_meta(op_hash)
            names = sorted(str(name) for name in getattr(state_op_meta, "names", set()))
            return ", ".join(names) or f"operation_{str(op_hash)[:8]}"

        def unique_name(base_name: str, counters: Dict[str, int]) -> str:
            occurrence = counters.get(base_name, 0)
            counters[base_name] = occurrence + 1
            return base_name if occurrence == 0 else f"{base_name}_{occurrence}"

        for host in self._state.activated_hosts:
            host_ip = str(host)
            host_result = result.host_results.get(host_ip)
            if not host_result:
                logger.warning(f"Host {host_ip} not found in result tracking")
                continue
            if not host_result.connected:
                continue

            op_name_counter: Dict[str, int] = {}
            previous_operation_failed = False
            current_operation: Optional[str] = None
            try:
                for op_hash in op_order:
                    base_name = operation_name(op_hash)
                    current_operation = base_name
                    op_name = unique_name(base_name, op_name_counter)

                    try:
                        op_data = get_op_data(host, op_hash)
                    except KeyError:
                        host_result.operations[op_name] = HostOperationResult(
                            operation_name=op_name,
                            success=True,
                            skipped=True,
                        )
                        host_result.total_operations += 1
                        host_result.skipped_operations += 1
                        with bind_log_context(host=host_ip, operation=op_name):
                            log_lifecycle_event(
                                logger, "operation_skipped", status="skipped"
                            )
                        continue

                    op_meta = getattr(op_data, "operation_meta", None)
                    if op_meta is None:
                        raise RuntimeError(
                            f"Operation {base_name} on {host_ip} has no result metadata"
                        )

                    is_complete = getattr(op_meta, "is_complete", None)
                    if not callable(is_complete) or not is_complete():
                        if previous_operation_failed:
                            host_result.operations[op_name] = HostOperationResult(
                                operation_name=op_name,
                                success=True,
                                skipped=True,
                            )
                            host_result.total_operations += 1
                            host_result.skipped_operations += 1
                            with bind_log_context(host=host_ip, operation=op_name):
                                log_lifecycle_event(
                                    logger,
                                    "operation_skipped",
                                    status="skipped",
                                    reason="previous_operation_failed",
                                )
                            continue
                        raise RuntimeError(
                            f"Operation {base_name} on {host_ip} has no completed result"
                        )

                    op_success = bool(op_meta.did_succeed())
                    op_changed = bool(op_meta.did_change())
                    stdout_lines = [str(line) for line in op_meta.stdout_lines]
                    stderr_lines = [str(line) for line in op_meta.stderr_lines]
                    op_output = stdout_lines + stderr_lines
                    op_error = None
                    if not op_success:
                        op_error = "\n".join(stderr_lines) or "PyInfra operation failed"

                    host_op_result = HostOperationResult(
                        operation_name=op_name,
                        success=op_success,
                        changed=op_changed,
                        output=op_output,
                        error=op_error,
                    )
                    host_result.operations[op_name] = host_op_result
                    host_result.total_operations += 1
                    if op_success:
                        host_result.successful_operations += 1
                    else:
                        host_result.failed_operations += 1
                    if op_changed:
                        host_result.changed_operations += 1
                    previous_operation_failed = not op_success

            except Exception as e:
                with bind_log_context(host=host_ip, operation=current_operation):
                    logger.error(
                        f"Error collecting results for host {host_ip}: {str(e)}",
                        exc_info=True,
                    )
                host_result.error = f"Failed to collect operation results: {str(e)}"

        # 记录总体统计信息
        total_ops = sum(len(host.operations) for host in result.host_results.values())
        logger.info(
            f"Collected {total_ops} total operations across {len(result.host_results)} hosts"
        )

    def _calculate_summary_metrics(self, result: InfraExecutionResult) -> None:
        """Calculate cross-host summary metrics for the global result."""
        # 初始化统计
        connected = 0
        successful = 0
        failed = 0
        changed = 0

        # 遍历主机结果计算汇总
        for host_result in result.host_results.values():
            if host_result.connected:
                connected += 1
            if host_result.success:
                successful += 1
            else:
                failed += 1
            if host_result.changed_operations > 0:
                changed += 1

        # 更新全局结果
        result.connected_hosts = connected
        result.successful_hosts = successful
        result.failed_hosts = failed
        result.changed_hosts = changed
        result.success = (
            result.global_error is None
            and result.failed_hosts == 0
            and result.total_hosts > 0
        )

    def _cleanup_execution(self) -> None:
        """Clean up execution environment (enhanced context reset)."""
        if self._state and self._state.initialised:
            logger.info("--> Cleaning up execution environment")

            # 断开主机连接
            try:
                # 简化版本：不使用progress_spinner，直接断开连接
                activated_hosts = getattr(self._state, "activated_hosts", [])
                greenlets: list[Any] = []

                for host in activated_hosts:
                    greenlet = self._state.pool.spawn(host.disconnect)
                    greenlets.append(greenlet)

                # 等待所有断开连接完成
                for greenlet in greenlets:
                    try:
                        greenlet.get()
                    except Exception as e:
                        logger.debug(f"Host disconnect error: {e}")

            except Exception as e:
                logger.warning(f"Error during cleanup: {str(e)}")

        # 重置所有上下文
        ctx_state.reset()
        ctx_config.reset()
        ctx_inventory.reset()
        ctx_host.reset()
        self._state = None

    def execute_files(
        self,
        infra_file_paths: Union[List[Union[str, Path]], Dict[str, Union[str, Path]]],
        host_ips: List[str],
        shared_data: Optional[Dict[str, Any]] = None,
        target_groups: Optional[Dict[str, Tuple[list[str], Dict[str, Any]]]] = None,
        execution_mode: str = "sequential",
        fail_fast: Optional[bool] = None,
    ) -> InfraExecutionResult:
        """Execute multiple infrastructure deployment files.

        Args:
            infra_file_paths: List of file paths or dict with custom names
            host_ips: List of target host IPs
            shared_data: Dynamic connection config per host
            target_groups: Optional host group definitions
            execution_mode: "sequential" (default) or "parallel"
            fail_fast: Override config.fail_fast setting

        Returns:
            InfraExecutionResult with aggregated results across all files
        """
        # 使用传入的fail_fast参数或配置中的值
        should_fail_fast = fail_fast if fail_fast is not None else self.config.fail_fast

        # 准备文件路径映射
        if isinstance(infra_file_paths, list):
            file_mapping = {Path(fp).stem: fp for fp in infra_file_paths}
        else:
            file_mapping = infra_file_paths

        # 初始化结果对象
        result = InfraExecutionResult(
            execution_start_time=time.time(),
            total_hosts=len(host_ips),
            host_results={ip: HostExecutionResult(hostname=ip) for ip in host_ips},
        )

        shared_data = shared_data or {}

        try:
            # 验证所有文件存在
            for name, file_path in file_mapping.items():
                path = Path(file_path)
                if not path.exists():
                    raise FileNotFoundError(
                        f"Infrastructure file not found: {file_path} (name: {name})"
                    )
                if not path.is_file():
                    raise IsADirectoryError(
                        f"Path is not a file: {file_path} (name: {name})"
                    )

            if execution_mode == "parallel":
                result = self._execute_files_parallel(
                    file_mapping, host_ips, shared_data, target_groups, result
                )
            else:
                result = self._execute_files_sequential_with_strategy(
                    file_mapping,
                    host_ips,
                    shared_data,
                    target_groups,
                    result,
                    should_fail_fast,
                )

        except Exception as e:
            error_msg = f"Multiple files execution failed: {str(e)}"
            logger.error(error_msg, exc_info=True)
            result.global_error = error_msg
            result.success = False

        finally:
            result.execution_end_time = time.time()
            self._calculate_summary_metrics(result)

        return result

    def _execute_files_sequential_with_strategy(
        self,
        file_mapping: Dict[str, Union[str, Path]],
        host_ips: List[str],
        shared_data: Dict[str, Any],
        target_groups: Optional[Dict[str, Tuple[list[str], Dict[str, Any]]]],
        result: InfraExecutionResult,
        fail_fast: bool,
    ) -> InfraExecutionResult:
        """Execute multiple files sequentially with configurable fail strategy."""
        if fail_fast:
            return self._execute_files_sequential_fail_fast(
                file_mapping, host_ips, shared_data, target_groups, result
            )
        else:
            return self._execute_files_sequential_continue_on_failure(
                file_mapping, host_ips, shared_data, target_groups, result
            )

    def _execute_files_sequential_fail_fast(
        self,
        file_mapping: Dict[str, Union[str, Path]],
        host_ips: List[str],
        shared_data: Dict[str, Any],
        target_groups: Optional[Dict[str, Tuple[list[str], Dict[str, Any]]]],
        result: InfraExecutionResult,
    ) -> InfraExecutionResult:
        """Execute multiple files sequentially with fail-fast behavior."""
        file_results: dict[str, Any] = {}
        total_operations = 0

        for file_name, file_path in file_mapping.items():
            logger.info(f"--> Executing infrastructure file: {file_name} ({file_path})")

            try:
                # 为每个文件创建新的执行器实例
                single_executor = InfraFileExecutor(self.config)

                # 执行单个文件
                file_result = single_executor.execute_file(
                    infra_file_path=file_path,
                    host_ips=host_ips,
                    shared_data=shared_data,
                    target_groups=target_groups,
                )

                # 记录当前文件执行结果
                file_results[file_name] = {
                    "success": file_result.success,
                    "duration": file_result.execution_duration,
                    "error": file_result.global_error,
                }

                # 检查文件执行是否失败
                if not file_result.success:
                    error_msg = f"File {file_name} execution failed: {file_result.global_error or 'Unknown error'}"
                    logger.error(error_msg)

                    # 合并部分结果（失败前的文件结果）
                    result = self._merge_execution_results(
                        result, file_result, file_name
                    )

                    # 设置全局错误并立即返回
                    result.global_error = error_msg
                    result.success = False

                    # 为所有主机添加失败标记操作
                    for host_result in result.host_results.values():
                        if host_result.connected:
                            failed_op = HostOperationResult(
                                operation_name=f"execution_stopped_at_{file_name}",
                                success=False,
                                error=f"Execution stopped due to failure in file: {file_name}",
                            )
                            host_result.operations[
                                f"execution_stopped_at_{file_name}"
                            ] = failed_op
                            host_result.total_operations += 1
                            host_result.failed_operations += 1

                    logger.info(
                        f"--> Stopping further file execution due to failure in: {file_name}"
                    )
                    return result

                # 文件执行成功，合并结果
                result = self._merge_execution_results(result, file_result, file_name)

                total_operations += sum(
                    host.total_operations for host in file_result.host_results.values()
                )

                logger.info(f"--> File {file_name} completed successfully")

            except Exception as e:
                error_msg = (
                    f"Exception occurred while executing file {file_name}: {str(e)}"
                )
                logger.error(error_msg, exc_info=True)

                # 设置全局错误并立即返回
                result.global_error = error_msg
                result.success = False

                # 为所有主机添加异常失败标记
                for host_result in result.host_results.values():
                    if host_result.connected:
                        failed_op = HostOperationResult(
                            operation_name=f"execution_exception_at_{file_name}",
                            success=False,
                            error=error_msg,
                        )
                        host_result.operations[
                            f"execution_exception_at_{file_name}"
                        ] = failed_op
                        host_result.total_operations += 1
                        host_result.failed_operations += 1

                # 记录失败文件信息
                file_results[file_name] = {"success": False, "error": error_msg}

                logger.info(
                    f"--> Stopping further file execution due to exception in: {file_name}"
                )
                return result

        # 所有文件都成功执行
        result.global_error = None
        result.success = True

        # 添加成功执行的文件汇总信息
        for host_result in result.host_results.values():
            host_result.operations["_file_execution_summary"] = HostOperationResult(
                operation_name="file_execution_summary",
                success=True,
                output=[
                    f"Successfully executed {len(file_mapping)} files",
                    f"Files executed: {list(file_results.keys())}",
                    f"Total operations: {total_operations}",
                ],
            )

        logger.info(f"--> All {len(file_mapping)} files executed successfully")
        return result

    def _execute_files_sequential_continue_on_failure(
        self,
        file_mapping: Dict[str, Union[str, Path]],
        host_ips: List[str],
        shared_data: Dict[str, Any],
        target_groups: Optional[Dict[str, Tuple[list[str], Dict[str, Any]]]],
        result: InfraExecutionResult,
    ) -> InfraExecutionResult:
        """Execute multiple files sequentially."""
        file_results: dict[str, Any] = {}
        total_operations = 0

        for file_name, file_path in file_mapping.items():
            logger.info(f"--> Executing infrastructure file: {file_name} ({file_path})")

            try:
                # 为每个文件创建新的执行器实例
                single_executor = InfraFileExecutor(self.config)

                # 执行单个文件
                file_result = single_executor.execute_file(
                    infra_file_path=file_path,
                    host_ips=host_ips,
                    shared_data=shared_data,
                    target_groups=target_groups,
                )

                # 合并结果到主结果对象
                result = self._merge_execution_results(result, file_result, file_name)

                file_results[file_name] = {
                    "success": file_result.success,
                    "duration": file_result.execution_duration,
                    "error": file_result.global_error,
                }

                total_operations += sum(
                    host.total_operations for host in file_result.host_results.values()
                )

                logger.info(f"--> File {file_name} completed successfully")

            except Exception as e:
                error_msg = f"Failed to execute file {file_name}: {str(e)}"
                logger.error(error_msg)

                # 标记所有主机在当前文件上的操作为失败
                for host_result in result.host_results.values():
                    if host_result.connected:
                        failed_op = HostOperationResult(
                            operation_name=f"file_execution_{file_name}",
                            success=False,
                            error=error_msg,
                        )
                        host_result.operations[f"file_execution_{file_name}"] = (
                            failed_op
                        )
                        host_result.total_operations += 1
                        host_result.failed_operations += 1

                file_results[file_name] = {"success": False, "error": error_msg}

        # 添加文件执行汇总信息
        result.global_error = None
        if not all(fr["success"] for fr in file_results.values()):
            failed_files = [
                name for name, fr in file_results.items() if not fr["success"]
            ]
            result.global_error = f"Failed files: {', '.join(failed_files)}"

        # 记录文件执行结果
        for host_result in result.host_results.values():
            host_result.operations["_file_execution_summary"] = HostOperationResult(
                operation_name="file_execution_summary",
                success=result.success,
                output=[
                    f"Executed {len(file_mapping)} files",
                    f"Files summary: {file_results}",
                ],
            )

        return result

    def _execute_files_parallel(
        self,
        file_mapping: Dict[str, Union[str, Path]],
        host_ips: List[str],
        shared_data: Dict[str, Any],
        target_groups: Optional[Dict[str, Tuple[list[str], Dict[str, Any]]]],
        result: InfraExecutionResult,
    ) -> InfraExecutionResult:
        """Execute multiple files in parallel."""
        import concurrent.futures

        file_results = {}

        def execute_single_file(
            file_name: str, file_path: Union[str, Path]
        ) -> tuple[str, InfraExecutionResult]:
            """Execute single file in thread."""
            # 为每个线程创建独立的执行器实例
            single_executor = InfraFileExecutor(self.config)

            try:
                file_result = single_executor.execute_file(
                    infra_file_path=file_path,
                    host_ips=host_ips,
                    shared_data=shared_data,
                    target_groups=target_groups,
                )
                return file_name, file_result
            except Exception as e:
                # 创建失败的结果对象
                error_result = InfraExecutionResult(
                    success=False,
                    global_error=f"Thread execution failed for {file_name}: {str(e)}",
                    execution_start_time=time.time(),
                    execution_end_time=time.time(),
                    total_hosts=len(host_ips),
                    host_results={
                        ip: HostExecutionResult(hostname=ip) for ip in host_ips
                    },
                )
                return file_name, error_result

        # 并行执行所有文件
        logger.info(f"--> Executing {len(file_mapping)} files in parallel")

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self.config.parallel
        ) as executor:
            # 提交所有任务
            future_to_name = {
                executor.submit(
                    copy_context().run,
                    execute_single_file,
                    name,
                    path,
                ): name
                for name, path in file_mapping.items()
            }

            # 收集结果
            for future in concurrent.futures.as_completed(future_to_name):
                file_name = future_to_name[future]
                try:
                    _, file_result = future.result()
                    result = self._merge_execution_results(
                        result, file_result, file_name
                    )
                    file_results[file_name] = {
                        "success": file_result.success,
                        "duration": file_result.execution_duration,
                        "error": file_result.global_error,
                    }
                    logger.info(f"--> Parallel file {file_name} completed")
                except Exception as e:
                    error_msg = f"Parallel execution error for {file_name}: {str(e)}"
                    logger.error(error_msg)
                    file_results[file_name] = {"success": False, "error": error_msg}

        # 记录并行执行汇总
        for host_result in result.host_results.values():
            host_result.operations["_parallel_execution_summary"] = HostOperationResult(
                operation_name="parallel_execution_summary",
                success=result.success,
                output=[
                    f"Parallel execution of {len(file_mapping)} files completed",
                    f"Files summary: {file_results}",
                ],
            )

        return result

    def _merge_execution_results(
        self,
        main_result: InfraExecutionResult,
        file_result: InfraExecutionResult,
        file_name: str,
    ) -> InfraExecutionResult:
        """Merge single file execution result into main result."""

        # 合并每个主机的操作结果
        for host_ip, host_main_result in main_result.host_results.items():
            if host_ip in file_result.host_results:
                host_file_result = file_result.host_results[host_ip]

                # 更新连接状态（只要有一个文件连接成功就认为是已连接）
                host_main_result.connected = (
                    host_main_result.connected or host_file_result.connected
                )

                # 合并操作结果，添加文件名前缀避免重复
                for op_name, op_result in host_file_result.operations.items():
                    prefixed_name = f"{file_name}_{op_name}"
                    host_main_result.operations[prefixed_name] = op_result

                    # 更新统计
                    host_main_result.total_operations += 1
                    if op_result.skipped:
                        host_main_result.skipped_operations += 1
                    elif op_result.success:
                        host_main_result.successful_operations += 1
                    else:
                        host_main_result.failed_operations += 1
                    if op_result.changed:
                        host_main_result.changed_operations += 1

                # 合并错误信息
                if host_file_result.error and not host_main_result.error:
                    host_main_result.error = f"[{file_name}] {host_file_result.error}"

        return main_result

    def execute_files_async(
        self,
        infra_file_paths: Union[List[Union[str, Path]], Dict[str, Union[str, Path]]],
        host_ips: List[str],
        shared_data: Optional[Dict[str, Dict[str, Any]]] = None,
        target_groups: Optional[Dict[str, Tuple[list[str], Dict[str, Any]]]] = None,
        execution_mode: str = "sequential",
    ) -> InfraExecutionResult:
        """Execute multiple infrastructure files asynchronously."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        try:
            return loop.run_until_complete(
                self._execute_files_async_wrapper(
                    infra_file_paths,
                    host_ips,
                    shared_data,
                    target_groups,
                    execution_mode,
                )
            )
        finally:
            loop.close()

    async def _execute_files_async_wrapper(
        self,
        infra_file_paths: Union[List[Union[str, Path]], Dict[str, Union[str, Path]]],
        host_ips: List[str],
        shared_data: Optional[Dict[str, Dict[str, Any]]],
        target_groups: Optional[Dict[str, Tuple[list[str], Dict[str, Any]]]],
        execution_mode: str,
    ) -> InfraExecutionResult:
        """Async wrapper for multiple files execution."""
        return await asyncio.to_thread(
            self.execute_files,
            infra_file_paths,
            host_ips,
            shared_data,
            target_groups,
            execution_mode,
        )


# ------------------------------
# 简化的快捷调用示例（符合你的使用习惯）
# ------------------------------
if __name__ == "__main__":
    # 1. 定义IP列表
    host_ips = ["172.31.57.21"]

    # 2. 动态传入每个IP的连接配置（shared_data）
    shared_data: dict[str, Any] = {
        "172.31.57.21": {
            "ssh_key": "~/.ssh/id_rsa",  # SSH私钥
            "ssh_user": "root",  # 登录用户
            "ssh_port": 22,  # 端口（可选，默认22）
            # 可添加其他参数：ssh_password、timeout等
        }
    }

    # 3. 初始化执行器
    executor = InfraFileExecutor()

    # 4. 执行部署文件
    result = executor.execute_file(
        infra_file_path="/opt/kubengine/src/infra/test_infra.py",
        host_ips=host_ips,
        shared_data=shared_data,
    )

    # 5. 查看精细化返回结果
    print(f"result: {result.to_json()}")
    print(f"整体执行是否成功: {result.success}")
    print(f"总执行时长: {result.execution_duration:.2f}秒")
    print(f"失败主机列表: {result.get_failed_hosts()}")
    print(f"变更主机列表: {result.get_changed_hosts()}")

    # 单主机详情
    host_result = result.get_host_result("172.31.57.21")
    if host_result:
        print(f"\n主机 {host_result.hostname} 执行详情:")
        print(f"连接状态: {host_result.connected}")
        print(f"执行时长: {host_result.execution_duration:.2f}秒")
        print(f"成功操作数: {host_result.successful_operations}")
        print(f"失败操作数: {host_result.failed_operations}")
        print(f"变更操作数: {host_result.changed_operations}")

        # 操作级详情
        for op_name, op_result in host_result.operations.items():
            print(f"\n操作 {op_name}:")
            print(f"  成功: {op_result.success}")
            print(f"  变更: {op_result.changed}")
            if op_result.error:
                print(f"  错误: {op_result.error}")
            if op_result.output:
                print(f"  输出: {op_result.output}")
