# AGENT.md

本文档用于指导 Codex 在本仓库中开发和验证代码。

## 固定开发环境

- 项目目录：`E:\工作\agent项目\KamaClaude`
- Conda 虚拟环境：`E:\conda_envs\kamaclaude`
- 默认终端：Windows PowerShell
- Python 版本：3.12

开始工作前先进入项目并激活指定环境：

```powershell
Set-Location "E:\工作\agent项目\KamaClaude"
conda activate "E:\conda_envs\kamaclaude"
```

## 路径与磁盘写入约束

1. 禁止在 C 盘创建或修改任何项目文件、缓存、日志、配置、临时文件、测试产物或虚拟环境。
2. 除已经固定在 `E:\conda_envs\kamaclaude` 的 Conda 环境外，所有运行产物只能写入项目目录 `E:\工作\agent项目\KamaClaude`。
3. 项目运行数据统一写入项目内 `.kama/`，包括配置、日志、trace、policy、session、skill、agent 和 context。
4. 工具缓存和临时文件统一写入项目内 `.cache/`、`.tmp/`、`.pytest_cache/`、`.mypy_cache/`、`.ruff_cache/` 或 `.uv-cache/`。
5. 禁止使用会解析到用户主目录的 `~/.kama`、`Path.home()` 或类似默认路径。
6. 执行可能产生缓存或临时文件的命令前，在当前 PowerShell 会话设置：

```powershell
$env:TEMP = "$PWD\.tmp"
$env:TMP = "$PWD\.tmp"
$env:TMPDIR = "$PWD\.tmp"
$env:PIP_CACHE_DIR = "$PWD\.cache\pip"
$env:UV_CACHE_DIR = "$PWD\.uv-cache"
$env:CONDA_PKGS_DIRS = "$PWD\.cache\conda-pkgs"
New-Item -ItemType Directory -Force `
  ".tmp", ".cache\pip", ".cache\conda-pkgs", ".uv-cache" | Out-Null
```

## 常用命令

下面每项先给出 `uv` 操作，再给出当前 Conda 环境中的等价操作。不要创建项目内 `.venv`；本机开发优先使用指定的 Conda 环境。

### 安装或同步依赖

```powershell
# uv
uv sync

# Conda 环境
conda activate "E:\conda_envs\kamaclaude"
python -m pip install -e .
python -m pip install ruff mypy pytest pytest-asyncio
```

### 代码检查

```powershell
# uv
uv run ruff check src tests scripts
uv run mypy src

# Conda 环境
ruff check src tests scripts
mypy src
```

### 测试

```powershell
# uv
uv run pytest tests/unit -v
uv run pytest tests/integration -v
uv run pytest tests -v

# Conda 环境
python -m pytest tests/unit -v
python -m pytest tests/integration -v
python -m pytest tests -v
```

运行单个测试：

```powershell
# uv
uv run pytest tests/unit/test_envelope.py::test_request_roundtrip -v

# Conda 环境
python -m pytest tests/unit/test_envelope.py::test_request_roundtrip -v
```

### 生成并校验协议文档

修改总线模型后必须重新生成 `WIRE_PROTOCOL.md`：

```powershell
# uv
uv run python scripts/gen_protocol_doc.py
uv run python scripts/gen_protocol_doc.py --check

# Conda 环境
python scripts/gen_protocol_doc.py
python scripts/gen_protocol_doc.py --check
```

### 启动守护进程

```powershell
# uv
uv run kama-core
$env:KAMA_PORT = "8000"
uv run kama-core

# Conda 环境
kama-core
$env:KAMA_PORT = "8000"
kama-core
```

前台运行时按 `Ctrl+C` 停止。

### CLI 与 TUI

```powershell
# uv
uv run kama ping
uv run kama --version
uv run kama-tui

