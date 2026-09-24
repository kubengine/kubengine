import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from core.logger import bind_log_context
from core.ssh import AsyncSSHClient


class FakeConnection:
    def __init__(
        self,
        *,
        exit_status: int = 0,
        stderr: str = "",
        run_delay: float = 0,
    ) -> None:
        self.closed = False
        self.exit_status = exit_status
        self.stderr = stderr
        self.run_delay = run_delay

    def is_closed(self) -> bool:
        return self.closed

    async def run(self, command: str, check: bool = False) -> SimpleNamespace:
        if self.run_delay:
            await asyncio.sleep(self.run_delay)
        return SimpleNamespace(
            stdout=command,
            stderr=self.stderr,
            exit_status=self.exit_status,
        )

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


@pytest.mark.asyncio
async def test_connection_to_one_host_does_not_lock_other_hosts(
    monkeypatch: Any,
) -> None:
    slow_started = asyncio.Event()
    release_slow = asyncio.Event()

    async def fake_connect(host: str, **kwargs: Any) -> FakeConnection:
        if host == "slow":
            slow_started.set()
            await release_slow.wait()
        return FakeConnection()

    monkeypatch.setattr("core.ssh.asyncssh.connect", fake_connect)
    client = AsyncSSHClient(connect_timeout=1)

    slow_task = asyncio.create_task(client.execute_command("slow", "slow-command"))
    await slow_started.wait()
    fast_result = await asyncio.wait_for(
        client.execute_command("fast", "fast-command"), timeout=0.2
    )
    release_slow.set()
    await slow_task

    assert fast_result["exit_status"] == 0
    await client.close_all_connections()


@pytest.mark.asyncio
async def test_ssh_command_lifecycle_records_success_and_connection_reuse(
    monkeypatch: Any,
    caplog: Any,
) -> None:
    connection = FakeConnection()

    async def fake_connect(host: str, **kwargs: Any) -> FakeConnection:
        return connection

    monkeypatch.setattr("core.ssh.asyncssh.connect", fake_connect)
    client = AsyncSSHClient()

    with bind_log_context(operation="validate_node"):
        with caplog.at_level(logging.DEBUG, logger="core.ssh"):
            first = await client.execute_command("node-1", "printf hello")
            second = await client.execute_command("node-1", "printf again")

    events = [getattr(record, "event", None) for record in caplog.records]
    command_records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) in {"ssh_command_start", "ssh_command_end"}
    ]

    assert first["exit_status"] == 0
    assert second["exit_status"] == 0
    assert events.count("ssh_connection_start") == 1
    assert events.count("ssh_connection_end") == 1
    assert events.count("ssh_connection_reused") == 1
    assert len(command_records) == 4
    assert command_records[0].command_id == command_records[1].command_id
    assert command_records[2].command_id == command_records[3].command_id
    assert command_records[0].command_id != command_records[2].command_id
    assert all(record.host == "node-1" for record in command_records)
    assert all(record.operation == "validate_node" for record in command_records)
    assert command_records[1].levelno == logging.INFO
    assert command_records[1].status == "success"
    assert command_records[1].stdout_bytes == len("printf hello")
    await client.close_all_connections()


@pytest.mark.asyncio
async def test_ssh_command_lifecycle_records_nonzero_exit(
    monkeypatch: Any,
    caplog: Any,
) -> None:
    async def fake_connect(host: str, **kwargs: Any) -> FakeConnection:
        return FakeConnection(exit_status=9, stderr="detail\nremote failed")

    monkeypatch.setattr("core.ssh.asyncssh.connect", fake_connect)
    client = AsyncSSHClient()

    with caplog.at_level(logging.DEBUG, logger="core.ssh"):
        result = await client.execute_command("node-2", "false")

    end_record = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "ssh_command_end"
    )
    assert result["error"] is None
    assert result["exit_status"] == 9
    assert end_record.levelno == logging.ERROR
    assert end_record.status == "failed"
    assert end_record.exit_code == 9
    assert end_record.error == "remote failed"
    assert end_record.stderr_bytes == len("detail\nremote failed")


@pytest.mark.asyncio
async def test_ssh_command_lifecycle_marks_timeout(
    monkeypatch: Any,
    caplog: Any,
) -> None:
    connection = FakeConnection(run_delay=1)

    async def fake_connect(host: str, **kwargs: Any) -> FakeConnection:
        return connection

    monkeypatch.setattr("core.ssh.asyncssh.connect", fake_connect)
    client = AsyncSSHClient()

    with caplog.at_level(logging.DEBUG, logger="core.ssh"):
        result = await client.execute_command(
            "node-3", "slow command", operation_timeout=0.01
        )

    end_record = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "ssh_command_end"
    )
    assert result["exit_status"] is None
    assert result["error"] == "SSH command timed out after 0.01s"
    assert connection.closed is True
    assert end_record.status == "timeout"
    assert end_record.timed_out is True
    assert end_record.levelno == logging.ERROR


@pytest.mark.asyncio
async def test_ssh_transfer_lifecycle_records_paths_and_status(
    monkeypatch: Any,
    caplog: Any,
) -> None:
    client = AsyncSSHClient()
    connection = FakeConnection()

    async def fake_get_connection(host: str, **kwargs: Any) -> FakeConnection:
        return connection

    async def fake_transfer(conn: Any) -> None:
        assert conn is connection

    monkeypatch.setattr(client, "_get_connection", fake_get_connection)

    with caplog.at_level(logging.DEBUG, logger="core.ssh"):
        error = await client._execute_transfer(
            "node-4",
            operation="sftp_upload",
            protocol="SFTP",
            direction="upload",
            source_path="/tmp/source",
            target_path="/srv/target",
            timeout=5,
            transfer=fake_transfer,
            connection_options={},
        )

    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) in {"ssh_transfer_start", "ssh_transfer_end"}
    ]
    assert error is None
    assert len(records) == 2
    assert records[0].transfer_id == records[1].transfer_id
    assert records[1].levelno == logging.INFO
    assert records[1].status == "success"
    assert records[1].host == "node-4"
    assert records[1].source_path == "/tmp/source"
    assert records[1].target_path == "/srv/target"
