# 运维手册（RUNBOOK）

## 环境与路径

项目固定在 `E:\工作\agent项目\KamaClaude`，使用 Conda 环境 `E:\conda_envs\kamaclaude`。所有配置、日志、trace、session、缓存和临时文件都必须位于项目目录，不得写入 C 盘。

```powershell
Set-Location "E:\工作\agent项目\KamaClaude"
conda activate "E:\conda_envs\kamaclaude"

$env:TEMP = "$PWD\.tmp"
$env:TMP = "$PWD\.tmp"
$env:TMPDIR = "$PWD\.tmp"
$env:PIP_CACHE_DIR = "$PWD\.cache\pip"
$env:UV_CACHE_DIR = "$PWD\.uv-cache"
$env:CONDA_PKGS_DIRS = "$PWD\.cache\conda-pkgs"
New-Item -ItemType Directory -Force `
  ".tmp", ".cache\pip", ".cache\conda-pkgs", ".uv-cache" | Out-Null
```

## 日常操作

### 启动守护进程

```powershell
# uv
uv run kama-core

# Conda
kama-core
```

默认监听 `127.0.0.1:7437`，前台运行时按 `Ctrl+C` 优雅退出。

### 验证连通

```powershell
# uv
uv run kama ping

# Conda
kama ping
```

正常输出类似：

```text
pong server=0.0.1 uptime=12ms latency=2ms
```

### 后台启动、查看和停止

```powershell
# uv
uv run kama core start
uv run kama core status
uv run kama core stop

# Conda
kama core start
kama core status
kama core stop
```

PID 文件位于项目内 `.kama/kama-core.pid`。

## 配置

优先级由低到高为：内置默认值 → 项目内 `.kama/config.toml` → 项目 `.env` → 系统环境变量。

### `.kama/config.toml`

```toml
[core]
host = "127.0.0.1"
port = 7437

[logging]
level = "INFO"
file = ".kama/logs/core.log"
format = "text"

[trace]
enabled = true
file = ".kama/traces/daemon.jsonl"
```

### `.env`

在 PowerShell 中从模板复制后修改。该文件存放本机配置和密钥，不提交到 Git：

```powershell
Copy-Item ".env.example" ".env"
```

### 常用环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `KAMA_CONFIG` | `.kama/config.toml` | 配置文件路径 |
| `KAMA_HOST` | `127.0.0.1` | TCP 监听地址 |
| `KAMA_PORT` | `7437` | TCP 监听端口 |
| `KAMA_LOG_LEVEL` | `INFO` | 日志级别 |
| `KAMA_LOG_FILE` | `.kama/logs/core.log` | Core 日志路径，留空则仅输出 stderr |
| `KAMA_LOG_FORMAT` | `text` | `text` 或 `json` |
| `KAMA_TRACE_FILE` | `.kama/traces/daemon.jsonl` | Trace 路径 |
| `KAMA_TUI_LOG_FILE` | `.kama/logs/tui.log` | TUI 日志路径 |

路径环境变量不得指向项目目录之外。

## 开发与验证

```powershell
# uv
uv run ruff check src tests scripts
uv run mypy src
uv run pytest tests -v
uv run pytest tests/unit -v
uv run python scripts/gen_protocol_doc.py --check

# Conda
ruff check src tests scripts
mypy src
python -m pytest tests -v
python -m pytest tests/unit -v
python scripts/gen_protocol_doc.py --check
```

## 日志

```powershell
Get-Content ".kama\logs\core.log" -Wait
Get-Content ".kama\logs\tui.log" -Wait
```

## 常见错误

| 报错 | 原因 | 处理 |
|---|---|---|
| `NotImplementedError`，栈位于 `loop.add_signal_handler` | Windows 事件循环不支持该接口 | 使用已加入同步信号回退的新版代码 |
| `core already running at 127.0.0.1:7437` | 已有守护进程运行 | 执行 `kama core status`，确认后执行 `kama core stop` |
| `core not running` | 守护进程未启动 | 执行 `kama-core` 或 `kama core start` |
| `Address already in use` | 端口被其他进程占用 | 先设置 `$env:KAMA_PORT = "8000"`，再启动 |
| `Config error: KAMA_PORT must be an integer` | `.env` 或系统环境变量中的端口不是整数 | 检查 `KAMA_PORT` |
