import asyncio
import time
from datetime import timedelta

import pytest
from fastapi import WebSocketDisconnect

from core.misc.websocket import ConnectionManager
from core.orm.auth import RevokedToken, revoke_token
from core.orm.engine import engine
from web.api import websocket as routes
from web.utils import auth


class Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.messages = []
        self.closed = None

    async def accept(self):
        pass

    async def receive_text(self):
        value = await self.incoming.get()
        if isinstance(value, Exception):
            raise value
        return value

    async def send_json(self, data):
        self.messages.append(data)

    async def close(self, code, reason):
        self.closed = code
        await self.incoming.put(WebSocketDisconnect(code))


@pytest.fixture
def session(monkeypatch):
    record = {"password_hash": "isolated-password-hash", "ak": "isolated-ak"}
    monkeypatch.setattr(auth, "load_auth_users", lambda: {"admin": record})
    RevokedToken.__table__.create(engine, checkfirst=True)
    token, _ = auth.create_access_token({"sub": "admin"})
    manager = ConnectionManager(send_timeout=0.2)
    monkeypatch.setattr(routes, "connection_manager", manager)
    monkeypatch.setattr(routes, "SESSION_CHECK_INTERVAL", 0.02)
    return record, token, manager


async def connected_socket(token, manager):
    socket = Socket()
    task = asyncio.create_task(
        routes.websocket_endpoint(socket, token=f"Bearer {token}")
    )

    async def wait_for_connection():
        while socket not in manager.active_connections:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_connection(), 1)
    return socket, task


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidation", ["logout", "rotation"])
async def test_invalidated_session_cannot_receive_broadcasts_or_replies(
    session, invalidation
):
    record, token, manager = session
    socket, task = await connected_socket(token, manager)
    try:
        await manager.send_message(socket, {"status": "initial"})
        assert socket.messages == [{"status": "initial"}]
        if invalidation == "logout":
            revoke_token(token, int(time.time()) + 600)
        else:
            record["password_hash"] = "rotated-password-hash"
        await manager.broadcast({"sensitive": "must not be sent"})
        await manager.send_message(socket, {"sensitive": "must not be sent"})
        await asyncio.wait_for(task, 1)
        assert socket.messages == [{"status": "initial"}]
        assert socket.closed == 1008
        assert not manager.active_connections
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_idle_revoked_session_is_closed(session):
    _, token, manager = session
    socket, task = await connected_socket(token, manager)
    try:
        revoke_token(token, int(time.time()) + 600)
        await asyncio.wait_for(task, 1)
        assert socket.closed == 1008
        assert not socket.messages
        assert not manager.active_connections
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_expired_session_never_joins_connection_pool(session):
    _, _, manager = session
    token, _ = auth.create_access_token(
        {"sub": "admin"}, timedelta(seconds=-1)
    )
    socket = Socket()
    await routes.websocket_endpoint(socket, token=f"Bearer {token}")
    assert socket.closed == 1008
    assert not manager.active_connections
