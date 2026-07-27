# S1 工具系统公共入口：定义工具、注册工具、执行一次调用并返回 ToolResult。
# AgentLoop 只依赖这些抽象，不需要知道 read_file 的文件读取细节。
from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry

__all__ = ["BaseTool", "ToolResult", "ToolRegistry", "invoke_tool"]
