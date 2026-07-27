from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ExecutionContext 是一次 run 的“工作记忆”：AgentLoop 和工具结果都写入同一个对象。
# S1 核心字段是 run_id、goal、max_steps、messages、step、status、reason；
# prefill/notes/context/system override 是当前 main 在 S4-S7 追加的会话与扩展能力。

@dataclass
class ExecutionContext:
    run_id: str  # 一次运行的关联 ID，事件和落盘目录都靠它串联
    goal: str  # 用户最初输入的任务目标
    max_steps: int  # 循环硬上限，避免模型永远请求下一步
    # ---------------- S4+ 会话预填与记忆：原始 S1 没有这些字段 ----------------
    prefill_messages: list[dict[str, Any]] = field(default_factory=list)
    session_notes: str = ""
    global_context: str = ""
    project_context: str = ""
    # ---------------- S1 核心状态 ----------------
    messages: list[dict[str, Any]] = field(default_factory=list)
    step: int = 0
    status: str = "running"  # "running" | "success" | "failed"
    reason: str | None = None
    # 最终文字结果与 prompt 覆盖是后续调用场景补充；不影响 S1 消息循环的理解。
    result: str = ""
    # skill 或 subagent 角色可覆盖默认 system prompt
    system_prompt_override: str | None = None

    # 初始化消息历史，优先使用 session 完整回放内容
    def __post_init__(self) -> None:
        if self.prefill_messages:
            # 复制每条消息的外层 dict，避免 runner 与 context 共用同一个可变字典。
            self.messages = [dict(m) for m in self.prefill_messages]
        elif not self.messages:
            # 原始 S1 走这条分支：goal 成为 Anthropic messages 的第一条 user 消息。
            self.messages.append({"role": "user", "content": self.goal})

    # ---------------- S4+ system prompt 记忆注入：S1 可跳过此方法 ----------------

    # 返回当前 run 的 system prompt；有 override 时跳过 base，直接注入记忆层
    def system_prompt(self, base: str) -> str:
        parts = [self.system_prompt_override if self.system_prompt_override else base]
        if self.global_context.strip():
            parts.append("\n\n## Global Context\n" + self.global_context.strip())
        if self.project_context.strip():
            parts.append("\n\n## Project Context\n" + self.project_context.strip())
        if self.session_notes.strip():
            parts.append(
                "\n\n## Session Notes\n"
                + self.session_notes.strip()
                + "\n\nRemember important durable facts by calling note_save."
            )
        return "".join(parts)

    # 将 LLM 响应的 content blocks 追加为 assistant 消息
    def add_assistant_message(self, content: list[Any]) -> None:
        # content 不是纯字符串，还可能含 text、tool_use、thinking 等 block。
        self.messages.append({"role": "assistant", "content": content})

    # 将工具调用结果追加为 user 消息；同一步的多个结果共享同一条消息
    def add_tool_result(
        self, tool_use_id: str, content: str, is_error: bool = False
    ) -> None:
        # Anthropic 把工具执行结果视为下一条 user 内容，而不是 assistant 的一部分。
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
        }
        if is_error:
            block["is_error"] = True

        last = self.messages[-1] if self.messages else None
        if (
            last is not None
            and last["role"] == "user"
            and isinstance(last["content"], list)
            and last["content"]
            and all(b.get("type") == "tool_result" for b in last["content"])
        ):
            # 同一轮多个 tool_use 的结果必须合在同一条 user 消息中。
            last["content"].append(block)
        else:
            self.messages.append({"role": "user", "content": [block]})

    # 返回 True 表示 loop 应停止（状态不再是 running）
    def is_done(self) -> bool:
        return self.status != "running"

    # 将 run 标记为成功
    def mark_success(self) -> None:
        self.status = "success"

    # 将 run 标记为失败并记录原因
    def mark_failed(self, reason: str) -> None:
        self.status = "failed"
        self.reason = reason
