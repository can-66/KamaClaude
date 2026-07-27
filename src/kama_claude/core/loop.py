from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from kama_claude.core.bus.events import StepFinishedEvent, StepStartedEvent
from kama_claude.core.context import ExecutionContext
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.base import LLMProvider
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry
import logging

if TYPE_CHECKING:
    from kama_claude.core.compact.compactor import Compactor
    from kama_claude.core.permissions.manager import PermissionManager


log = logging.getLogger(__name__)

# 返回步骤事件使用的 UTC ISO 8601 时间戳
def _now() -> str:
    return datetime.now(UTC).isoformat()


# S1 的决策发动机：反复调用 LLM、记录响应、执行工具并回填结果
class AgentLoop:
    # 初始化循环所需依赖：LLM provider、工具注册表、事件总线，以及可选的权限管理器、压缩器和 session ID
    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        bus: EventBus,
        *,
        permission_manager: PermissionManager | None = None,
        compactor: Compactor | None = None,
        compact_threshold: float = 0.80,
        session_id: str = "",
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._bus = bus
        # ---------------- S5/S6+：S1 原始构造器只有上面三个依赖 ----------------
        self._permission_manager = permission_manager
        self._compactor = compactor
        self._compact_threshold = compact_threshold
        self._session_id = session_id

    # 驱动“调用 LLM → 记录响应 → 执行工具/回填结果”循环；CancelledError 向上传播
    async def run(self, context: ExecutionContext) -> None:
        while not context.is_done():
            # step 从 1 开始；原始 S1 每轮调用一次 provider.chat，但一次响应可含多个 tool_use。
            context.step += 1
            await self._bus.publish(
                StepStartedEvent(run_id=context.run_id, step=context.step, ts=_now())
            )

            # [plan] call LLM — API errors terminate the run
            try:
                response = await self._provider.chat(
                    messages=context.messages,
                    tool_schemas=self._registry.tool_schemas(),
                    bus=self._bus,
                    run_id=context.run_id,
                    step=context.step,
                    system=context.system_prompt(
                        "You are a helpful AI assistant. "
                        "Use the available tools to complete the user's goal. "
                        "When the goal is fully achieved, respond with a final answer "
                        "and do not call any more tools."
                    ),
                )
            except asyncio.CancelledError:
                # 取消是控制流信号：先记录状态，再原样上抛给 AgentRunner 收尾。
                context.mark_failed("cancelled")
                raise
            except Exception:
                logging.getLogger(__name__).exception(
                    "LLM call failed run_id=%s step=%d", context.run_id, context.step
                )
                context.mark_failed("llm_error")
                # 这里直接 break，因此失败的这一轮不会再发布 step.finished。
                break

            # [observe] append assistant content blocks to context
            # thinking blocks must come first and be preserved verbatim for extended thinking mode
            # 先 observe 再 act 很关键：tool_result 必须紧跟产生它的 assistant tool_use。
            blocks: list[dict[str, object]] = list(response.thinking_blocks)
            if response.text:
                blocks.append({"type": "text", "text": response.text})
            for tc in response.tool_calls:
                blocks.append(
                    {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input}
                )
            context.add_assistant_message(blocks)

            # [act] execute each requested tool; errors become tool results so loop continues
            if response.stop_reason == "tool_use":
                for tc in response.tool_calls:
                    result = await invoke_tool(
                        self._registry, tc, self._bus, context.run_id,
                        permission_manager=self._permission_manager,
                        session_id=self._session_id,
                    )
                    # invoke_tool 无论成功失败都返回 ToolResult，错误也交回 LLM 自行恢复。
                    context.add_tool_result(tc.id, result.content, is_error=result.is_error)
            elif response.stop_reason == "max_tokens" and response.tool_calls:
                # ---------------- 后续增强：S1 原始代码没有 max_tokens 平衡分支 ----------------
                # Output token limit hit mid-tool-call; input is incomplete.
                # Add synthetic error results so the conversation stays balanced.
                for tc in response.tool_calls:
                    context.add_tool_result(
                        tc.id,
                        "Error: output token limit reached before this tool call could be completed. "
                        "Please break the task into smaller steps and try again.",
                        is_error=True,
                    )

            # Termination check — end_turn wins over max_steps if both hit on same step
            if response.stop_reason == "end_turn":
                # end_turn 代表模型认为任务已完成；同一步撞上 max_steps 时成功优先。
                context.result = response.text or ""
                context.mark_success()
            elif context.step >= context.max_steps:
                context.mark_failed("exceeded_max_steps")

            # ---------------- S6+ 自动压缩：学习 S1 时可跳到 StepFinishedEvent ----------------
            # 工具结果追加完毕（messages 末尾为 user）后检查压缩，仅在 run 继续时触发
            # 此时压缩结果 [user_summary, assistant_ack] 对下一次 LLM 调用是合法输入
            if (
                not context.is_done()
                and response.stop_reason == "tool_use"
                and self._compactor is not None
                and self._compact_threshold > 0
                and response.usage is not None
                and response.usage.context_pct >= self._compact_threshold
            ):
                await self._compactor.compact(context, self._provider)

            await self._bus.publish(
                StepFinishedEvent(run_id=context.run_id, step=context.step, ts=_now())
            )
