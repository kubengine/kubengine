import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from core.ssh import AsyncSSHClient


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    async def run(self, command: str, check: bool = False) -> SimpleNamespace:
        return SimpleNamespace(stdout=command, stderr="", exit_status=0)

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
