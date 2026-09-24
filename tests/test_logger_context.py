import asyncio
import logging

from core.logger import (
    LogContextFilter,
    bind_log_context,
    get_log_context,
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
