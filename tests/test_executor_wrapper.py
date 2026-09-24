from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from pyinfra.api.operation import OperationMeta
from pyinfra.connectors.util import CommandOutput, OutputLine

from infra.executor_wrapper import (
    HostExecutionResult,
    HostOperationResult,
    InfraExecutionResult,
    InfraFileExecutor,
)


class FakeOperationMeta:
    def __init__(
        self,
        *,
        success: bool = True,
        changed: bool = False,
        complete: bool = True,
        stdout: list[str] | None = None,
        stderr: list[str] | None = None,
    ) -> None:
        self._success = success
        self._changed = changed
        self._complete = complete
        self.stdout_lines = stdout or []
        self.stderr_lines = stderr or []

    def is_complete(self) -> bool:
        return self._complete

    def did_succeed(self) -> bool:
        return self._success

    def did_change(self) -> bool:
        return self._changed


@dataclass(frozen=True)
class FakeHost:
    name: str

    def __str__(self) -> str:
        return self.name


class FakeState:
    def __init__(
        self, hosts: list[FakeHost], operations: list[tuple[str, str]]
    ) -> None:
        self.activated_hosts = set(hosts)
        self._order = [op_hash for op_hash, _ in operations]
        self._names = {
            op_hash: SimpleNamespace(names={name}) for op_hash, name in operations
        }
        self._data: dict[tuple[FakeHost, str], SimpleNamespace] = {}

    def add_result(self, host: FakeHost, op_hash: str, meta: FakeOperationMeta) -> None:
        self._data[(host, op_hash)] = SimpleNamespace(operation_meta=meta)

    def get_op_order(self) -> list[str]:
        return self._order

    def get_op_meta(self, op_hash: str) -> SimpleNamespace:
        return self._names[op_hash]

    def get_op_data_for_host(self, host: FakeHost, op_hash: str) -> SimpleNamespace:
        return self._data[(host, op_hash)]


def make_result(*hosts: FakeHost, connected: bool = True) -> InfraExecutionResult:
    return InfraExecutionResult(
        total_hosts=len(hosts),
        host_results={
            str(host): HostExecutionResult(hostname=str(host), connected=connected)
            for host in hosts
        },
    )


def test_collects_success_change_and_skipped_operations() -> None:
    host = FakeHost("node-1")
    state = FakeState(
        hosts=[host], operations=[("one", "Install"), ("two", "Worker only")]
    )
    state.add_result(
        host,
        "one",
        FakeOperationMeta(success=True, changed=True, stdout=["installed"]),
    )
    executor = InfraFileExecutor()
    executor._state = state  # type: ignore[assignment]
    result = make_result(host)

    executor._collect_operation_results(result)
    executor._calculate_summary_metrics(result)

    host_result = result.host_results["node-1"]
    assert host_result.total_operations == 2
    assert host_result.successful_operations == 1
    assert host_result.changed_operations == 1
    assert host_result.skipped_operations == 1
    assert host_result.operations["Install"].output == ["installed"]
    assert host_result.operations["Worker only"].dict()["status"] == "skipped"
    assert host_result.dict()["summary"] == {
        "success_rate": 100.0,
        "failure_rate": 0.0,
        "change_rate": 100.0,
        "skip_rate": 50.0,
    }
    assert result.success is True


def test_failed_operation_makes_host_and_deployment_fail() -> None:
    host = FakeHost("node-1")
    state = FakeState(hosts=[host], operations=[("one", "Restart service")])
    state.add_result(
        host,
        "one",
        FakeOperationMeta(success=False, stderr=["service failed"]),
    )
    executor = InfraFileExecutor()
    executor._state = state  # type: ignore[assignment]
    result = make_result(host)

    executor._collect_operation_results(result)
    executor._calculate_summary_metrics(result)

    operation = result.host_results["node-1"].operations["Restart service"]
    assert operation.success is False
    assert operation.error == "service failed"
    assert result.failed_hosts == 1
    assert result.success is False


def test_incomplete_result_is_not_reported_as_success() -> None:
    host = FakeHost("node-1")
    state = FakeState(hosts=[host], operations=[("one", "Install")])
    state.add_result(host, "one", FakeOperationMeta(complete=False))
    executor = InfraFileExecutor()
    executor._state = state  # type: ignore[assignment]
    result = make_result(host)

    executor._collect_operation_results(result)
    executor._calculate_summary_metrics(result)

    assert "Failed to collect operation results" in (
        result.host_results["node-1"].error or ""
    )
    assert result.success is False


def test_empty_deployment_can_succeed() -> None:
    host = FakeHost("node-1")
    executor = InfraFileExecutor()
    executor._state = FakeState(hosts=[host], operations=[])  # type: ignore[assignment]
    result = make_result(host)

    executor._collect_operation_results(result)
    executor._calculate_summary_metrics(result)

    assert result.host_results["node-1"].total_operations == 0
    assert result.success is True


def test_connection_failure_makes_deployment_fail() -> None:
    host = FakeHost("node-1")
    executor = InfraFileExecutor()
    result = make_result(host, connected=False)

    executor._calculate_summary_metrics(result)

    assert result.connected_hosts == 0
    assert result.failed_hosts == 1
    assert result.success is False


def test_unsupported_pyinfra_state_api_is_explicit_failure() -> None:
    executor = InfraFileExecutor()
    executor._state = SimpleNamespace(activated_hosts=set())  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="Unsupported PyInfra state API"):
        executor._collect_operation_results(InfraExecutionResult())


def test_merge_preserves_skipped_operation_count() -> None:
    executor = InfraFileExecutor()
    main_result = InfraExecutionResult(
        total_hosts=1,
        host_results={"node-1": HostExecutionResult(hostname="node-1")},
    )
    file_result = InfraExecutionResult(
        total_hosts=1,
        host_results={
            "node-1": HostExecutionResult(
                hostname="node-1",
                connected=True,
                operations={
                    "Worker only": HostOperationResult(
                        operation_name="Worker only",
                        success=True,
                        skipped=True,
                    )
                },
                total_operations=1,
                skipped_operations=1,
            )
        },
    )

    executor._merge_execution_results(main_result, file_result, "install")

    merged = main_result.host_results["node-1"]
    assert merged.total_operations == 1
    assert merged.successful_operations == 0
    assert merged.skipped_operations == 1


def test_collects_real_pyinfra_36_operation_metadata() -> None:
    host = FakeHost("node-1")
    state = FakeState(hosts=[host], operations=[("one", "Real metadata")])
    operation_meta = OperationMeta("one", None)
    operation_meta.set_complete(
        True,
        [object()],
        CommandOutput([OutputLine("stdout", "completed")]),
    )
    state.add_result(host, "one", operation_meta)  # type: ignore[arg-type]
    executor = InfraFileExecutor()
    executor._state = state  # type: ignore[assignment]
    result = make_result(host)

    executor._collect_operation_results(result)
    executor._calculate_summary_metrics(result)

    assert result.success is True
    operation = result.host_results["node-1"].operations["Real metadata"]
    assert operation.success is True
    assert operation.changed is True
    assert operation.output == ["completed"]
