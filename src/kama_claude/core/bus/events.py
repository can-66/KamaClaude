from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Discriminator

# Command 表示“请 Core 做什么”，Event 表示“Core 里发生了什么”。
# S0 只有启动事件 CoreStartedEvent；丰富的执行事件在后续阶段才出现。
# S1 精读 Run / Step / Tool / LLM / LogLine 事件；它们共享 type、run_id、ts，
# type 是反序列化时的“事件种类标签”，run_id 把同一次任务的事件串起来。

# daemon 成功启动监听后可发布的事件模型
class CoreStartedEvent(BaseModel):
    type: Literal["core.started"] = "core.started"
    listen_addr: str  # e.g. "127.0.0.1:7437"
    version: str  # 正在运行的 daemon 版本


# ---------------- S1 及以后：S0 学习到这里可以先停 ----------------
# S1 重点是下面的一次 Agent run 可观测事件。

# 宣布一次 run 已创建；goal 是用户交给 Agent 的原始目标
class RunStartedEvent(BaseModel):
    type: Literal["run.started"] = "run.started"
    run_id: str
    goal: str
    ts: str  # ISO 8601


# 宣布 run 的最终状态；reason 只在失败时解释终止原因
class RunFinishedEvent(BaseModel):
    type: Literal["run.finished"] = "run.finished"
    run_id: str
    status: str  # "success" | "failed"
    reason: str | None = None  # "exceeded_max_steps" | "cancelled" | "llm_error" | ...
    steps: int
    ts: str


# 宣布第 N 次“调用 LLM 并按需执行工具”的步骤开始
class StepStartedEvent(BaseModel):
    type: Literal["step.started"] = "step.started"
    run_id: str
    step: int
    ts: str


# 宣布第 N 步正常走到末尾；异常或取消可能在发布它之前离开该步
class StepFinishedEvent(BaseModel):
    type: Literal["step.finished"] = "step.finished"
    run_id: str
    step: int
    ts: str


# 工具执行前发布；tool_use_id 来自模型，用来把请求与结果精确配对
class ToolCallStartedEvent(BaseModel):
    type: Literal["tool.call_started"] = "tool.call_started"
    run_id: str
    tool_use_id: str
    tool_name: str
    params: dict[str, Any]
    ts: str


# 工具成功返回后发布；output 是后续阶段为 TUI 展示补充的字段
class ToolCallFinishedEvent(BaseModel):
    type: Literal["tool.call_finished"] = "tool.call_finished"
    run_id: str
    tool_use_id: str
    tool_name: str
    elapsed_ms: int
    output: str = ""  # tool result content, for TUI display
    ts: str


# 工具未完成时发布；错误被结构化记录后仍可作为 tool_result 交还模型
class ToolCallFailedEvent(BaseModel):
    type: Literal["tool.call_failed"] = "tool.call_failed"
    run_id: str
    tool_use_id: str
    tool_name: str
    # "runtime_error" | "timeout" | "schema_error" | "permission_denied" | "rate_limited"
    error_class: str
    error_message: str
    elapsed_ms: int
    attempt: int = 1  # 1=first attempt, 2=first retry, 3=second retry
    ts: str


# 模型每流出一小段文字就发布一次，因此终端不必等完整回答生成
class LlmTokenEvent(BaseModel):
    type: Literal["llm.token"] = "llm.token"
    run_id: str
    token: str
    ts: str


# 一次模型调用结束后的 token 用量；context_pct 是后续压缩阶段新增
class LlmUsageEvent(BaseModel):
    type: Literal["llm.usage"] = "llm.usage"
    run_id: str
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    context_pct: float = 0.0
    ts: str


# 记录本步实际选择的模型；S1 只有 static 策略
class LlmModelSelectedEvent(BaseModel):
    type: Literal["llm.model_selected"] = "llm.model_selected"
    run_id: str
    model: str
    strategy: str  # "static" | "rule_based" | "cost_budget"
    ts: str


# 为结构化日志预留的事件模型；原始 S1 主链并没有实际发布它
class LogLineEvent(BaseModel):
    type: Literal["log.line"] = "log.line"
    run_id: str
    level: str  # "DEBUG" | "INFO" | "WARNING" | "ERROR"
    source: str
    message: str
    ts: str


# ---------------- S4+：会话事件，学习 S1 时从这里跳到 Event 联合 ----------------

class SessionCreatedEvent(BaseModel):
    type: Literal["session.created"] = "session.created"
    session_id: str
    mode: str
    ts: str


class SessionMessageReceivedEvent(BaseModel):
    type: Literal["session.message_received"] = "session.message_received"
    session_id: str
    content: str
    ts: str


class SessionWaitingForInputEvent(BaseModel):
    type: Literal["session.waiting_for_input"] = "session.waiting_for_input"
    session_id: str
    last_run_id: str
    ts: str


class SessionResumedEvent(BaseModel):
    type: Literal["session.resumed"] = "session.resumed"
    session_id: str
    ts: str


class SessionClosedEvent(BaseModel):
    type: Literal["session.closed"] = "session.closed"
    session_id: str
    ts: str


class ContextCompactedEvent(BaseModel):
    type: Literal["context.compacted"] = "context.compacted"
    session_id: str
    run_id: str
    original_tokens: int
    summary_tokens: int
    ts: str


class PermissionRequestedEvent(BaseModel):
    type: Literal["permission.requested"] = "permission.requested"
    run_id: str
    tool_use_id: str
    tool_name: str
    params: dict[str, Any]
    param_preview: str
    session_id: str
    ts: str


class PermissionGrantedEvent(BaseModel):
    type: Literal["permission.granted"] = "permission.granted"
    run_id: str
    tool_use_id: str
    # "allow_once" | "always_allow" | "auto_allow"
    decision: str
    ts: str


class PermissionDeniedEvent(BaseModel):
    type: Literal["permission.denied"] = "permission.denied"
    run_id: str
    tool_use_id: str
    # "deny_once" | "always_deny" | "auto_deny"
    decision: str
    ts: str


class SubagentStartedEvent(BaseModel):
    type: Literal["subagent.started"] = "subagent.started"
    run_id: str          # 子 agent run_id
    parent_run_id: str
    description: str
    ts: str


class SubagentFinishedEvent(BaseModel):
    type: Literal["subagent.finished"] = "subagent.finished"
    run_id: str
    parent_run_id: str
    status: str          # "success" | "failed"
    ts: str


class SkillInvokedEvent(BaseModel):
    type: Literal["skill.invoked"] = "skill.invoked"
    skill_name: str
    arguments: str
    run_id: str
    ts: str


# 根据 type 字段决定事件类型；这与 commands.py 中的 Command 联合采用同一思路。
# 当前 main 的联合包含所有阶段，S1 只需识别上面的执行事件。
Event = Annotated[
    CoreStartedEvent
    | RunStartedEvent
    | RunFinishedEvent
    | StepStartedEvent
    | StepFinishedEvent
    | ToolCallStartedEvent
    | ToolCallFinishedEvent
    | ToolCallFailedEvent
    | LlmTokenEvent
    | LlmUsageEvent
    | LlmModelSelectedEvent
    | LogLineEvent
    | SessionCreatedEvent
    | SessionMessageReceivedEvent
    | SessionWaitingForInputEvent
    | SessionResumedEvent
    | SessionClosedEvent
    | ContextCompactedEvent
    | PermissionRequestedEvent
    | PermissionGrantedEvent
    | PermissionDeniedEvent
    | SubagentStartedEvent
    | SubagentFinishedEvent
    | SkillInvokedEvent,
    Discriminator("type"),
]
