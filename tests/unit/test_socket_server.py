from __future__ import annotations

import asyncio
import socket

from kama_claude.core.transport.socket_server import SocketServer

# 本文件由 S2 新增，专门验证 SocketServer 与 broadcaster 的连接生命周期接缝。
# 它不是 S0 原始测试；S0 的请求解析和错误路径主要由 test_ping_roundtrip.py 覆盖。

# 返回一个当前空闲端口，供本文件短暂启动 SocketServer
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# 功能：验证客户端断开后 SocketServer 调用 broadcaster.unsubscribe(writer) 清理订阅
# 设计：最小 MockBroadcaster 在 unsubscribe 时置位 Event，按信号等待清理而不靠 sleep 猜时序
async def test_broadcaster_unsubscribe_called_on_disconnect() -> None:
    unsubscribed = asyncio.Event()

    # 只实现 SocketServer 清理阶段会调用的最小接口
    class MockBroadcaster:
        # 用 Event 记录清理动作，避免依赖不稳定的 sleep 时间
        def unsubscribe(self, writer: object) -> None:
            unsubscribed.set()

    port = _free_port()
    server = SocketServer("127.0.0.1", port, broadcaster=MockBroadcaster())  # type: ignore[arg-type]
    await server.start()

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        await writer.wait_closed()

        await asyncio.wait_for(unsubscribed.wait(), timeout=2.0)
    finally:
        await server.stop()


# 功能：验证不传入 broadcaster 时 SocketServer 仍可正常启动和停止（backward-compatible 默认值）
# 设计：随机空闲端口上用默认 broadcaster=None 完成 start/stop，回归验证 S0 调用方式仍兼容
async def test_no_broadcaster_server_starts_and_stops() -> None:
    port = _free_port()
    server = SocketServer("127.0.0.1", port)
    await server.start()
    await server.stop()
