from __future__ import annotations

from dataclasses import dataclass, field


# 汇总一次 LLM 调用的 token 用量；缓存字段用于观察 prompt caching 是否命中
@dataclass
class UsageStats:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    context_pct: float = 0.0  # S6+：输入 token 占模型上下文窗口的比例


# 模型原生 tool_use block 的内部表示；id 会原样带到对应 tool_result
@dataclass
class ToolCallBlock:
    id: str
    name: str
    input: dict[str, object]


# Provider 交给 AgentLoop 的统一结果，隔离 Anthropic SDK 的具体对象类型
@dataclass
class LlmResponse:
    stop_reason: str  # S1 重点："end_turn" | "tool_use"
    tool_calls: list[ToolCallBlock] = field(default_factory=list)
    text: str = ""  # 流式 token 最终拼接出的完整文字
    usage: UsageStats | None = None
    # S7+ extended thinking：必须原样放回历史；S1 学习时可跳过
    thinking_blocks: list[dict[str, object]] = field(default_factory=list)
