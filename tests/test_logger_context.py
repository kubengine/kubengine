import asyncio
import io
import logging

from core.config.application import Application
from core.logger import (
    GlobalLoggerManager,
    LogContextFilter,
    ReadableFormatter,
    bind_log_context,
    get_log_context,
    log_lifecycle_event,
    log_context_environment,
    with_log_context,
    with_new_log_context,
)


def make_record(message: str = "test") -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_filter_injects_context_and_restores_parent_scope() -> None:
    context_filter = LogContextFilter()

    with bind_log_context(request_id="req-1"):
        with bind_log_context(task_id=42):
            record = make_record()
            assert context_filter.filter(record) is True
            assert record.request_id == "req-1"  # type: ignore[attr-defined]
            assert record.task_id == "42"  # type: ignore[attr-defined]
            assert record.cluster_id == "-"  # type: ignore[attr-defined]
            assert record.context_suffix == (  # type: ignore[attr-defined]
                " | request_id=req-1 | task_id=42"
            )

        assert get_log_context() == {"request_id": "req-1"}

    assert get_log_context() == {}


def test_context_decorator_reads_function_arguments() -> None:
    @with_log_context(task_id="job_id", cluster_id="cluster")
    def execute(job_id: int, cluster: int) -> dict[str, str]:
        return get_log_context()

    assert execute(7, cluster=9) == {"task_id": "7", "cluster_id": "9"}
    assert get_log_context() == {}


def test_async_contexts_are_isolated() -> None:
    @with_log_context(task_id="task_id")
    async def execute(task_id: str) -> tuple[str, str]:
        await asyncio.sleep(0)
        return task_id, get_log_context()["task_id"]

    async def run() -> list[tuple[str, str]]:
        return list(await asyncio.gather(execute("one"), execute("two")))

    assert asyncio.run(run()) == [("one", "one"), ("two", "two")]
    assert get_log_context() == {}


def test_new_context_id_and_child_process_environment() -> None:
    @with_new_log_context("deployment_id", prefix="dep-")
    def execute() -> tuple[str, dict[str, str]]:
        deployment_id = get_log_context()["deployment_id"]
        return deployment_id, log_context_environment()

    deployment_id, environment = execute()

    assert deployment_id.startswith("dep-")
    assert len(deployment_id) == 36
    assert environment == {"KUBENGINE_LOG_DEPLOYMENT_ID": deployment_id}
    assert get_log_context() == {}


def test_child_environment_removes_stale_context() -> None:
    environment = log_context_environment(
        {
            "PATH": "/bin",
            "KUBENGINE_LOG_REQUEST_ID": "stale-request",
            "KUBENGINE_LOG_TASK_ID": "stale-task",
        }
    )

    assert environment == {"PATH": "/bin"}


def test_async_new_context_id_is_scoped() -> None:
    @with_new_log_context("request_id", prefix="async-")
    async def execute() -> str:
        await asyncio.sleep(0)
        return get_log_context()["request_id"]

    request_id = asyncio.run(execute())

    assert request_id.startswith("async-")
    assert get_log_context() == {}


def test_lifecycle_event_exposes_event_fields_and_context(caplog) -> None:
    logger = logging.getLogger("test.lifecycle")

    with caplog.at_level(logging.INFO, logger="test.lifecycle"):
        with bind_log_context(deployment_id="dep-1", component="containerd"):
            log_lifecycle_event(
                logger,
                "component_end",
                status="success",
                duration_ms=12,
            )

    record = caplog.records[-1]
    assert record.event == "component_end"  # type: ignore[attr-defined]
    assert record.deployment_id == "dep-1"  # type: ignore[attr-defined]
    assert record.component == "containerd"  # type: ignore[attr-defined]
    assert record.status == "success"  # type: ignore[attr-defined]
    assert record.duration_ms == 12  # type: ignore[attr-defined]
    assert record.funcName == "test_lifecycle_event_exposes_event_fields_and_context"
    assert record.getMessage() == (
        "event=component_end status=success duration_ms=12"
    )


def test_lifecycle_event_truncates_only_rendered_long_field(caplog) -> None:
    logger = logging.getLogger("test.lifecycle.long")
    command = "x" * 620

    with caplog.at_level(logging.INFO, logger=logger.name):
        log_lifecycle_event(logger, "command_end", command=command)

    record = caplog.records[-1]
    assert record.command == command  # type: ignore[attr-defined]
    assert "x" * 500 in record.getMessage()
    assert "…<truncated_chars=120>" in record.getMessage()
    assert len(record.getMessage()) < len(command)


def test_readable_formatter_removes_ansi_control_codes() -> None:
    formatter = ReadableFormatter("%(message)s")
    record = make_record("\x1b[31mfailed\x1b[0m: package install")

    assert formatter.format(record) == "failed: package install"


def test_readable_formatter_honors_ansi_switch(monkeypatch) -> None:
    monkeypatch.setattr(Application.LOGGER_CONFIG, "STRIP_ANSI", False)
    formatter = ReadableFormatter("%(message)s")
    record = make_record("\x1b[31mfailed\x1b[0m")

    assert formatter.format(record) == "\x1b[31mfailed\x1b[0m"


def test_lifecycle_event_honors_configured_field_length(monkeypatch, caplog) -> None:
    monkeypatch.setattr(Application.LOGGER_CONFIG, "MAX_EVENT_FIELD_LENGTH", 60)
    logger = logging.getLogger("test.lifecycle.configured_length")

    with caplog.at_level(logging.INFO, logger=logger.name):
        log_lifecycle_event(logger, "command_end", command="x" * 75)

    assert "x" * 60 in caplog.records[-1].getMessage()
    assert "…<truncated_chars=15>" in caplog.records[-1].getMessage()


def test_application_exposes_logging_controls() -> None:
    config = Application.LOGGER_CONFIG

    assert config.LEVEL == "INFO"
    assert config.CONSOLE_OUTPUT is True
    assert config.ROTATE_ENABLE is True
    assert config.ROTATE_WHEN == "D"
    assert config.ROTATE_BACKUP_COUNT == 7
    assert config.STRIP_ANSI is True
    assert config.MAX_EVENT_FIELD_LENGTH == 500
    assert config.THIRD_PARTY_LOG_LEVELS["pyinfra"] == "WARNING"


def test_third_party_loggers_use_central_output_channel(monkeypatch) -> None:
    logger_name = "test.noisy.third.party"
    third_party_logger = logging.getLogger(logger_name)
    handler = logging.StreamHandler(io.StringIO())
    third_party_logger.addHandler(handler)
    third_party_logger.setLevel(logging.DEBUG)
    third_party_logger.propagate = False
    monkeypatch.setattr(
        Application.LOGGER_CONFIG,
        "THIRD_PARTY_LOG_LEVELS",
        {logger_name: "WARNING"},
    )

    try:
        GlobalLoggerManager()._setup_third_party_loggers()

        assert third_party_logger.level == logging.WARNING
        assert third_party_logger.handlers == []
        assert third_party_logger.propagate is True
    finally:
        third_party_logger.handlers.clear()
        third_party_logger.setLevel(logging.NOTSET)
        third_party_logger.propagate = True
