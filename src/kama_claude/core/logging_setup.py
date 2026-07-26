from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from kama_claude.core.config import KamaConfig

# S0 里日志有两个去向：终端 stderr，以及可选的滚动日志文件。
# 日志走 stderr 而不是 stdout，避免污染 `kama ping` 等命令的正常输出。
_TEXT_FMT = 'level=%(levelname)s ts=%(asctime)s source=%(name)s msg="%(message)s"'
_JSON_FMT = '{"level":"%(levelname)s","ts":"%(asctime)s","source":"%(name)s","msg":"%(message)s"}'


# 根据配置初始化 root logger：设置级别、格式，并挂载 stderr 和可选的滚动文件 handler
def setup_logging(config: KamaConfig) -> None:
    # 配置写错日志级别时安全回退到 INFO，避免 daemon 因日志配置无法启动。
    level = getattr(logging, config.logging.level.upper(), logging.INFO)
    fmt = _JSON_FMT if config.logging.format == "json" else _TEXT_FMT
    formatter = logging.Formatter(fmt, datefmt="%Y-%m-%dT%H:%M:%S")

    # root logger 是所有模块 logger 的共同上游；清空 handler 可避免重复初始化。
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    # stderr 始终启用，因此即使关闭文件日志，启动错误仍能在终端看到。
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    root.addHandler(stderr_handler)

    # file 为空字符串时跳过文件日志，集成测试借此避免产生无关文件。
    if config.logging.file:
        log_path = Path(config.logging.file).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # 单个日志文件最多 10 MB，最多保留 5 份旧文件，防止无限占用磁盘。
        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
