from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.provider import AnthropicProvider
from kama_claude.core.llm.types import LlmResponse

# 本文件完全伪造 Anthropic stream，不发网络请求，验证 S1 Provider 的翻译与事件职责。
# MagicMock 模拟 SDK 对象，FakeStream 模拟 async with 和异步 token 迭代器。

# --- helpers -----------------------------------------------------------------


# 构造只包含 Provider 会读取字段的 usage 假对象
def _make_usage(
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read: int = 0,
    cache_create: int = 0,
) -> MagicMock:
    u = MagicMock()
    u.input_tokens = input_tokens
    u.output_tokens = output_tokens
    u.cache_read_input_tokens = cache_read
    u.cache_creation_input_tokens = cache_create
    return u


# 构造 Anthropic 最终消息假对象，可注入 stop_reason、content 与 token 用量
def _make_final(
    stop_reason: str = "end_turn",
    content: list[MagicMock] | None = None,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read: int = 0,
) -> MagicMock:
    msg = MagicMock()
    msg.stop_reason = stop_reason
    msg.content = content or []
    msg.usage = _make_usage(input_tokens, output_tokens, cache_read)
    return msg


# 模拟 Anthropic SDK 的流式上下文管理器
class FakeStream:
    """Minimal async context manager that fakes the anthropic streaming interface."""

    # 保存即将逐段产出的文字和最终完整消息
    def __init__(self, texts: list[str], final: MagicMock) -> None:
        self._texts = texts
        self._final = final

    # 进入 async with 时返回 stream 自身
    async def __aenter__(self) -> FakeStream:
        return self

    # 离开 async with 无额外资源需要释放
    async def __aexit__(self, *args: object) -> None:
        pass

    @property
    # 返回异步生成器，模拟 SDK 每次吐出一段 token 文本
    def text_stream(self):  # type: ignore[return]
        # 按预设顺序逐段产出文本
        async def _gen():
            for t in self._texts:
                yield t

        return _gen()

    # 返回包含 stop_reason、content、usage 的最终消息
    async def get_final_message(self) -> MagicMock:
        return self._final


# 把 FakeStream 接到可注入 client 上，得到不会访问网络的 Provider
def _make_provider(
    texts: list[str] | None = None,
    stop_reason: str = "end_turn",
    content: list[MagicMock] | None = None,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read: int = 0,
) -> tuple[AnthropicProvider, MagicMock]:
    final = _make_final(stop_reason, content, input_tokens, output_tokens, cache_read)
    client = MagicMock()
    client.messages.stream.return_value = FakeStream(texts or [], final)
    return AnthropicProvider(model="test-model", client=client), client


# 创建事件收集器并执行一次 Provider.chat，返回规范化结果与事件序列
async def _chat(
    provider: AnthropicProvider,
    messages: list[dict[str, object]] | None = None,
    tool_schemas: list[dict[str, object]] | None = None,
) -> tuple[LlmResponse, list[BaseModel]]:
    collected: list[BaseModel] = []
    bus = EventBus()

    # 收集 Provider 在调用期间广播的 model/token/usage 事件
    async def _collect(e: BaseModel) -> None:
        collected.append(e)

    bus.subscribe(_collect)
    result = await provider.chat(
        messages=messages or [],
        tool_schemas=tool_schemas or [],
        bus=bus,
        run_id="r1",
    )
    return result, collected


# --- tests -------------------------------------------------------------------


# 功能：验证每次 chat 调用都发布携带模型名和路由策略的 llm.model_selected 事件
# 设计：检查事件数量为 1 且字段精确匹配，因为 S6 路由报告依赖此事件统计模型使用分布
async def test_model_selected_event_published() -> None:
    provider, _ = _make_provider()
    _, events = await _chat(provider)
    sel = [e for e in events if e.type == "llm.model_selected"]  # type: ignore[attr-defined]
    assert len(sel) == 1
    assert sel[0].model == "test-model"  # type: ignore[attr-defined]
    assert sel[0].strategy == "static"  # type: ignore[attr-defined]
    assert sel[0].run_id == "r1"  # type: ignore[attr-defined]


