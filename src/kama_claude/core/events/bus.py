from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import BaseModel

# EventHandler 是异步回调：收到一个 Pydantic 事件，处理完成后不返回业务结果。
type EventHandler = Callable[[BaseModel], Awaitable[None]]


# 进程内的轻量广播中心，让 AgentLoop 只负责“报告发生了什么”
class EventBus:
    # 创建空的订阅列表；同一个 handler 可被多次注册并多次收到事件
    def __init__(self) -> None:
        self._subscribers: list[EventHandler] = []

    # 注册一个事件处理函数
    def subscribe(self, handler: EventHandler) -> None:
        self._subscribers.append(handler)

    # 按注册顺序依次调用所有订阅者
    async def publish(self, event: BaseModel) -> None:
        # 顺序 await 让 events.jsonl 和终端看到一致顺序；代价是慢订阅者会拖慢发布者。
        # handler 异常不会在这里吞掉，调用方可以明确感知观测链路失败。
        for handler in self._subscribers:
            await handler(event)
