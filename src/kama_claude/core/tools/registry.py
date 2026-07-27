from __future__ import annotations

from kama_claude.core.tools.base import BaseTool


# 工具目录：一边供运行时按 name 找实现，一边供 LLM 获取全部 schema
class ToolRegistry:
    # 创建名称到工具实例的映射；dict 会保留注册顺序
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    # 注册工具；同名覆盖
    def register(self, tool: BaseTool) -> None:
        # 覆盖语义让调用方可以注入同名测试替身，但也要小心无意覆盖。
        self._tools[tool.name] = tool

    # 按名称查找工具，不存在返回 None
    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    # 返回所有工具的 Anthropic 格式 schema 列表
    def tool_schemas(self) -> list[dict[str, object]]:
        # 返回的是描述数据，不包含 Python invoke 方法；模型只能“提出调用请求”。
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in self._tools.values()
        ]
