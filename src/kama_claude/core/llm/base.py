from __future__ import annotations

from typing import Protocol

from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse


# LLM 后端的结构协议；测试 stub 只要实现同形状的 chat() 就能替代真实 API
class LLMProvider(Protocol):
    # 流式调用 LLM 并发布进度事件，返回完整响应
    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,  # 后续阶段用于重试日志和 trace；S1 可忽略
        system: str | None = None,  # 后续阶段允许按 session/skill 覆盖 prompt
    ) -> LlmResponse: ...
