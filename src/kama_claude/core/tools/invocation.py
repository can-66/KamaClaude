from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from kama_claude.core.bus.events import (
    PermissionDeniedEvent,
    PermissionGrantedEvent,
    PermissionRequestedEvent,
    ToolCallFailedEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
)
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.tools.base import ToolResult
from kama_claude.core.tools.errors import RateLimitedError
from kama_claude.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from kama_claude.core.permissions.manager import PermissionManager

# S1 的核心职责仍是：started 事件 → 找工具/校验 → 限时执行 → finished 或 failed。
# 原始 S1 已区分 runtime/timeout/schema；当前 main 的 Pydantic、权限、attempt 和重试属于 S5+。
# 初学 S1 时把这些后续增强当作包在同一执行边界里的“安全外壳”即可。

_DEFAULT_TIMEOUT: float = 120.0
_MAX_RETRIES: int = 2
_RETRY_BASE_S: float = 2.0  # backoff base; tests can monkeypatch to 0
_RETRYABLE: frozenset[str] = frozenset({"runtime_error", "rate_limited"})


# 返回事件使用的 UTC ISO 8601 时间戳
def _now() -> str:
    return datetime.now(UTC).isoformat()


# 发布 ToolCallFailedEvent 并返回对应 ToolResult
async def _fail(
    bus: EventBus,
    run_id: str,
    tool_call: ToolCallBlock,
    error_class: str,
    error_message: str,
    elapsed_ms: int,
    *,
    attempt: int = 1,
) -> ToolResult:
    await bus.publish(
        ToolCallFailedEvent(
            run_id=run_id,
            tool_use_id=tool_call.id,
            tool_name=tool_call.name,
            error_class=error_class,
            error_message=error_message,
            elapsed_ms=elapsed_ms,
            attempt=attempt,
            ts=_now(),
        )
    )
    return ToolResult(content=error_message, is_error=True, error_type=error_class)


# 校验参数、检查权限、限时调用工具、发布进度事件，失败时指数退避重试，返回 ToolResult（不抛异常）
async def invoke_tool(
    registry: ToolRegistry,
    tool_call: ToolCallBlock,
    bus: EventBus,
    run_id: str,
    timeout: float = _DEFAULT_TIMEOUT,
    *,
    permission_manager: PermissionManager | None = None,
    session_id: str = "",
) -> ToolResult:
    # monotonic 只计算耗时，系统时钟被校准也不会让 elapsed 变成负数。
    t0 = time.monotonic()

    # 即使后续发现工具不存在，也先发布 started，事件时间线才有完整起点。
    await bus.publish(
        ToolCallStartedEvent(
            run_id=run_id,
            tool_use_id=tool_call.id,
            tool_name=tool_call.name,
            params=dict(tool_call.input),
            ts=_now(),
        )
    )

    # 把运行到当前时刻的秒数转换成便于事件展示的整数毫秒
    def elapsed() -> int:
        return int((time.monotonic() - t0) * 1000)

    tool = registry.get(tool_call.name)
    if tool is None:
        return await _fail(
            bus, run_id, tool_call,
            "runtime_error", f"unknown tool: {tool_call.name}", elapsed(),
        )

    # ---------------- S5+：原始 S1 仅按 input_schema.required 检查缺失键 ----------------
    if tool.params_model is not None:
        try:
            tool.params_model.model_validate(dict(tool_call.input))
        except ValidationError as exc:
            return await _fail(
                bus, run_id, tool_call,
                "schema_error", str(exc), elapsed(),
            )

    # ---------------- S5+ 权限审批：学习 S1 时可跳到下面的执行循环 ----------------
    if permission_manager is not None:
        # 把 PermissionManager 的原始字典重新包装成项目事件
        async def _emit_permission(raw: dict[str, Any]) -> None:
            await bus.publish(PermissionRequestedEvent(**raw, run_id=run_id))

        allowed, decision = await permission_manager.check_and_wait(
            tool_use_id=tool_call.id,
            tool_name=tool_call.name,
            params=dict(tool_call.input),
            session_id=session_id,
            event_emitter=_emit_permission,
        )
        if allowed:
            if decision not in ("auto_allow",):
                await bus.publish(
                    PermissionGrantedEvent(
                        run_id=run_id,
                        tool_use_id=tool_call.id,
                        decision=decision,
                        ts=_now(),
                    )
                )
        else:
            if decision != "auto_deny":
                await bus.publish(
                    PermissionDeniedEvent(
                        run_id=run_id,
                        tool_use_id=tool_call.id,
                        decision=decision,
                        ts=_now(),
                    )
                )
            return await _fail(
                bus, run_id, tool_call,
                "permission_denied",
                "Permission denied by user. You may not execute this command. "
                "Try an alternative approach or ask the user what to do.",
                elapsed(),
            )

    # ---------------- S5+ 重试：原始 S1 只执行一次 asyncio.wait_for ----------------
    for attempt in range(1, _MAX_RETRIES + 2):
        error_class: str | None = None
        error_message: str | None = None

        try:
            # wait_for 同时提供超时边界；超时会取消内部工具协程。
            result = await asyncio.wait_for(
                tool.invoke(dict(tool_call.input)), timeout=timeout
            )
            ms = elapsed()

            if result.is_error:
                # 工具主动返回的失败与抛异常统一进入相同分类/重试路径。
                error_class = result.error_type or "runtime_error"
                error_message = result.content
            else:
                await bus.publish(
                    ToolCallFinishedEvent(
                        run_id=run_id,
                        tool_use_id=tool_call.id,
                        tool_name=tool_call.name,
                        elapsed_ms=ms,
                        output=result.content,
                        ts=_now(),
                    )
                )
                return result

        except RateLimitedError as exc:
            error_class = "rate_limited"
            error_message = str(exc)
        except TimeoutError:
            return await _fail(
                bus, run_id, tool_call,
                "timeout", f"tool timed out after {timeout}s", elapsed(),
                attempt=attempt,
            )
        except Exception as exc:
            error_class = "runtime_error"
            error_message = str(exc)

        assert error_class is not None and error_message is not None
        ms = elapsed()

        if error_class in _RETRYABLE and attempt <= _MAX_RETRIES:
            # 指数退避为 2s、4s；测试会把基数 monkeypatch 为 0，避免真实等待。
            await bus.publish(
                ToolCallFailedEvent(
                    run_id=run_id,
                    tool_use_id=tool_call.id,
                    tool_name=tool_call.name,
                    error_class=error_class,
                    error_message=error_message,
                    elapsed_ms=ms,
                    attempt=attempt,
                    ts=_now(),
                )
            )
            await asyncio.sleep(_RETRY_BASE_S * (2 ** (attempt - 1)))
            continue

        return await _fail(
            bus, run_id, tool_call,
            error_class, error_message, ms,
            attempt=attempt,
        )

    # 理论上循环内一定 return；保留防御性返回帮助 mypy 证明函数覆盖所有路径。
    return ToolResult(content="internal error", is_error=True, error_type="runtime_error")
