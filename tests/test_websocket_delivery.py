import asyncio
import threading

from core.misc.websocket import ConnectionManager


class Socket:
    def __init__(self, *, fail=False, stall=False):
        self.fail = fail
        self.stall = stall
        self.messages = []
        self.loops = []
        self.sent = asyncio.Event()

    async def send_json(self, message):
        self.loops.append(asyncio.get_running_loop())
        if self.fail:
            raise RuntimeError("closed")
        if self.stall:
            await asyncio.Event().wait()
        self.messages.append(message)
        self.sent.set()


def test_failed_connection_is_removed_without_deadlock_or_skipping_others():
    async def check():
        manager = ConnectionManager(send_timeout=0.02)
        dead, slow, good = Socket(fail=True), Socket(stall=True), Socket()
        for socket in (dead, slow, good):
            await manager.connect(socket)
        await asyncio.wait_for(manager.broadcast({"status": "updated"}), 1)
        assert manager.active_connections == [good]
        assert good.messages == [{"status": "updated"}]
        await asyncio.wait_for(manager.disconnect(good), 1)

    asyncio.run(check())


def test_thread_notifications_send_on_connection_event_loop():
    async def check():
        manager = ConnectionManager()
        socket = Socket()
        await manager.connect(socket)
        owner = asyncio.get_running_loop()
        thread = threading.Thread(target=manager.broadcast_from_thread, args=({"status": "done"},))
        thread.start()
        await asyncio.wait_for(socket.sent.wait(), 1)
        thread.join(timeout=1)
        assert socket.loops == [owner]
        assert socket.messages == [{"status": "done"}]

    asyncio.run(check())


def test_async_broadcast_from_another_loop_uses_owner_loop():
    async def check():
        manager = ConnectionManager()
        socket = Socket()
        await manager.connect(socket)
        owner = asyncio.get_running_loop()
        await asyncio.to_thread(asyncio.run, manager.broadcast({"status": "done"}))
        assert socket.loops == [owner]

    asyncio.run(check())


def test_stopped_event_loop_is_removed_without_sending():
    manager = ConnectionManager()
    socket = Socket()
    asyncio.run(manager.connect(socket))
    manager.broadcast_from_thread({"status": "done"})
    assert not manager.active_connections
    assert not socket.messages
