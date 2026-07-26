from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncGenerator

import pytest

# 这里放跨测试文件复用的“夹具”（fixture）。
# S0 最关键的是 running_daemon：测试会真的启动第二个 Python 进程，而不是 mock 一个假 daemon。

# 为每个测试向操作系统申请一个临时空闲端口，避免固定 7437 与本机服务冲突
@pytest.fixture
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        # 端口写 0 表示“请操作系统自动分配一个当前空闲端口”。
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # 离开 with 后探测 socket 已释放，随后启动的 daemon 才能绑定该端口。
    return port


# 启动真实 kama-core 子进程，等待它就绪；测试结束后无论成功失败都回收进程
@pytest.fixture
async def running_daemon(free_port: int) -> AsyncGenerator[subprocess.Popen[bytes], None]:
    # 复制当前环境，再只覆盖测试需要的配置，避免修改父进程的 os.environ。
    env = os.environ.copy()
    env["KAMA_PORT"] = str(free_port)
    env["KAMA_LOG_FILE"] = ""
    env["KAMA_LOG_LEVEL"] = "WARNING"

    # `python -m kama_claude.core` 最终会进入 core/__main__.py → app.run()。
    proc = subprocess.Popen([sys.executable, "-m", "kama_claude.core"], env=env)

    # 子进程创建后不代表端口已开始监听，所以最多轮询 3 秒等待真正就绪。
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", free_port)
            writer.close()
            await writer.wait_closed()
            break
        except (ConnectionRefusedError, OSError):
            pass
    else:
        proc.terminate()
        proc.wait()
        pytest.fail("Daemon did not start within 3 seconds")

    # yield 前是测试准备，yield 后是清理；测试函数在这里拿到运行中的进程。
    yield proc

    # 先礼貌 terminate，2 秒仍不退出才 kill，防止测试遗留后台进程。
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
