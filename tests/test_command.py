import time
from pathlib import Path

from core.command import execute_command
from core.logger import bind_log_context


def test_timeout_kills_command_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "orphan-survived"
    command = (
        "(trap '' TERM; sleep 1; "
        f"touch '{marker}') & wait"
    )

    result = execute_command(command, timeout=0.1, log_output=False)

    assert result.return_code == 124
    assert "timeout" in result.stderr
    time.sleep(1.2)
    assert not marker.exists()


def test_command_captures_output() -> None:
    result = execute_command("printf 'hello'; printf 'problem' >&2", log_output=False)

    assert result.is_success()
    assert result.stdout == "hello"
    assert result.stderr == "problem"


def test_command_passes_log_context_to_child_process() -> None:
    with bind_log_context(request_id="req-child", task_id=12):
        result = execute_command(
            "printf '%s:%s' \"$KUBENGINE_LOG_REQUEST_ID\" \"$KUBENGINE_LOG_TASK_ID\"",
            log_output=False,
        )

    assert result.is_success()
    assert result.stdout == "req-child:12"
