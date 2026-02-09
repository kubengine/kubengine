
import asyncio
from typing import Any
from fastapi import WebSocket


class ConnectionManager:
    '''
    description: WebSocket连接管理类（核心：维护连接池+广播）
    '''

    def __init__(self):
        # 存储已连接的WebSocket对象
        self.active_connections: list[WebSocket] = []
        # 异步锁：保证并发操作连接池的安全
        self.lock = asyncio.Lock()

    # 添加新连接
    async def connect(self, websocket: WebSocket):
        async with self.lock:
            self.active_connections.append(websocket)

    # 移除断开的连接
    async def disconnect(self, websocket: WebSocket):
        async with self.lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)

    # 广播消息给所有已连接客户端（泛洪推送）
    async def broadcast(self, message: dict[str, Any]):
        async with self.lock:
            # 遍历所有连接，逐个发送消息
            for connection in self.active_connections:
                try:
                    await connection.send_json(message)
                except Exception as e:
                    # 捕获发送失败（如客户端已断开），清理无效连接
                    print(f"发送消息失败：{e}，清理无效连接")
                    await self.disconnect(connection)


# 初始化连接管理器
connection_manager = ConnectionManager()