# Conda 环境
kama ping
kama --version
kama-tui
```

## 系统架构

这是一个双进程本地 AI Agent 系统。`kama-core` 是常驻守护进程，`kama` 和 `kama-tui` 通过 TCP 连接它：

```text
kama-core（守护进程）
  └─ 监听 127.0.0.1:7437（TCP）
       ↑ JSON-RPC 2.0 NDJSON
kama（CLI）   kama-tui（TUI）
```

`kama-tui` 是主要前端。涉及任务管理、可观测性和交互的用户界面功能，应优先在 TUI 中设计和验证。`kama` CLI 只用于快速脚本测试和调试，不作为主要产品界面。

### 协议层（`src/kama_claude/core/bus/`）

所有 IPC 消息都是 Pydantic v2 类型模型，并通过 `type` 字段组成可辨识联合。新增命令或事件时，需要在 `commands.py` 或 `events.py` 中新增模型，并扩展 `Command` 或 `Event` 联合。

- `envelope.py`：JSON-RPC 请求、成功响应、错误响应、错误码和 `make_error()`
- `commands.py`：命令及其结果模型
- `events.py`：事件模型

`WIRE_PROTOCOL.md` 由 `scripts/gen_protocol_doc.py` 自动生成。修改总线模型后必须重新生成并提交该文件。

### 传输层（`src/kama_claude/core/transport/`）

`socket_server.py` 基于 `asyncio.start_server` 实现 TCP 服务，读取 NDJSON、分发命令并处理 JSON-RPC 错误。启动时会探测目标地址；若已有守护进程监听则报错。处理器通过 `server.register("method.name", handler_fn)` 注册。

### 配置（`src/kama_claude/core/config.py`）

配置优先级由低到高为：内置默认值 → 项目内 `.kama/config.toml` → 项目 `.env` → 系统环境变量。

常用环境变量包括：

- `KAMA_CONFIG`
- `KAMA_HOST`
- `KAMA_PORT`
- `KAMA_LOG_LEVEL`
- `KAMA_LOG_FILE`
- `KAMA_LOG_FORMAT`

所有默认配置和数据路径必须位于项目目录，不得回退到用户主目录或 C 盘。

### 守护进程入口（`src/kama_claude/core/app.py`）

`CoreApp.run()` 是唯一异步入口：加载配置、初始化日志和 trace、创建 `SocketServer`、注册处理器、等待退出通知，最后停止服务并释放资源。新增处理器时，在 `CoreApp` 中实现方法并通过 `server.register()` 注册。

Windows 默认事件循环不支持 `loop.add_signal_handler()`；退出处理必须兼容 Windows，可在不支持信号回调时使用同步 `signal.signal()` 或其他跨平台方案。

### 测试

`tests/conftest.py` 使用随机空闲端口启动真实守护进程子进程，并轮询 `asyncio.open_connection` 等待服务就绪。测试运行产生的临时文件也必须通过前述环境变量留在项目内 `.tmp/`。

## 代码风格

所有函数的 `def` 行正上方必须有一行中文注释，简洁说明函数用途：

```python
# 发送 JSON-RPC 响应并刷新写缓冲区
async def _send(self, writer: asyncio.StreamWriter, msg: BaseModel) -> None:
    ...
```

不要写多行 docstring，一行简洁中文注释即可。

每个测试函数的 `def` 行正上方必须有两行中文注释：

```python
# 功能：验证 publish 后订阅者能收到事件对象
# 设计：用内联处理器收集事件引用，断言 is 而非 ==，排除序列化中间步骤干扰
async def test_publish_reaches_subscriber() -> None:
    ...
```

- `# 功能：`：一句话说明验证的具体行为或不变式。
- `# 设计：`：说明测试方法、边界条件以及所选 stub、fixture 或断言的理由。

两行缺一不可。

## 仓库外设计文档

规划文档位于仓库同级的 `../docs/`，不提交到本仓库：

- `agent_development_plan.md`：S0–S8 分阶段路线图
- `s0_implementation_plan.md`：S0 详细决策与理由
- `agent_functional_outline.md`：完整功能目录
