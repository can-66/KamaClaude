from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

from kama_claude.core.config import KamaConfig
from kama_claude.core.transport.socket_client import IpcError, SocketClient

# S2 的 CLI 主线：
# stage/s1 在本进程创建 AgentRunner；stage/s2 改为 SocketClient 连接 daemon，
# 先 event.subscribe，再 agent.run，最后等待同一连接推来的 run.finished。
# StdoutPrinter 这个“事件 → 文字”的职责来自 S1，但接收 Pydantic Event 改成 dict
# 正是事件越过 IPC 边界后的 S2 变化。当前 main 基本保留了这条 S2 客户端链路。


# 把结构化运行事件翻译成适合人阅读的终端输出
class StdoutPrinter:
    # 当前 main 接收从 IPC 反序列化的 dict，并初始化终端打印状态
    def __init__(self) -> None:
        # token 使用 end="" 流式打印；True 表示光标仍停在一行文字末尾。
        self._inline = False
        # monotonic 时间只用来算耗时，不受用户修改系统时钟影响。
        self._run_start: float = 0.0

    # 若当前行有未换行的 token，补一个换行符
    def _ensure_newline(self) -> None:
        if self._inline:
            print()
            self._inline = False

    # 根据网络事件 dict 的 type 字段分发；原始 S1 在同一位置用 isinstance
    async def handle(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")

        if t == "run.started":
            self._run_start = time.monotonic()
            print(f"[run] {event.get('run_id', '')}")

        elif t == "step.started":
            self._ensure_newline()
            print(f"[step {event.get('step')}] planning...")

        elif t == "llm.token":
            print(event.get("token", ""), end="", flush=True)
            self._inline = True

        elif t == "tool.call_started":
            self._ensure_newline()
            params_str = json.dumps(event.get("params", {}), ensure_ascii=False)
            print(f"[tool] {event.get('tool_name', '')} {params_str}")

        elif t == "tool.call_finished":
            print(f"[tool] {event.get('tool_name', '')} ✓  {event.get('elapsed_ms')}ms")

        elif t == "tool.call_failed":
            print(
                f"[tool] {event.get('tool_name', '')} ✗  {event.get('error_message', '')}",
                file=sys.stderr,
            )

        elif t == "step.finished":
            self._ensure_newline()
            print(f"[step {event.get('step')}] done")

        elif t == "run.finished":
            self._ensure_newline()
            elapsed = time.monotonic() - self._run_start
            print(f"[run] {event.get('status', '')}  {event.get('steps')} steps  {elapsed:.1f}s")


# 异步核心：连接 daemon，订阅事件，触发 run，等待 run.finished
async def _run_async(goal: str, config: KamaConfig) -> int:
    # SocketClient 只在 CLI 进程中收发消息；AgentRunner 已搬到 daemon。
    client = SocketClient(config.host, config.port)
    try:
        await client.connect()
    except (ConnectionRefusedError, OSError):
        print(f"error: core not running ({config.host}:{config.port})", file=sys.stderr)
        return 1

    printer = StdoutPrinter()
    # finished 是协程之间的一次性信号，不携带最终结果数据。
    finished = asyncio.Event()
    exit_code = 0

    # 消费 daemon 推送的事件；run.finished 同时充当“本次任务结束”的信号
    async def on_event(event: dict[str, Any]) -> None:
        nonlocal exit_code
        await printer.handle(event)
        if event.get("type") == "run.finished":
            if event.get("status") != "success":
                exit_code = 1
            finished.set()

    client.on_event(on_event)
    # 后台读循环必须先启动，否则 send_command 的响应和服务端事件都无人接收。
    loop_task = asyncio.create_task(client.run_event_loop())

    try:
        # 先订阅再启动 run，避免任务很快时漏掉最前面的 run.started 事件。
        await client.send_command(
            "event.subscribe",
            {
                "topics": ["run.*", "step.*", "tool.*", "llm.token", "llm.usage"],
                "scope": "global",
            },
        )
        # RPC 很快返回 run_id；任务完成要继续等后面的 run.finished 推送。
        await client.send_command("agent.run", {"goal": goal})
    except IpcError as e:
        print(f"error: {e}", file=sys.stderr)
        loop_task.cancel()
        await client.close()
        return 1

    # 原始 S2 daemon 同时只跑一个 run，所以 global scope 不会串到别的任务。
    # 当前 main 已允许更多运行形态；这种历史假设会在 S2 指南的代码边界中说明。
    await finished.wait()

    loop_task.cancel()
    try:
        await loop_task
    except asyncio.CancelledError:
        pass

    await client.close()
    return exit_code


# 执行 kama run --goal "..." 命令
def cmd_run(goal: str, config: KamaConfig) -> None:
    # 同步 CLI 用 asyncio.run 托管异步客户端；Ctrl+C 使用约定俗成的退出码 130。
    try:
        exit_code = asyncio.run(_run_async(goal, config))
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(exit_code)
