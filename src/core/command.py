"""Command execution utility module.

This module provides utilities for executing shell commands with timeout control,
real-time output logging, and error handling capabilities.
"""

from core.logger import get_logger
from typing import BinaryIO, Optional, Literal
import os
import signal
import subprocess
import sys
import tempfile


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

    def exit_if_failed(
        self, exit_code: int = 1, error_message: Optional[str] = None
    ) -> 'CommandResult':
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
    timeout: float = 6000,
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
    timeout: float,
    log_output: bool
) -> CommandResult:
    """Execute a command and guarantee that a timeout kills all descendants.

    Output is spooled to temporary files instead of unbounded in-memory pipe
    readers.  ``start_new_session`` gives every command its own process group,
    allowing a timeout to terminate the shell and all of its children.
    """
    max_output_bytes = 10 * 1024 * 1024

    def _read_output(stream: BinaryIO) -> str:
        stream.seek(0)
        data = stream.read(max_output_bytes + 1)
        truncated = len(data) > max_output_bytes
        text = data[:max_output_bytes].decode("utf-8", errors="replace")
        if truncated:
            text += "\n[output truncated by kubengine]"
        return text.rstrip()

    def _signal_group(process: subprocess.Popen[bytes], sig: int) -> None:
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        except OSError as exc:
            log.warning(
                "Failed to signal command process group %s: %s", process.pid, exc
            )

    def _group_exists(process: subprocess.Popen[bytes]) -> bool:
        try:
            os.killpg(process.pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    try:
        with (
            tempfile.TemporaryFile() as stdout_file,
            tempfile.TemporaryFile() as stderr_file,
        ):
            process = subprocess.Popen(
                cmd,
                shell=True,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )
            log.debug("Executing command in process group %s: %s", process.pid, cmd)
            timed_out = False
            try:
                return_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                log.error(
                    "Execute command [%s] timeout [%s]s; terminating pgid=%s",
                    cmd,
                    timeout,
                    process.pid,
                )
                _signal_group(process, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                # The shell may exit while a descendant ignores SIGTERM. Check
                # the process group itself, not only the group leader.
                if _group_exists(process):
                    log.warning(
                        "Command process group %s survived SIGTERM; sending SIGKILL",
                        process.pid,
                    )
                    _signal_group(process, signal.SIGKILL)
                if process.poll() is None:
                    process.wait()
                return_code = 124

            stdout = _read_output(stdout_file)
            stderr = _read_output(stderr_file)
            if timed_out:
                timeout_message = f"execute command timeout after {timeout}s"
                stderr = f"{stderr}\n{timeout_message}".strip()

            if log_output:
                for line in stdout.splitlines():
                    log.debug("[STDOUT] %s", line)
                for line in stderr.splitlines():
                    log.debug("[STDERR] %s", line)
            return CommandResult(return_code, stdout, stderr)
    except Exception as exc:
        log.exception("Execute command [%s] got exception", cmd)
        return CommandResult(1, "", f"execute command error: {exc}")


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
