from __future__ import annotations

import asyncio
import json
import sys
import time

import kama_claude
from kama_claude.core.bus.commands import PongResult
from kama_claude.core.bus.envelope import JsonRpcError, JsonRpcSuccess
from kama_claude.core.config import KamaConfig

# 这个文件就是 S0 的“客户端半边”：
# 同步入口 cmd_ping() 方便 CLI 调用，异步函数 _ping() 负责真实网络 I/O。


# 同步入口：运行 ping 协程，连接失败时打印错误并退出
def cmd_ping(config: KamaConfig) -> None:
    try:
        # asyncio.run() 创建事件循环，执行协程，完成后再关闭事件循环。
        asyncio.run(_ping(config))
    except (ConnectionRefusedError, OSError):
        # 最常见原因是另一个终端里还没有启动 kama-core。
        print(f"error: core not running ({config.host}:{config.port})", file=sys.stderr)
        sys.exit(1)


# 向 core 守护进程发送 ping 请求，打印 pong 响应及延迟
async def _ping(config: KamaConfig) -> None:
    # monotonic() 只会向前走，适合测耗时；系统时间被校准也不会影响它。
    t0 = time.monotonic()
    # 成功后得到 reader（收数据）和 writer（发数据），它们代表同一条 TCP 连接。
    reader, writer = await asyncio.open_connection(config.host, config.port)

    # JSON-RPC 外壳：id 对应请求和响应，method 决定服务端路由，params 是业务参数。
    req = {
        "jsonrpc": "2.0",
        "id": "cli-1",
        "method": "core.ping",
        "params": {"client": f"cli/{kama_claude.__version__}"},
    }
    # NDJSON 规定“一条 JSON 占一行”；末尾的换行符就是消息边界。
    writer.write((json.dumps(req) + "\n").encode())
    # drain() 等待写缓冲区回落，避免数据过量积压；它不代表对端已经处理完成。
    await writer.drain()

    # daemon 同样以换行结尾，因此 readline() 正好读回一整帧；最长等 10 秒。
    line = await asyncio.wait_for(reader.readline(), timeout=10.0)
    latency_ms = int((time.monotonic() - t0) * 1000)

    # 一次 ping 已完成，主动释放本端连接资源。
    writer.close()
    await writer.wait_closed()

    # 网络对面的数据都不应直接信任：先解析 JSON，再用 Pydantic 校验结构。
    raw = json.loads(line)
    if "error" in raw:
        err = JsonRpcError.model_validate(raw)
        print(f"error: {err.error.code} {err.error.message}", file=sys.stderr)
        sys.exit(1)

    # 分两层校验：先校验 JSON-RPC 成功外壳，再校验业务结果 PongResult。
    resp = JsonRpcSuccess.model_validate(raw)
    result = PongResult.model_validate(resp.result)
    print(f"pong server={result.server_version} uptime={result.uptime_ms}ms latency={latency_ms}ms")
