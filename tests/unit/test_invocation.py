from __future__ import annotations

import asyncio

from pydantic import BaseModel

from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry

# 本文件把工具实现换成极小 stub，专门验证 invoke_tool 的统一执行边界。
# 这些核心用例源自 S1；Pydantic 参数模型与重试行为是当前 main 的 S5+ 增强。

# --- stub tools --------------------------------------------------------------


# 正常返回输入内容的工具，同时用 Pydantic 模型声明必填参数
class _EchoParams(BaseModel):
    msg: str


# 正常路径 stub：用于观察成功结果和 finished 事件
class _EchoTool(BaseTool):
    name = "echo"
    description = "Echoes the msg param"
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {"msg": {"type": "string"}},
        "required": ["msg"],
    }
    params_model = _EchoParams

    # 原样返回 msg，排除真实文件 I/O 对调用包装测试的干扰
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult(content=str(params["msg"]))


# 超时路径 stub：休眠时间远大于测试传入的 timeout
class _SlowTool(BaseTool):
    name = "slow"
    description = "Sleeps forever"
    input_schema: dict[str, object] = {"type": "object", "properties": {}, "required": []}

    # 模拟迟迟不返回的外部工具
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        await asyncio.sleep(60)
        return ToolResult(content="done")


# 异常路径 stub：每次调用都抛出可识别的错误
class _BrokenTool(BaseTool):
    name = "broken"
    description = "Always raises"
    input_schema: dict[str, object] = {"type": "object", "properties": {}, "required": []}

    # 抛出 RuntimeError，验证 invoke_tool 会把异常转换成 ToolResult
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        raise RuntimeError("boom")


# --- helpers -----------------------------------------------------------------


# 快速构造模型返回的 tool_use 请求
def _call(name: str, inp: dict[str, object] | None = None, uid: str = "t1") -> ToolCallBlock:
    return ToolCallBlock(id=uid, name=name, input=inp or {})


# 为每个用例创建独立 bus，执行调用并同时返回结果与事件
async def _run(
    registry: ToolRegistry,
    tool_call: ToolCallBlock,
    timeout: float = 5.0,
) -> tuple[ToolResult, list[BaseModel]]:
    bus = EventBus()
    events: list[BaseModel] = []

    # 收集调用包装层发布的生命周期事件
    async def _collect(e: BaseModel) -> None:
        events.append(e)

    bus.subscribe(_collect)
    result = await invoke_tool(registry, tool_call, bus, run_id="r1", timeout=timeout)
    return result, events


# --- tests -------------------------------------------------------------------


# 功能：验证正常调用时返回工具内容且发布 started + finished 事件
# 设计：同时检查返回值和事件序列，因为 invoke_tool 的双重职责是"返回结果 + 发布事件"，缺一不可
async def test_success_returns_content_and_finished_event() -> None:
    registry = ToolRegistry()
    registry.register(_EchoTool())
    result, events = await _run(registry, _call("echo", {"msg": "hi"}))
    assert not result.is_error
    assert result.content == "hi"
    types = [e.type for e in events]  # type: ignore[attr-defined]
    assert types[0] == "tool.call_started"
    assert "tool.call_finished" in types
    assert "tool.call_failed" not in types


# 功能：验证调用不存在的工具时返回 runtime_error 并发布 failed 事件而非 finished
# 设计：传入空 registry，确认 error_type 和事件类型同时正确，排除"未知工具却发布了 finished"的情况
async def test_unknown_tool_returns_runtime_error() -> None:
    result, events = await _run(ToolRegistry(), _call("nonexistent"))
    assert result.is_error
    assert result.error_type == "runtime_error"
    assert "unknown tool" in result.content
    types = [e.type for e in events]  # type: ignore[attr-defined]
    assert "tool.call_started" in types
    assert "tool.call_failed" in types
    assert "tool.call_finished" not in types


# 功能：验证缺少必填参数时返回 schema_error 而非 runtime_error
# 设计：注册需要 msg 参数的 EchoTool 但传空 input，确认错误分类准确，schema 错误与运行时错误对 S4 重试策略有不同影响
async def test_missing_required_param_gives_schema_error() -> None:
    registry = ToolRegistry()
    registry.register(_EchoTool())
    result, events = await _run(registry, _call("echo", {}))  # "msg" is required
    assert result.is_error
    assert result.error_type == "schema_error"
    types = [e.type for e in events]  # type: ignore[attr-defined]
    assert "tool.call_failed" in types


# 功能：验证工具执行超时时返回 timeout 类型错误而非 runtime_error
# 设计：使用永久 sleep 的 SlowTool + 极短超时（50ms），测试 asyncio.wait_for 的超时路径，确认超时被正确分类
async def test_timeout_gives_timeout_error() -> None:
    registry = ToolRegistry()
    registry.register(_SlowTool())
    result, events = await _run(registry, _call("slow"), timeout=0.05)
    assert result.is_error
    assert result.error_type == "timeout"
    types = [e.type for e in events]  # type: ignore[attr-defined]
    assert "tool.call_failed" in types


# 功能：验证工具内部抛出异常时被捕获并转为 runtime_error，错误信息保留原始异常消息
# 设计：工具直接 raise RuntimeError，确认异常不向上传播（invoke_tool 的"不抛异常"契约），error_message 包含 "boom"
async def test_runtime_exception_gives_runtime_error() -> None:
    registry = ToolRegistry()
    registry.register(_BrokenTool())
    result, events = await _run(registry, _call("broken"))
    assert result.is_error
    assert result.error_type == "runtime_error"
    assert "boom" in result.content


# 功能：验证 tool.call_started 始终是第一个被发布的事件，即使工具调用最终失败
# 设计：用不存在的工具触发失败路径，确认即使失败也先发布 started，保证事件流的时序可观测性
async def test_started_event_always_first() -> None:
    result, events = await _run(ToolRegistry(), _call("nonexistent"))
    assert events[0].type == "tool.call_started"  # type: ignore[attr-defined]
