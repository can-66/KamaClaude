from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from kama_claude.core.transport.socket_client import IpcError, SocketClient

# 本文件用临时 asyncio server 充当最小 daemon，只验证 S2 SocketClient 的混合流分发。
# 它不验证 CoreApp、AgentRunner 或事件落盘，这些属于集成测试职责。

# 在系统分配的随机端口启动 mock server，并把实际端口返回给客户端
async def _start_mock_server(
    handler: Any,
) -> tuple[asyncio.Server, int]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port: int = server.sockets[0].getsockname()[1]
    return server, port


# 功能：验证 send_command 向 mock server 发送 JSON-RPC 请求并正确解析响应 result
# 设计：随机端口 mock server 返回同 id 响应，并发读循环负责 resolve send_command 创建的 Future
async def test_send_command_returns_result() -> None:
    # 读取一条请求后返回同 id 的成功响应
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        req = json.loads(line)
        resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"pong": True}}
        writer.write(json.dumps(resp).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _start_mock_server(handle)
    async with server:
        client = SocketClient("127.0.0.1", port)
        await client.connect()
        loop_task = asyncio.create_task(client.run_event_loop())

        result = await asyncio.wait_for(
            client.send_command("core.ping", {"client": "test"}),
            timeout=2.0,
        )
        assert result == {"pong": True}

        await loop_task
        await client.close()


# 功能：验证 server 返回 JSON-RPC error 时 send_command 抛出 IpcError 并携带正确错误码
# 设计：mock server 返回 error 对象（code=-32601），断言异常类型和 code 属性，确认客户端的错误路径处理
async def test_send_command_raises_ipc_error() -> None:
    # 返回同 id 的 JSON-RPC error，触发客户端 Future 的异常路径
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        req = json.loads(line)
        resp = {
            "jsonrpc": "2.0",
            "id": req["id"],
            "error": {"code": -32601, "message": "Method not found"},
        }
        writer.write(json.dumps(resp).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _start_mock_server(handle)
    async with server:
        client = SocketClient("127.0.0.1", port)
        await client.connect()
        loop_task = asyncio.create_task(client.run_event_loop())

        with pytest.raises(IpcError) as exc_info:
            await asyncio.wait_for(
                client.send_command("core.nope", {}),
                timeout=2.0,
            )
        assert exc_info.value.code == -32601

        await loop_task
        await client.close()


# 功能：验证 server 推送 kind=event 的消息时，on_event 注册的 handler 能收到 event 字典
# 设计：mock server 在同一连接依次写响应和推送，用 Event 等回调而非 sleep，再断言 event.type
async def test_event_push_routed_to_handler() -> None:
    received_events: list[dict[str, Any]] = []
    push_done = asyncio.Event()

    # 先回复订阅命令，再在同一连接主动推送 kind=event
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        req = json.loads(line)
        resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"subscription_id": "sub-1"}}
        writer.write(json.dumps(resp).encode() + b"\n")
        push = {"kind": "event", "event": {"type": "run.started", "run_id": "r1"}}
        writer.write(json.dumps(push).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _start_mock_server(handle)
    async with server:
        client = SocketClient("127.0.0.1", port)

        # 收集推送内容并发信号，避免靠固定 sleep 猜测到达时间
        async def collect(event_data: dict[str, Any]) -> None:
            received_events.append(event_data)
            push_done.set()

        client.on_event(collect)
        await client.connect()
        loop_task = asyncio.create_task(client.run_event_loop())

        await asyncio.wait_for(
            client.send_command("event.subscribe", {"topics": ["run.*"]}),
            timeout=2.0,
        )
        await asyncio.wait_for(push_done.wait(), timeout=2.0)

        assert len(received_events) == 1
        assert received_events[0]["type"] == "run.started"

        await loop_task
        await client.close()


# 功能：验证 server 关闭连接后 run_event_loop 正常退出（不挂起）
# 设计：mock server 建连后立即关闭，让 readline 得到 EOF，并用 wait_for 防回归时测试永久挂起
async def test_run_event_loop_exits_on_server_close() -> None:
    # 接受连接后立即关闭，使客户端 readline() 得到 EOF
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()
        await writer.wait_closed()

    server, port = await _start_mock_server(handle)
    async with server:
        client = SocketClient("127.0.0.1", port)
        await client.connect()
        await asyncio.wait_for(client.run_event_loop(), timeout=2.0)
        await client.close()
