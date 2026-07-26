from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from kama_claude.core.bus.envelope import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    HandlerError,
    JsonRpcError,
    JsonRpcRequest,
    JsonRpcSuccess,
    make_error,
)
from kama_claude.core.trace.record import TraceRecord
from kama_claude.core.trace.writer import TraceWriter
from kama_claude.core.transport.ipc_broadcaster import IpcEventBroadcaster

logger = logging.getLogger(__name__)

# 这个文件是 S0 的“服务端传输半边”。
# 它只负责：收一行 → 校验 JSON-RPC 外壳 → 按 method 找 handler → 回一行。
# 它不知道 core.ping 的业务细节，因此以后新增命令时不必重写 TCP 读写代码。

# handler 接收 params 字典，并异步返回普通对象或 Pydantic 模型
type CommandHandler = Callable[[dict[str, Any]], Awaitable[Any]]

# S2 以后：保存当前连接，供事件订阅 handler 找到要推送的客户端；S0 可以跳过
_writer_var: ContextVar[asyncio.StreamWriter] = ContextVar("_writer_var")


# 生成用于 trace 的 UTC 时间戳
def _now() -> str:
    return datetime.now(UTC).isoformat()


# 返回当前 handler 调用所属连接的 StreamWriter
def get_connection_writer() -> asyncio.StreamWriter:
    return _writer_var.get()

# 当前 main 为 MCP 大结果放宽到 64 MB；原始 S0 是 1 MB，原理都是限制单帧大小。
_MAX_LINE_BYTES = 64 * 1024 * 1024


