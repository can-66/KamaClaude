from __future__ import annotations

import asyncio
import fnmatch
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel

from kama_claude.core.bus.envelope import EventPushEnvelope
from kama_claude.core.trace.record import TraceRecord
from kama_claude.core.trace.writer import TraceWriter

logger = logging.getLogger(__name__)

# S2 的服务端事件扇出层：
# EventBus 把 Pydantic 事件交给 handle()，本类按 topic/scope 过滤后，
# 再把 EventPushEnvelope 写回每个订阅者原来的 TCP 连接。

# 生成 S3+ trace 记录使用的 UTC 时间戳；原始 S2 broadcaster 没有 trace
def _now() -> str:
    return datetime.now(UTC).isoformat()


# 保存一条连接的订阅条件；下划线表示只在本模块内部使用
@dataclass
class _Subscription:
    sub_id: str  # 返回给客户端的订阅标识
    writer: asyncio.StreamWriter  # 事件最终写回的连接
    topics: list[str]  # fnmatch 模式列表
    scope: str  # global 或 run:<run_id>


# 把 daemon 内部 EventBus 事件广播给所有匹配的 IPC 订阅者
class IpcEventBroadcaster:
    # 初始化内存订阅表；trace 是 S3+ 的可选旁路，不影响 S2 广播语义
    def __init__(self, trace: TraceWriter | None = None) -> None:
        self._subscriptions: list[_Subscription] = []
        self._trace = trace

    # 注册一个客户端订阅，返回 subscription_id
    def subscribe(
        self,
        writer: asyncio.StreamWriter,
        topics: list[str],
        scope: str = "global",
    ) -> str:
        sub_id = f"sub-{uuid.uuid4().hex[:8]}"
        sub = _Subscription(sub_id=sub_id, writer=writer, topics=topics, scope=scope)
        # 一个连接可以重复订阅；当前协议没有 event.unsubscribe，断开时按 writer 统一清理。
        self._subscriptions.append(sub)
        return sub_id

    # 移除指定 writer 的所有订阅
    def unsubscribe(self, writer: asyncio.StreamWriter) -> None:
        # 用 is 比较连接对象身份，避免两个值“看起来相等”的 mock 被误删。
        self._subscriptions = [s for s in self._subscriptions if s.writer is not writer]

    # 将事件推送到所有匹配的订阅客户端，写入失败时延迟清理死连接
    async def handle(self, event: BaseModel) -> None:
        # 网络另一端不认识 Pydantic 对象，先转成只含 JSON 兼容值的 dict。
        event_dict = event.model_dump()
        event_type: str = event_dict.get("type", "")
        run_id: str | None = event_dict.get("run_id")

        dead: list[asyncio.StreamWriter] = []

        # list(...) 做快照，确保稍后清理订阅时不会破坏当前遍历。
        # 写入按订阅者顺序 await，并非并行发送；慢客户端会延后后续订阅者。
        for sub in list(self._subscriptions):
            if not self._matches_topic(event_type, sub.topics):
                continue
            if not self._matches_scope(run_id, sub.scope):
                continue
            try:
                envelope = EventPushEnvelope(event=event_dict)
                sub.writer.write(envelope.model_dump_json().encode() + b"\n")
                await sub.writer.drain()
                # ---------------- S3+：记录 IPC trace，学习 S2 可跳过 ----------------
                if self._trace is not None:
                    client_id = str(sub.writer.get_extra_info("peername", "<unknown>"))
                    self._trace.emit(
                        TraceRecord(
                            ts=_now(),
                            direction="CORE→CLIENT",
                            layer="ipc",
                            kind="push",
                            run_id=run_id,
                            client_id=client_id,
                            data={"sub_id": sub.sub_id, "event_type": event_type},
                        )
                    )
            except (ConnectionResetError, BrokenPipeError, OSError):
                logger.debug("dead connection for sub %s, scheduling cleanup", sub.sub_id)
                dead.append(sub.writer)

        # 遍历结束后再统一删除，避免原地修改列表而跳过下一个订阅者。
        for writer in dead:
            self.unsubscribe(writer)

    # 检查事件类型是否匹配订阅的 topic 列表（支持 fnmatch glob 模式）
    @staticmethod
    def _matches_topic(event_type: str, topics: list[str]) -> bool:
        return any(fnmatch.fnmatch(event_type, pattern) for pattern in topics)

    # 检查事件 run_id 是否匹配订阅的 scope（global 全通，run:<id> 精确匹配）
    @staticmethod
    def _matches_scope(run_id: str | None, scope: str) -> bool:
        if scope == "global":
            return True
        if scope.startswith("run:"):
            return run_id == scope[4:]
        return False
