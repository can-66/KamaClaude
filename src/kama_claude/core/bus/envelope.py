from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# “信封”只描述消息怎么包装，不关心 core.ping 具体做什么。
# 可以把它类比成快递单：id 是单号，method 是收件部门，params/result 是包裹内容。
# S2 的关键新增是 EventPushEnvelope：同一条 TCP 连接既回 JSON-RPC 响应，也收 daemon 主动事件。

# 客户端发给 daemon 的 JSON-RPC 请求外壳
class JsonRpcRequest(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"  # Literal 限定只能是协议版本 "2.0"
    id: str  # 请求唯一标识；服务端响应会原样带回，客户端据此配对
    method: str  # 路由名，例如 "core.ping"
    params: dict[str, Any] = Field(default_factory=dict)  # 本次调用的业务参数


# S2 用于 daemon 主动向客户端推送事件；kind 让 SocketClient 能与 JSON-RPC 响应分流
class EventPushEnvelope(BaseModel):
    kind: Literal["event"] = "event"  # 推送没有请求 id，靠该固定值识别
    event: dict[str, Any]  # Pydantic Event 经 model_dump() 后的网络形态


# handler 正常完成时，daemon 返回的成功外壳
class JsonRpcSuccess(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: str  # 必须与请求 id 相同
    result: Any  # 具体结构由 PongResult 等业务结果模型继续校验


# JSON-RPC 错误的主体；code 便于程序判断，message 便于人阅读
class JsonRpcErrorObject(BaseModel):
    code: int
    message: str
    data: Any = None  # 可选调试信息；生产客户端不能依赖其具体格式


# 请求失败时，daemon 返回的错误外壳
class JsonRpcError(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: str | None = None  # 连请求本身都无法解析时，服务端可能不知道 id
    error: JsonRpcErrorObject


# 标准错误码让客户端不必解析自然语言 message 就能分类失败。
PARSE_ERROR = -32700  # 收到的字节不是合法 JSON
INVALID_REQUEST = -32600  # JSON 合法，但不符合 JsonRpcRequest 结构
METHOD_NOT_FOUND = -32601  # method 没有注册对应 handler
INVALID_PARAMS = -32602  # params 不符合业务模型；真实 S0 定义了但未接入此错误码
INTERNAL_ERROR = -32603  # handler 出现未预期异常


# 后续阶段的 handler 可主动抛出此异常；S0 主要关注上面的五类标准错误
class HandlerError(Exception):
    """命令 handler 抛出此异常，SocketServer 将其转换为结构化 JSON-RPC 错误响应。"""

    # 保存错误码和附加数据，交给 SocketServer 统一转换成 JsonRpcError
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


# 构造一个 JSON-RPC 错误响应对象
def make_error(id: str | None, code: int, message: str, data: Any = None) -> JsonRpcError:
    return JsonRpcError(id=id, error=JsonRpcErrorObject(code=code, message=message, data=data))
