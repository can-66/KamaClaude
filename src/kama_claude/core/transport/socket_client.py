from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from kama_claude.core.bus.envelope import JsonRpcRequest

# S2 的客户端传输层：在一条 TCP/NDJSON 连接上同时处理“命令响应”和“事件推送”。
# send_command() 用 Future 等指定 id 的响应，run_event_loop() 则是唯一读者并负责分流。

# 事件回调接收已经反序列化的普通 dict；网络边界之后不再是 Pydantic Event 对象
type EventHandler = Callable[[dict[str, Any]], Awaitable[None]]

# 当前 main 为 S7 MCP 大结果放宽到 64 MB；原始 S2 是 1 MB，协议原理不变
_MAX_LINE_BYTES = 64 * 1024 * 1024


# 把 JSON-RPC error 转成客户端可捕获的 Python 异常
class IpcError(RuntimeError):
    # 保存机器可判断的错误码，同时构造便于终端显示的消息
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code


# 维护到 kama-core 的单条长连接，并把混合消息流路由给正确等待者
class SocketClient:
    # 保存连接状态、待完成请求和事件回调；构造时尚未真正连接网络
    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        # key 是请求 id，value 是 send_command() 正在 await 的“未来结果”。
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._event_handlers: list[EventHandler] = []

    # 建立到 core 守护进程的 TCP 连接
    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(
            self._host, self._port, limit=_MAX_LINE_BYTES
        )

    # 关闭 TCP 连接并等待底层 socket 释放
    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                # 防止异常网络状态让 CLI/TUI 永久卡在关闭阶段。
                await asyncio.wait_for(self._writer.wait_closed(), timeout=1.0)
            except TimeoutError:
                pass

    # 注册服务器推送事件的回调，可多次调用以添加多个 handler
    def on_event(self, handler: EventHandler) -> None:
        # _dispatch 会按注册顺序逐个 await；慢 handler 也会暂缓后续网络读取。
        self._event_handlers.append(handler)

    # 发送 JSON-RPC 命令并等待响应，成功返回 result dict，失败抛出 IpcError
    async def send_command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._writer is None:
            raise RuntimeError("not connected — call connect() first")
        req_id = str(uuid.uuid4())
        request = JsonRpcRequest(id=req_id, method=method, params=params)
        # 先登记 Future 再发送，避免极快响应到达时还找不到对应等待者。
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        self._writer.write(request.model_dump_json().encode() + b"\n")
        await self._writer.drain()
        # 这里能被唤醒的前提是 run_event_loop() 已在另一个 task 中持续读消息。
        return await fut

    # 持续读取服务器消息，分发 RPC 响应到 pending future 或事件到 event handler
    async def run_event_loop(self) -> None:
        if self._reader is None:
            raise RuntimeError("not connected — call connect() first")
        try:
            while True:
                try:
                    line = await self._reader.readline()
                except (ConnectionResetError, OSError):
                    break
                except (ValueError, asyncio.LimitOverrunError):
                    # 当前 main 的后续健壮性处理；原始 S2 没有这一分支。
                    continue
                if not line:
                    break
                await self._dispatch(line)
        finally:
            # 连接结束后取消所有无望收到响应的请求，不能让调用方永远 await。
            for fut in self._pending.values():
                if not fut.done():
                    fut.cancel()
            self._pending.clear()

    # 解析单行消息并路由到 pending future（RPC 响应）或 event handler（服务器推送）
    async def _dispatch(self, line: bytes) -> None:
        try:
            msg: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            # 客户端无法给无 id 的坏推送回错误响应，只能忽略该行并继续读。
            return

        if "jsonrpc" in msg:
            # JSON-RPC 响应：用 id 找到 send_command() 创建的 Future。
            req_id: str | None = msg.get("id")
            if req_id and req_id in self._pending:
                fut = self._pending.pop(req_id)
                if not fut.done():
                    if "error" in msg:
                        err = msg["error"]
                        fut.set_exception(
                            IpcError(err.get("code", -1), err.get("message", "unknown"))
                        )
                    else:
                        fut.set_result(msg.get("result") or {})
        elif msg.get("kind") == "event":
            # daemon 主动推送：没有请求 id，直接交给所有事件 handler。
            event_data: dict[str, Any] = msg.get("event", {})
            for handler in self._event_handlers:
                await handler(event_data)
