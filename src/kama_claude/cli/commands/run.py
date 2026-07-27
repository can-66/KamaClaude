from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

from kama_claude.core.config import KamaConfig
from kama_claude.core.transport.socket_client import IpcError, SocketClient

# S1 学习提示：
# StdoutPrinter 是 S1 就有的“事件 → 终端文字”适配器，值得精读。
# 原始 S1 的 handle 接收 Pydantic 事件并用 isinstance 分发；当前 dict/type 写法来自 S2 IPC。
# 真实 stage/s1 的 cmd_run 会在本进程直接创建 AgentRunner；当前 main 从 S2 起改为
# SocketClient 连接 daemon，所以 _run_async 的订阅和 IPC 细节现在只需知道用途。


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

    # 当前 main 根据 dict 的 type 字段分发；原始 S1 在同一位置用 isinstance
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
    # ---------------- S2+ IPC 包装：理解 S1 时可先跳到 cmd_run ----------------
    client = SocketClient(config.host, config.port)
    try:
        await client.connect()
    except (ConnectionRefusedError, OSError):
        print(f"error: core not running ({config.host}:{config.port})", file=sys.stderr)
        return 1

    printer = StdoutPrinter()
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
        await client.send_command("agent.run", {"goal": goal})
    except IpcError as e:
        print(f"error: {e}", file=sys.stderr)
        loop_task.cancel()
        await client.close()
        return 1

    # 这里等的是事件流中的 run.finished，不是 agent.run 命令的即时 RPC 响应。
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
