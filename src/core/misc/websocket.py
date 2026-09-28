"""Best-effort notifications on each WebSocket's owning event loop."""

import asyncio
import threading
from concurrent.futures import Future
from typing import Any, Awaitable, Callable

from fastapi import WebSocket

from core.logger import get_logger

logger = get_logger(__name__)
SessionValidator = Callable[[], Awaitable[bool]]


class ConnectionManager:
    """Track local-process connections without holding a pool lock during IO."""

    def __init__(self, send_timeout: float = 5.0):
        self._connections: dict[
            WebSocket, tuple[asyncio.AbstractEventLoop, asyncio.Lock, SessionValidator | None]
        ] = {}
        self._lock = threading.Lock()
        self.send_timeout = send_timeout

    @property
    def active_connections(self) -> list[WebSocket]:
        with self._lock:
            return list(self._connections)

    async def connect(self, websocket: WebSocket, validator: SessionValidator | None = None):
        with self._lock:
            self._connections[websocket] = (asyncio.get_running_loop(), asyncio.Lock(), validator)

    def _remove(self, websocket: WebSocket) -> None:
        with self._lock:
            self._connections.pop(websocket, None)

    async def disconnect(self, websocket: WebSocket):
        self._remove(websocket)

    def _snapshot(self):
        with self._lock:
            return list(self._connections.items())

    async def validate(self, websocket: WebSocket) -> bool:
        """Check the current session on the connection's event loop; fail closed."""
        with self._lock:
            connection = self._connections.get(websocket)
        if connection is None:
            return False
        validator = connection[2]
        if validator is None:
            return True
        try:
            valid = await asyncio.wait_for(validator(), timeout=self.send_timeout)
        except Exception:
            valid = False
        if valid:
            with self._lock:
                return self._connections.get(websocket) is connection

        # Remove before awaiting close so no pending snapshot can send more data.
        self._remove(websocket)
        logger.info("WebSocket session expired or invalid; connection removed")
        try:
            await asyncio.wait_for(
                websocket.close(code=1008, reason="Session expired or invalid"),
                timeout=self.send_timeout,
            )
        except Exception:
            pass
        return False

    async def _send(self, websocket: WebSocket, send_lock: asyncio.Lock, message: dict[str, Any]):
        async def send():
            async with send_lock:
                if not await self.validate(websocket):
                    return
                await websocket.send_json(message)

        try:
            await asyncio.wait_for(send(), timeout=self.send_timeout)
        except Exception:
            self._remove(websocket)
            logger.warning("WebSocket notification failed; connection removed", exc_info=True)

    def _schedule(self, websocket, loop, send_lock, message) -> Future | None:
        if loop.is_closed() or not loop.is_running():
            self._remove(websocket)
            return None
        coroutine = self._send(websocket, send_lock, message)
        try:
            return asyncio.run_coroutine_threadsafe(coroutine, loop)
        except RuntimeError:
            coroutine.close()
            self._remove(websocket)
            logger.warning("WebSocket event loop stopped before notification")
            return None

    async def send_message(self, websocket: WebSocket, message: dict[str, Any]) -> None:
        """Send a direct reply with the same session check as a broadcast."""
        with self._lock:
            connection = self._connections.get(websocket)
        if connection is None:
            return
        loop, send_lock, _ = connection
        if loop is asyncio.get_running_loop():
            await self._send(websocket, send_lock, message)
        else:
            future = self._schedule(websocket, loop, send_lock, message)
            if future is not None:
                await asyncio.wrap_future(future)

    def broadcast_from_thread(self, message: dict[str, Any]) -> None:
        """Schedule notifications without blocking a synchronous task's outcome.

        These connections belong to this process only. Cross-process delivery
        needs a shared event transport and is deliberately not implied here.
        """
        for websocket, (loop, send_lock, _) in self._snapshot():
            self._schedule(websocket, loop, send_lock, message)

    async def broadcast(self, message: dict[str, Any]):
        current_loop = asyncio.get_running_loop()
        pending = []
        for websocket, (loop, send_lock, _) in self._snapshot():
            if loop is current_loop:
                pending.append(self._send(websocket, send_lock, message))
            else:
                future = self._schedule(websocket, loop, send_lock, message)
                if future is not None:
                    pending.append(asyncio.wrap_future(future))
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


connection_manager = ConnectionManager()