class SocketServer:
    # 保存监听地址及可选的后续阶段组件；S0 只需要 host、port 和 handlers
    def __init__(
        self,
        host: str,
        port: int,
        broadcaster: IpcEventBroadcaster | None = None,
        trace: TraceWriter | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._handlers: dict[str, CommandHandler] = {}
        self._server: asyncio.AbstractServer | None = None
        self._broadcaster = broadcaster
        self._trace = trace
        self._active_writers: set[asyncio.StreamWriter] = set()

    # 注册“方法名 → 处理函数”，例如 "core.ping" → CoreApp._ping_handler
    def register(self, method: str, handler: CommandHandler) -> None:
        self._handlers[method] = handler

    # 启动 TCP 服务器；绑定前先探测同一地址，避免误启动两个 daemon
    async def start(self) -> str:
        try:
            # 若连接成功，说明该 host:port 已经有服务监听，当前进程不再抢占。
            _r, w = await asyncio.open_connection(self._host, self._port)
            w.close()
            await w.wait_closed()
            raise SystemExit(f"core already running at {self._host}:{self._port}")
        except (ConnectionRefusedError, OSError):
            # 连接失败正是预期结果：端口目前无人监听，可以尝试绑定。
            pass

        # start_server 每接收一个客户端，就调用一次 _handle_connection。
        # limit 限制 StreamReader 缓冲的一帧大小，避免异常输入无限占用内存。
        self._server = await asyncio.start_server(
            self._handle_connection,
            host=self._host,
            port=self._port,
            limit=_MAX_LINE_BYTES,
        )
        return f"{self._host}:{self._port}"

    # 关闭服务器：先断开所有活跃连接，再等待服务器完全关闭（最多 2 秒）
    async def stop(self) -> None:
        if self._server is None:
            return
        for writer in list(self._active_writers):
            try:
                writer.close()
            except Exception:
                pass
        self._server.close()
        try:
            await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
        except (TimeoutError, asyncio.CancelledError):
            pass

    # 处理一个客户端连接；连接存在期间持续读消息，断开时统一清理资源
    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername", "<unknown>")
        logger.debug("client connected: %s", peer)
        self._active_writers.add(writer)
        try:
            # 正常情况下会一直停在读循环，直到客户端关闭连接。
            await self._read_loop(reader, writer)
        finally:
            # finally 保证正常断开和异常断开都不会遗留 writer。
            self._active_writers.discard(writer)
            if self._broadcaster is not None:
                self._broadcaster.unsubscribe(writer)
            try:
                writer.close()
            except Exception:
                pass
            logger.debug("client disconnected: %s", peer)

    # 持续读取 NDJSON：readline() 每次恰好取一条以换行结尾的消息
    async def _read_loop(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        while True:
            try:
                # TCP 只提供连续字节流；NDJSON 的换行符帮助我们恢复消息边界。
                line = await reader.readline()
            except asyncio.LimitOverrunError:
                await self._send(writer, make_error(None, INVALID_REQUEST, "Request too large"))
                return

            if not line:
                # 空 bytes 表示对端已经关闭连接，而不是收到了一条空 JSON。
                return

            # 当前 main 为后续长任务并发创建 task；原始 S0 是直接 await 顺序处理。
            asyncio.create_task(self._handle_line(line, writer))

    # 依次完成 JSON 解析、外壳校验、方法路由、handler 调用和成功/错误响应
    async def _handle_line(self, line: bytes, writer: asyncio.StreamWriter) -> None:
        # 第 1 关：字节必须能解析成合法 JSON，否则连请求字段都无从检查。
        try:
            raw: Any = json.loads(line)
        except json.JSONDecodeError as e:
            await self._send(writer, make_error(None, PARSE_ERROR, f"Parse error: {e}"))
            return

        # 第 2 关：JSON 必须符合 JsonRpcRequest，例如版本为 2.0 且含 id/method。
        try:
            req = JsonRpcRequest.model_validate(raw)
        except ValidationError as e:
            await self._send(writer, make_error(None, INVALID_REQUEST, "Invalid Request", str(e)))
            return

        if self._trace is not None:
            client_id = str(writer.get_extra_info("peername", "<unknown>"))
            self._trace.emit(
                TraceRecord(
                    ts=_now(),
                    direction="CLIENT→CORE",
                    layer="ipc",
                    kind="command",
                    client_id=client_id,
                    data={"method": req.method, "id": req.id, "params": req.params},
                )
            )

        # 第 3 关：method 必须已经通过 register() 注册。
        handler = self._handlers.get(req.method)
        if handler is None:
            await self._send(
                writer,
                make_error(req.id, METHOD_NOT_FOUND, f"Method not found: {req.method}"),
            )
            return

        # 后续事件订阅需要知道当前连接；它不是 S0 ping 的必要条件。
        _writer_var.set(writer)
        try:
            # 传输层只把 params 交给 handler，业务参数应由具体 handler 再校验。
            result = await handler(req.params)
        except HandlerError as e:
            await self._send(writer, make_error(req.id, e.code, str(e), e.data))
            return
        except ValidationError as e:
            await self._send(
                writer,
                make_error(req.id, INVALID_REQUEST, "Invalid params", str(e)),
            )
            return
        except Exception as e:
            logger.exception("handler %s raised: %s", req.method, e)
            await self._send(writer, make_error(req.id, INTERNAL_ERROR, "Internal error"))
            return

        # Pydantic 模型先转成可 JSON 序列化的 dict，普通返回值则直接使用。
        result_data: Any = result.model_dump() if isinstance(result, BaseModel) else result
        try:
            await self._send(writer, JsonRpcSuccess(id=req.id, result=result_data))
        except (ConnectionResetError, BrokenPipeError, OSError):
            logger.debug("client disconnected before response for %s", req.method)

    # 把响应序列化成一行 NDJSON；write() 入缓冲，drain() 等待底层可继续发送
    async def _send(self, writer: asyncio.StreamWriter, msg: BaseModel) -> None:
        # 与客户端完全对称：JSON 文本编码为 bytes，并以 b"\n" 标记帧结束。
        writer.write(msg.model_dump_json().encode() + b"\n")
        await writer.drain()
        if self._trace is not None:
            kind = "error" if isinstance(msg, JsonRpcError) else "response"
            client_id = str(writer.get_extra_info("peername", "<unknown>"))
            self._trace.emit(
                TraceRecord(
                    ts=_now(),
                    direction="CORE→CLIENT",
                    layer="ipc",
                    kind=kind,
                    client_id=client_id,
                    data=msg.model_dump(),
                )
            )
