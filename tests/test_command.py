import time
import logging
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


def test_command_lifecycle_records_success(caplog) -> None:
    with caplog.at_level(logging.DEBUG, logger="core.command"):
        result = execute_command("true", log_output=False)

    records = [record for record in caplog.records if hasattr(record, "event")]
    assert result.is_success()
    assert [record.event for record in records] == [  # type: ignore[attr-defined]
        "command_start",
        "command_end",
    ]
    assert records[-1].status == "success"  # type: ignore[attr-defined]
    assert records[-1].exit_code == 0  # type: ignore[attr-defined]
    assert records[-1].duration_ms >= 0  # type: ignore[attr-defined]


def test_command_lifecycle_records_failure_at_error_level(caplog) -> None:
    with caplog.at_level(logging.DEBUG, logger="core.command"):
        result = execute_command("exit 7", log_output=False)

    end_record = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "command_end"
    )
    assert result.return_code == 7
    assert end_record.levelno == logging.ERROR
    assert end_record.status == "failed"  # type: ignore[attr-defined]
    assert end_record.exit_code == 7  # type: ignore[attr-defined]
