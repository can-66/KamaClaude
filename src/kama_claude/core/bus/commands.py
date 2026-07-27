from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Discriminator

from kama_claude.core.session.model import SessionMode, SessionStatus

# 本文件定义“业务命令参数/结果”，传输外壳则在 envelope.py。
# S2 只需精读 AgentRunCommand/Result 与 EventSubscribeCommand/Result；
# Session、Permission、Compact 是 S4-S6 在同一协议模式上的后续扩展。

# S0 只需精读 PingCommand 与 PongResult。
# 后面的 Agent、Session、Permission 命令都是沿用同一模式在后续阶段扩展出来的。

# ping 请求的业务参数模型；type 用来在 Command 联合中判别具体命令
class PingCommand(BaseModel):
    type: Literal["core.ping"] = "core.ping"
    client: str  # 谁发起了 ping，例如 "cli/0.0.1"


# ping 成功后的业务结果模型
class PongResult(BaseModel):
    server_version: str  # daemon 版本，用来发现客户端/服务端版本不一致
    uptime_ms: int  # daemon 已运行多久，不是本次网络延迟
    received_at: str  # ISO 8601


# ---------------- S2：把 S1 的本地 run 和事件流搬到 daemon ----------------

# 请求 daemon 在后台启动一次 run；goal 仍是 S1 AgentRunner 的输入
class AgentRunCommand(BaseModel):
    type: Literal["agent.run"] = "agent.run"
    goal: str  # 用户目标；daemon 会为它生成 run_id


# agent.run 的即时确认结果；它只表示“已受理”，不是任务最终答案
class AgentRunResult(BaseModel):
    run_id: str  # 后续事件与落盘目录都用它关联同一次运行


# 请求在当前 TCP 连接上订阅事件；注册前必须先拿到这条连接的 writer
class EventSubscribeCommand(BaseModel):
    type: Literal["event.subscribe"] = "event.subscribe"
    topics: list[str]  # fnmatch 模式，如 ["step.*", "tool.*"]，不是正则表达式
    scope: str = "global"  # "global" 接收所有 run；"run:<run_id>" 只接收指定 run
    replay_from_run: str | None = None  # 非空时先读取该 run 的 events.jsonl


# 订阅建立后的确认信息
class EventSubscribeResult(BaseModel):
    subscription_id: str  # 当前实现只返回给客户端展示，取消订阅仍按 writer 清理
    replayed_count: int = 0  # 建立实时订阅前实际回放的历史事件数


# ---------------- S4+：会话；学习 S2 时从这里跳到文件末尾的 Command 联合 ----------------

class SessionCreateCommand(BaseModel):
    type: Literal["session.create"] = "session.create"
    mode: SessionMode = "chat"
    title: str = ""


class SessionCreateResult(BaseModel):
    session_id: str
    status: SessionStatus


class SessionSendMessageCommand(BaseModel):
    type: Literal["session.send_message"] = "session.send_message"
    session_id: str
    content: str


class SessionSendMessageResult(BaseModel):
    run_id: str


class SessionGetHistoryCommand(BaseModel):
    type: Literal["session.get_history"] = "session.get_history"
    session_id: str


class SessionGetHistoryResult(BaseModel):
    messages: list[dict[str, Any]]


class SessionCloseCommand(BaseModel):
    type: Literal["session.close"] = "session.close"
    session_id: str


class SessionCloseResult(BaseModel):
    status: SessionStatus


class PermissionRespondCommand(BaseModel):
    type: Literal["permission.respond"] = "permission.respond"
    tool_use_id: str
    # "allow_once" | "always_allow" | "deny_once" | "always_deny"
    decision: str


class PermissionRespondResult(BaseModel):
    ok: bool = True


class SessionCompactCommand(BaseModel):
    type: Literal["session.compact"] = "session.compact"
    session_id: str
    focus: str = ""


class SessionCompactResult(BaseModel):
    summary_tokens: int
    saved_tokens: int


# 根据业务对象内部的 type 字段决定命令类型。
# 注意：SocketServer 真正路由请求靠 JSON-RPC 外层的 method；当前 handler 会直接校验具体模型，
# 所以不要把这里的 type 与请求外层的 method 混成同一个字段。
Command = Annotated[
    PingCommand
    | AgentRunCommand
    | EventSubscribeCommand
    | SessionCreateCommand
    | SessionSendMessageCommand
    | SessionGetHistoryCommand
    | SessionCloseCommand
    | PermissionRespondCommand
    | SessionCompactCommand,
    Discriminator("type"),
]