# 功能：验证流式响应的每个 token 触发独立的 llm.token 事件，内容与顺序均正确
# 设计：使用 FakeStream 控制精确的 token 序列，断言数量和各 token 的值，排除批量合并或跳过情况
async def test_token_events_published_per_chunk() -> None:
    provider, _ = _make_provider(texts=["Hello", " world"])
    _, events = await _chat(provider)
    tokens = [e for e in events if e.type == "llm.token"]  # type: ignore[attr-defined]
    assert len(tokens) == 2
    assert tokens[0].token == "Hello"  # type: ignore[attr-defined]
    assert tokens[1].token == " world"  # type: ignore[attr-defined]


# 功能：验证 llm.usage 事件中的 token 统计字段（input、output、cache_read）正确
# 设计：向 FakeStream 注入固定 usage 值，断言三个字段精确匹配，因为这些字段是 S6 成本计算的数据源
async def test_usage_event_published_after_stream() -> None:
    provider, _ = _make_provider(input_tokens=200, output_tokens=75, cache_read=150)
    _, events = await _chat(provider)
    usage_events = [e for e in events if e.type == "llm.usage"]  # type: ignore[attr-defined]
    assert len(usage_events) == 1
    ue = usage_events[0]
    assert ue.input_tokens == 200  # type: ignore[attr-defined]
    assert ue.output_tokens == 75  # type: ignore[attr-defined]
    assert ue.cache_read_input_tokens == 150  # type: ignore[attr-defined]


# 功能：验证事件发布顺序为 model_selected → token（×N） → usage
# 设计：检查类型列表的首尾元素，聚焦 provider 的时序契约，而非中间 token 事件的顺序（那由流本身决定）
async def test_event_order_model_selected_first_usage_last() -> None:
    provider, _ = _make_provider(texts=["hi"])
    _, events = await _chat(provider)
    types = [e.type for e in events]  # type: ignore[attr-defined]
    assert types[0] == "llm.model_selected"
    assert "llm.token" in types
    assert types[-1] == "llm.usage"


# 功能：验证 stop_reason=tool_use 时 final_message content block 被正确解析为 ToolCallBlock
# 设计：注入带 tool_use block 的 final_message，逐字段检查 ToolCallBlock 的 id/name/input，确认解析路径完整
async def test_tool_use_parsed_from_final_message() -> None:
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.id = "toolu_01"
    tool_block.name = "read_file"
    tool_block.input = {"path": "README.md"}
    provider, _ = _make_provider(stop_reason="tool_use", content=[tool_block])
    result, _ = await _chat(provider)
    assert result.stop_reason == "tool_use"
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.id == "toolu_01"
    assert tc.name == "read_file"
    assert tc.input == {"path": "README.md"}


# 功能：验证 stop_reason=end_turn 时不产生任何工具调用
# 设计：与 test_tool_use_parsed 互补，确认 stop_reason 决策树的另一侧，防止 end_turn 响应被误解析为 tool_use
async def test_end_turn_produces_no_tool_calls() -> None:
    provider, _ = _make_provider(stop_reason="end_turn")
    result, _ = await _chat(provider)
    assert result.stop_reason == "end_turn"
    assert result.tool_calls == []


# 功能：验证多个流式 token 被正确拼接为 LlmResponse.text 字段
# 设计：三段 token 拼接后检查完整字符串；AgentLoop 用它写 assistant 历史和最终结果
async def test_text_accumulated_from_tokens() -> None:
    provider, _ = _make_provider(texts=["foo", "bar", "baz"])
    result, _ = await _chat(provider)
    assert result.text == "foobarbaz"


# 功能：验证缺少 ANTHROPIC_API_KEY 时 provider 初始化立即 SystemExit 而非等到调用时才报错
# 设计：用 monkeypatch 清除环境变量后实例化，确认 fail-fast 行为，防止"幽灵 run"（有 started 但无 finished 事件）
async def test_missing_api_key_raises_system_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        AnthropicProvider(model="any")


# 功能：验证空流式响应不发布任何 llm.token 事件且 result.text 为空字符串
# 设计：texts=[] 覆盖零 token 边界条件，确认 text="" 而非 None，避免调用方对空内容做额外 None 判断
async def test_no_tokens_when_response_is_empty() -> None:
    provider, _ = _make_provider(texts=[])
    result, events = await _chat(provider)
    tokens = [e for e in events if e.type == "llm.token"]  # type: ignore[attr-defined]
    assert tokens == []
    assert result.text == ""
