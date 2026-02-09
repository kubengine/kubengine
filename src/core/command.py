"""Command execution utility module.

This module provides utilities for executing shell commands with timeout control,
real-time output logging, and error handling capabilities.
"""

from core.logger import get_logger
from typing import Optional, Literal
from threading import Thread
import subprocess
import sys


log = get_logger(__name__)


class CommandResult:
    """Encapsulates result of command execution with convenient access methods."""

    def __init__(self, return_code: int, stdout: str, stderr: str) -> None:
        """Initialize command result.

        Args:
            return_code: The exit code of command.
            stdout: Standard output content.
            stderr: Standard error content.
        """
        self._return_code = return_code
        self._stdout = stdout
        self._stderr = stderr

    @property
    def return_code(self) -> int:
        """Get return code of command."""
        return self._return_code

    @property
    def stdout(self) -> str:
        """Get standard output of command."""
        return self._stdout

    @property
    def stderr(self) -> str:
        """Get standard error output of the command."""
        return self._stderr

    def is_success(self) -> bool:
        """Check if command executed successfully.

        Returns:
            True if return code is 0, False otherwise.
        """
        return self._return_code == 0

    def is_failure(self) -> bool:
        """Check if command execution failed.

        Returns:
            True if return code is not 0, False otherwise.
        """
        return self._return_code != 0

    def get_output_lines(self) -> list[str]:
        """Get stdout as a list of lines.

        Returns:
            List of lines from stdout, excluding empty lines.
        """
        return [line for line in self._stdout.split('\n') if line.strip()]

    def get_error_lines(self) -> list[str]:
        """Get stderr as a list of lines.

        Returns:
            List of lines from stderr, excluding empty lines.
        """
        return [line for line in self._stderr.split('\n') if line.strip()]

    def to_dict(self) -> dict[str, str | int]:
        """Convert result to dictionary format for backward compatibility.

        Returns:
            Dictionary with 'ret', 'out', 'err' keys.
        """
        return {
            'ret': self._return_code,
            'out': self._stdout,
            'err': self._stderr
        }

    def raise_if_failed(self, error_message: Optional[str] = None) -> 'CommandResult':
        """Raise exception if command failed.

        Args:
            error_message: Custom error message

        Returns:
            Self for method chaining

        Raises:
            Exception: If command failed
        """
        if self.is_failure():
            msg = error_message or f"Command failed with return code {self._return_code}"
            if self.stderr:
                msg += f": {self.stderr}"
            raise Exception(msg)
        return self

    def exit_if_failed(self, exit_code: int = 1, error_message: Optional[str] = None) -> 'CommandResult':
        """Exit process if command failed.

        Args:
            exit_code: Exit code to use
            error_message: Custom error message

        Returns:
            Self for method chaining
        """
        if self.is_failure():
            msg = error_message or f"Command failed with return code {self._return_code}"
            if self.stderr:
                msg += f": {self.stderr}"
            log.error(msg)
            sys.exit(exit_code)
        return self

    def __str__(self) -> str:
        """String representation of command result."""
        status = "SUCCESS" if self.is_success() else "FAILURE"
        return f"CommandResult[{status}](return_code={self._return_code})"

    def __repr__(self) -> str:
        """Detailed string representation of command result."""
        return (f"CommandResult(return_code={self._return_code}, "
                f"stdout_length={len(self._stdout)}, "
                f"stderr_length={len(self._stderr)})")


class CommandError(Exception):
    """Exception raised when command execution fails."""

    def __init__(self, command: str, result: CommandResult, message: Optional[str] = None):
        """Initialize command error.

        Args:
            command: The command that failed
            result: Command result object
            message: Custom error message
        """
        self.command = command
        self.result = result
        self.message = message or f"Command failed: {command}"

        super().__init__(self.message)


def execute_command(
    cmd: str,
    timeout: int = 6000,
    log_output: bool = True,
    *,
    # 新增的错误处理参数
    fail_action: Optional[Literal['exit', 'raise', 'none']] = None,
    exit_code: int = 1,
    error_message: Optional[str] = None,
    # 向后兼容参数
    exit: Optional[bool] = None,
) -> CommandResult:
    """Execute a shell command with comprehensive error handling.

    Args:
        cmd: The shell command to execute.
        timeout: Timeout in seconds, defaults to 6000.
        log_output: Whether to log output, defaults to True.
        fail_action: How to handle failure:
            - 'exit': Exit the program (default when exit=True)
            - 'raise': Raise exception
            - 'none': Return result (default)
        exit_code: Exit code when fail_action='exit'
        error_message: Custom error message
        exit: Backward compatibility - if True, equivalent to fail_action='exit'

    Returns:
        CommandResult object containing execution results.

    Raises:
        CommandError: If fail_action='raise' and command fails
        SystemExit: If fail_action='exit' and command fails
    """
    # 向后兼容性处理
    if exit is not None:
        fail_action = 'exit' if exit else 'none'
    elif fail_action is None:
        fail_action = 'none'

    # 核心执行逻辑
    result = _execute_command_core(cmd, timeout, log_output)

    # 统一的错误处理
    if result.is_failure() and fail_action != 'none':
        _handle_command_failure(cmd, result, fail_action,
                                exit_code, error_message)

    return result


