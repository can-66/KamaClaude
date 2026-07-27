from __future__ import annotations

import logging
from pathlib import Path
from typing import IO

from pydantic import BaseModel

from kama_claude.core.events.bus import EventBus

logger = logging.getLogger(__name__)

# S1 的事件持久化器：每个 Pydantic 事件占 events.jsonl 中的一行
# JSONL 适合边运行边追加；即使文件只写了一半，也能读取此前完整的行。

# 用 async context manager 管理事件文件的打开、刷新与关闭
class EventWriter:
    # 只保存目标路径；真正打开文件推迟到 async with 入口
    def __init__(self, path: Path) -> None:
        self._path = path
        self._file: IO[str] | None = None

    # 打开事件文件（追加模式），供 async with 使用
    async def __aenter__(self) -> EventWriter:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # 追加模式保留同路径已有记录；encoding 显式指定以避免 Windows 本地编码差异。
        self._file = open(self._path, "a", encoding="utf-8")
        return self

    # 关闭事件文件
    async def __aexit__(self, *args: object) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    # 将事件序列化为 JSON 行并写入文件，写入失败时记录日志但不抛出异常
    async def handle(self, event: BaseModel) -> None:
        if self._file is None:
            return
        try:
            # 一行就是一个事件；换行既是 JSONL 分隔符，也是后续流式读取的消息边界。
            self._file.write(event.model_dump_json() + "\n")
            # 每条立即 flush，牺牲少量吞吐换取进程异常时尽量少丢事件。
            self._file.flush()
        except (OSError, ValueError) as e:
            # 观测文件失败不应反过来终止 Agent 主任务。
            logger.error("EventWriter: failed to write event: %s", e)

    # 将 handle 注册为 bus 的订阅者
    def subscribe(self, bus: EventBus) -> None:
        bus.subscribe(self.handle)
