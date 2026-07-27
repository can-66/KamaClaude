from __future__ import annotations

import argparse
import sys

from kama_claude.cli.commands.chat import cmd_chat
from kama_claude.cli.commands.core import cmd_core_start, cmd_core_status, cmd_core_stop
from kama_claude.cli.commands.ping import cmd_ping
from kama_claude.cli.commands.run import cmd_run
from kama_claude.cli.commands.trace import cmd_trace
from kama_claude.cli.commands.version import cmd_version
from kama_claude.core.config import get_config
from kama_claude.core.logging_setup import setup_logging

# S0 新手阅读路线：
# `kama ping` 会从本文件的 main() 进入，然后走到 commands/ping.py。
# chat、run、core、trace 都是后续阶段加入的命令，学习 S0 时暂时跳过。
#
# S1 新手阅读路线：
# `kama run --goal "..."` 仍从 main() 进入，再分发到 commands/run.py。
# 真实 stage/s1 会在 CLI 进程里直接创建 AgentRunner；当前 main 已采用 S2 的
# SocketClient 连接 daemon。先记住“run 是 S1 命令”，网络搬迁细节暂时跳过。


# CLI 主入口：解析命令行参数并分发到对应子命令
def main() -> None:
    # ArgumentParser 负责把终端里的字符串参数变成结构化的 args 对象。
    parser = argparse.ArgumentParser(prog="kama", description="KamaClaude CLI")
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    subparsers = parser.add_subparsers(dest="command")

    # S0 只有 ping；run 是 S1 新增入口，chat/core/trace 是后续阶段入口。
    subparsers.add_parser("ping", help="Ping the core daemon")
    subparsers.add_parser("chat", help="Start a multi-turn chat session")

    # argparse 会把 --goal 的字符串放进 args.goal，随后原样交给 cmd_run。
    run_parser = subparsers.add_parser("run", help="Run an agent task")
    run_parser.add_argument("--goal", required=True, help="Goal for the agent to accomplish")

    core_parser = subparsers.add_parser("core", help="Manage the core daemon")
    core_sub = core_parser.add_subparsers(dest="core_command")
    core_sub.add_parser("start", help="Start the daemon in the background")
    core_sub.add_parser("stop", help="Stop the running daemon")
    core_sub.add_parser("status", help="Show daemon status")

    trace_parser = subparsers.add_parser("trace", help="View system trace log")
    trace_parser.add_argument("run_id", nargs="?", default=None, help="Filter by run ID")
    trace_parser.add_argument("--layer", choices=["ipc", "event", "llm"], help="Filter by layer")
    trace_parser.add_argument("--direction", help="Filter by direction (e.g. CORE→LLM)")
    trace_parser.add_argument("--raw", action="store_true", help="Output raw NDJSON")
    trace_parser.add_argument("--follow", "-f", action="store_true", help="Follow new records")

    # 例如 `kama ping` 会得到 args.command == "ping"。
    args = parser.parse_args()

    if args.version:
        cmd_version()
        return

    # 所有需要连接 daemon 的子命令共用一份 host/port 和日志配置。
    config = get_config()
    setup_logging(config)

    if args.command == "ping":
        # S0 主线在这里离开参数解析层，进入真正的 TCP 客户端代码。
        cmd_ping(config)
    elif args.command == "chat":
        cmd_chat(config)
    elif args.command == "run":
        # 当前 main 从这里进入 S2 客户端；原始 S1 则从 cmd_run 直接进入 AgentRunner。
        cmd_run(args.goal, config)
    elif args.command == "core":
        if args.core_command == "start":
            cmd_core_start(config)
        elif args.core_command == "stop":
            cmd_core_stop(config)
        elif args.core_command == "status":
            cmd_core_status(config)
        else:
            core_parser.print_help()
            sys.exit(1)
    elif args.command == "trace":
        cmd_trace(
            args.run_id,
            config,
            layer=args.layer,
            direction=args.direction,
            raw=args.raw,
            follow=args.follow,
        )
    else:
        parser.print_help()
        sys.exit(1)
