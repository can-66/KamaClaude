from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from kama_claude.core.tools.base import BaseTool, ToolResult

# S1 唯一内建工具：把本地文本文件读成字符串，让 LLM 获得“观察外部世界”的能力。
# 工具声明说路径应相对当前工作目录；真实实现只明确拒绝含 `..` 的路径，
# 并未拒绝绝对路径。这是阶段代码的已知边界，学习注释不擅自改变它。
_MAX_BYTES = 512 * 1024  # 512 KB


# S5+ 的本地参数模型；原始 S1 直接从 params 读取 path
class ReadFileParams(BaseModel):
    model_config = ConfigDict(extra="ignore")  # 多余键忽略，与工具 schema 宽容策略一致
    path: str  # 相对当前进程工作目录解释


# 读取 UTF-8 文本的工具实现；工具元数据会同时提供给 ToolRegistry 和 LLM
class ReadFileTool(BaseTool):
    params_model = ReadFileParams
    name = "read_file"
    description = (
        "Read the text content of a file. "
        "Path must be relative to the current working directory. "
        "Files larger than 512 KB are truncated."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the file (relative to current working directory).",
            }
        },
        "required": ["path"],
    }

    # 读取文件内容；超 512KB 截断；禁止 .. 路径遍历
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        # model_validate 让直接调用工具时也能检查 path 类型，而不只依赖 LLM 服务端。
        path_str = ReadFileParams.model_validate(params).path

        # 检查路径片段而不是字符串包含关系，避免把普通文件名中的两个点误判。
        if ".." in Path(path_str).parts:
            raise PermissionError(f"path traversal not allowed: {path_str}")

        path = Path(path_str)
        # read_bytes 是同步文件 I/O；S1 为保持最小实现接受这一点，并限制最多返回 512KB。
        raw = path.read_bytes()  # 文件不存在时让 FileNotFoundError 交给 invoke_tool 统一转换
        truncated = len(raw) > _MAX_BYTES
        # 非法 UTF-8 用替换字符保留其余文本，避免整次工具调用因单个坏字节失败。
        text = raw[:_MAX_BYTES].decode("utf-8", errors="replace")
        if truncated:
            text += "\n[truncated]"

        return ToolResult(content=text)
