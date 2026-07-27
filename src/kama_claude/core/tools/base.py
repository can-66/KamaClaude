from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from pydantic import BaseModel


# invoke_tool 会把工具返回值或捕获到的异常统一整理成 ToolResult，再交回 AgentLoop
@dataclass
class ToolResult:
    content: str  # 成功内容或可交给 LLM 阅读的错误说明
    is_error: bool = False  # AgentLoop 会把它写进下一次请求的 tool_result.is_error
    # "runtime_error" | "timeout" | "schema_error" | "permission_denied"
    error_type: str | None = None


# 所有工具的最小模板：元数据告诉 LLM 怎么调用，invoke() 负责真正执行
class BaseTool(ABC):
    name: str  # ToolRegistry 的唯一键，也必须与模型返回的 tool_use.name 一致
    description: str  # 给 LLM 阅读的用途说明
    input_schema: dict[str, object]  # 交给 Anthropic tools 参数的 JSON Schema
    # S5+ 使用 Pydantic 做本地完整校验；原始 S1 只检查 input_schema.required。
    params_model: ClassVar[type[BaseModel] | None] = None

    @abstractmethod
    # 执行工具调用，返回结果或错误
    async def invoke(self, params: dict[str, object]) -> ToolResult: ...
