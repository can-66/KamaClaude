from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

from kama_claude.core.config import KamaConfig

# S2 的 daemon 生命周期命令：`kama core start/status/stop`。
# 它只管理后台进程，不参与 agent.run 的事件协议；真正的服务入口仍是 CoreApp.run()。

# 当前 main 把 PID 放在项目内；真实 stage/s2 原本使用 Path.home() / ".kama"
_PID_FILE = Path(".kama/kama-core.pid")


# 探测 daemon 端口是否可连接；这里只建连并关闭，并未发送 core.ping 请求
async def _ping_check(config: KamaConfig) -> None:
    _r, w = await asyncio.open_connection(config.host, config.port)
    w.close()
    await w.wait_closed()


# 读取 PID 文件并确认进程存活，进程已消失则删除文件并返回 None
def _running_pid() -> int | None:
    if not _PID_FILE.exists():
        return None
    try:
        pid = int(_PID_FILE.read_text().strip())
        # 信号 0 只探测 PID 是否存在，不会真正终止进程。
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        _PID_FILE.unlink(missing_ok=True)
        return None


# 打印 daemon 当前状态（running / not running）
def cmd_core_status(config: KamaConfig) -> None:
    try:
        asyncio.run(_ping_check(config))
        print(f"running  ({config.host}:{config.port})")
    except (ConnectionRefusedError, OSError):
        print("not running")


# 在后台启动 daemon，若已在运行则提示并退出
def cmd_core_start(config: KamaConfig) -> None:
    try:
        asyncio.run(_ping_check(config))
        print(f"already running  ({config.host}:{config.port})")
        return
    except (ConnectionRefusedError, OSError):
        pass

    proc = subprocess.Popen(
        # 用当前 Python 环境启动包入口，保证依赖与当前 kama CLI 一致。
        [sys.executable, "-m", "kama_claude.core"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(proc.pid))
    print(f"started  pid={proc.pid}  ({config.host}:{config.port})")


# 向 daemon 发送 SIGTERM 停止进程，若未运行则提示
def cmd_core_stop(config: KamaConfig) -> None:
    # config 保持与另外两个 core 子命令一致，但停止动作本身只依赖 PID 文件。
    pid = _running_pid()
    if pid is None:
        print("not running")
        return
    os.kill(pid, signal.SIGTERM)
    _PID_FILE.unlink(missing_ok=True)
    print(f"stopped  pid={pid}")
