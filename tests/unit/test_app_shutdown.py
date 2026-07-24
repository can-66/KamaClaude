from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable
from types import FrameType
from typing import Any

import pytest

from kama_claude.core.app import _register_shutdown_signal


class _UnsupportedSignalLoop:
    # 模拟 Windows 事件循环不支持 add_signal_handler
    def add_signal_handler(self, _sig: int, _callback: Callable[[], None]) -> None:
        raise NotImplementedError

    # 立即执行投递到事件循环的回调
    def call_soon_threadsafe(self, callback: Callable[[], None]) -> None:
        callback()


# 功能：验证事件循环不支持信号处理器时会回退到 signal.signal 并触发退出事件
# 设计：用 Windows 行为的最小 fake loop 和 monkeypatch 捕获同步 handler，避免依赖当前测试平台
def test_register_shutdown_signal_falls_back_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registered: dict[signal.Signals, Callable[[int, FrameType | None], None]] = {}

    # 捕获回退处理器，供测试主动模拟 Ctrl+C
    def fake_signal(
        sig: signal.Signals,
        handler: Callable[[int, FrameType | None], None],
    ) -> Any:
        registered[sig] = handler
        return signal.SIG_DFL

    monkeypatch.setattr(signal, "signal", fake_signal)
    shutdown = asyncio.Event()

    _register_shutdown_signal(  # type: ignore[arg-type]
        _UnsupportedSignalLoop(),
        shutdown,
        signal.SIGINT,
    )
    registered[signal.SIGINT](signal.SIGINT, None)

    assert shutdown.is_set()
