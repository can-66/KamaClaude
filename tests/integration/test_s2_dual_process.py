from __future__ import annotations

import asyncio
import subprocess
from typing import Any

from kama_claude.core.transport.socket_client import SocketClient

# S2 最关键的真实进程验收：fixture 启动 kama-core，测试进程充当一个或两个客户端。
# 三个用例分别证明“远程启动”“多客户端扇出”“断线后回放”，都不需要真实 LLM Key。


# 功能：验证 agent.run 命令返回非空 run_id，且 daemon 随即广播 run.started 事件
# 设计：SocketClient 发命令，Event 等推送并设 5 秒超时；run.started 早于 LLM 初始化，故无需 API Key
async def test_agent_run_returns_run_id_and_emits_started(
    running_daemon: subprocess.Popen[bytes],
    free_port: int,
) -> None:
    client = SocketClient("127.0.0.1", free_port)
    await client.connect()

    started_event: asyncio.Event = asyncio.Event()
    received: dict[str, Any] = {}

    # 只捕获本用例关心的 run.started，并用 Event 唤醒测试协程
    async def on_event(event: dict[str, Any]) -> None:
        if event.get("type") == "run.started":
            received.update(event)
            started_event.set()

    client.on_event(on_event)
    loop_task = asyncio.create_task(client.run_event_loop())

    try:
        await client.send_command("event.subscribe", {"topics": ["run.*"], "scope": "global"})
        result = await client.send_command("agent.run", {"goal": "hello"})

        assert result.get("run_id"), "run_id must be non-empty"
        returned_run_id: str = result["run_id"]

        await asyncio.wait_for(started_event.wait(), timeout=5.0)
        assert received.get("run_id") == returned_run_id
        assert received.get("goal") == "hello"
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)
        await client.close()


# 功能：验证两个独立客户端同时订阅后，其中一个触发 agent.run，两个都能收到 run.started 广播
# 设计：用 gather 并行等待两个 SocketClient 的 Event，隔离验证一个发布者向所有订阅者扇出
async def test_two_clients_both_receive_broadcast(
    running_daemon: subprocess.Popen[bytes],
    free_port: int,
) -> None:
    client1 = SocketClient("127.0.0.1", free_port)
    client2 = SocketClient("127.0.0.1", free_port)
    await client1.connect()
    await client2.connect()

    event1: asyncio.Event = asyncio.Event()
    event2: asyncio.Event = asyncio.Event()

    # 记录 client1 是否收到广播
    async def on_event1(event: dict[str, Any]) -> None:
        if event.get("type") == "run.started":
            event1.set()

    # 记录 client2 是否收到同一广播
    async def on_event2(event: dict[str, Any]) -> None:
        if event.get("type") == "run.started":
            event2.set()

    client1.on_event(on_event1)
    client2.on_event(on_event2)

    loop1 = asyncio.create_task(client1.run_event_loop())
    loop2 = asyncio.create_task(client2.run_event_loop())

    try:
        await client1.send_command("event.subscribe", {"topics": ["run.*"], "scope": "global"})
        await client2.send_command("event.subscribe", {"topics": ["run.*"], "scope": "global"})
        await client1.send_command("agent.run", {"goal": "broadcast test"})

        await asyncio.wait_for(
            asyncio.gather(event1.wait(), event2.wait()),
            timeout=5.0,
        )
    finally:
        loop1.cancel()
        loop2.cancel()
        await asyncio.gather(loop1, loop2, return_exceptions=True)
        await client1.close()
        await client2.close()


# 功能：验证客户端断开后使用 replay_from_run 重连，订阅响应中 replayed_count > 0
# 设计：client1 触发并捕获 run_id，client2 重连回放；只断言计数，避免依赖 LLM 和完整任务结果
async def test_disconnect_and_replay_from_run(
    running_daemon: subprocess.Popen[bytes],
    free_port: int,
) -> None:
    # 第一阶段：触发 run，并等 run.started 已经通过事件链到达客户端
    client1 = SocketClient("127.0.0.1", free_port)
    await client1.connect()

    started_event: asyncio.Event = asyncio.Event()
    run_id_holder: list[str] = []

    # 保存第一阶段事件里的 run_id，供第二个客户端指定 replay_from_run
    async def on_event(event: dict[str, Any]) -> None:
        if event.get("type") == "run.started":
            run_id_holder.append(event.get("run_id", ""))
            started_event.set()

    client1.on_event(on_event)
    loop1 = asyncio.create_task(client1.run_event_loop())

    try:
        await client1.send_command("event.subscribe", {"topics": ["run.*"], "scope": "global"})
        await client1.send_command("agent.run", {"goal": "replay test"})
        await asyncio.wait_for(started_event.wait(), timeout=5.0)
    finally:
        loop1.cancel()
        await asyncio.gather(loop1, return_exceptions=True)
        await client1.close()

    assert run_id_holder, "run.started was never received"
    run_id = run_id_holder[0]

    # 给 EventWriter 一小段刷新时间；这也说明此测试靠时序近似确认落盘
    await asyncio.sleep(0.05)

    # 第二阶段：换一条新连接，按 run_id 请求历史回放
    client2 = SocketClient("127.0.0.1", free_port)
    await client2.connect()
    loop2 = asyncio.create_task(client2.run_event_loop())

    try:
        result = await client2.send_command(
            "event.subscribe",
            {
                "topics": ["run.*"],
                "scope": "global",
                "replay_from_run": run_id,
            },
        )
        assert result.get("replayed_count", 0) > 0, (
            f"Expected replayed_count > 0 for run_id={run_id!r}, got {result}"
        )
    finally:
        loop2.cancel()
        await asyncio.gather(loop2, return_exceptions=True)
        await client2.close()