def _execute_command_core(
    cmd: str,
    timeout: int,
    log_output: bool
) -> CommandResult:
    """Core command execution logic."""

    def _execute_subprocess(command: str, log_enabled: bool) -> CommandResult:
        """Execute subprocess and capture output in real-time."""
        process = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            bufsize=1
        )

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        def _read_stdout() -> None:
            """Read stdout in real-time."""
            try:
                for line in iter(process.stdout.readline, ''):  # type: ignore
                    line = line.rstrip()
                    if line:
                        stdout_lines.append(line)
                        if log_enabled:
                            log.debug(f"[STDOUT] {line}")
            except Exception as e:
                log.error(f"Error reading stdout: {e}")

        def _read_stderr() -> None:
            """Read stderr in real-time."""
            try:
                for line in iter(process.stderr.readline, ''):  # type: ignore
                    line = line.rstrip()
                    if line:
                        stderr_lines.append(line)
                        if log_enabled:
                            log.debug(f"[STDERR] {line}")
            except Exception as e:
                log.error(f"Error reading stderr: {e}")

        stdout_thread = Thread(target=_read_stdout, daemon=True)
        stderr_thread = Thread(target=_read_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        return_code = process.wait()

        stdout_thread.join(timeout=0.1)
        stderr_thread.join(timeout=0.1)

        output = '\n'.join(stdout_lines)
        error_output = '\n'.join(stderr_lines)

        return CommandResult(return_code, output, error_output)

    class CommandThread(Thread):
        """Custom thread class for command execution with timeout handling."""

        def __init__(self, command: str, log_enabled: bool) -> None:
            """Initialize command thread."""
            super().__init__()
            self.command = command
            self.log_enabled = log_enabled
            self.result: Optional[CommandResult] = None
            self.error: Optional[Exception] = None

        def run(self) -> None:
            """Execute command in thread."""
            try:
                self.result = _execute_subprocess(
                    self.command, self.log_enabled)
            except Exception as e:
                self.error = e

        def stop_thread(self) -> None:
            """Stop thread execution."""
            if hasattr(self, '_Thread__stop'):
                self._Thread__stop()  # type: ignore

            if self.is_alive():
                import ctypes
                res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
                    ctypes.c_long(self.ident or 0), ctypes.py_object(
                        SystemExit)
                )
                if res > 1:
                    ctypes.pythonapi.PyThreadState_SetAsyncExc(
                        self.ident, None)

    command_thread = CommandThread(cmd, log_output)
    command_thread.start()

    log.debug(f"Executing command: {cmd}")

    command_thread.join(timeout)

    if command_thread.error is not None:
        error = command_thread.error
        result = CommandResult(1, '', 'execute command error!')
        log.error(f'Execute command [{cmd}] got exception [{error}]!')
    elif command_thread.result is None:
        result = CommandResult(1, '', 'execute command timeout!')
        log.error(f'Execute command [{cmd}] timeout [{timeout}]s!')
    else:
        result = command_thread.result
        log.debug(
            f'Command [{cmd}] finished with return code: {result.return_code}')

    if command_thread.is_alive():
        log.warning("Command thread still alive after timeout, stopping...")
        command_thread.stop_thread()
        command_thread.join(timeout=1)
        if command_thread.is_alive():
            log.error("Failed to stop command thread!")

    return result


def _handle_command_failure(
    cmd: str,
    result: CommandResult,
    fail_action: Literal['exit', 'raise'],
    exit_code: int,
    error_message: Optional[str]
) -> None:
    """Handle command failure based on action type."""

    if error_message is None:
        error_message = f'The command [{cmd}] execution failed'
        if result.stderr:
            error_message += f'. Error: [{result.stderr}]'

    log.error(error_message)

    if fail_action == 'exit':
        sys.exit(exit_code)
    elif fail_action == 'raise':
        raise CommandError(cmd, result, error_message)


if __name__ == "__main__":
    # Example usage with unified function

    # 原来的 execute_command 用法
    result1 = execute_command("ls -l")
    print(f"Result: {result1.is_success()}")

    result2 = execute_command("ls -l", fail_action='exit')

    try:
        result3 = execute_command("false", fail_action='raise')
    except CommandError as e:
        print(f"Caught error: {e}")

    # 新的方法链式用法
    result4 = execute_command("ls -l").raise_if_failed("List command failed")
    result5 = execute_command("ls -l").exit_if_failed(exit_code=2)
