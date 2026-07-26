.PHONY: lint test integration-test docs verify-s0

# 静态检查：Ruff 查常见代码问题，Mypy 查类型问题
lint:
	uv run ruff check src tests scripts
	uv run mypy src

# 快速单元测试：不要求手动启动 daemon
test:
	uv run pytest tests/unit -v

# 集成测试：fixture 会自行启动和回收真实 daemon
integration-test:
	uv run pytest tests/integration -v

# 从 Pydantic 模型重新生成 WIRE_PROTOCOL.md
docs:
	uv run python scripts/gen_protocol_doc.py

# S0 一站式验收：环境、静态检查、单元测试、ping 集成测试、协议同步
verify-s0:
	uv sync --frozen
	uv run ruff check src tests scripts
	uv run mypy src
	uv run pytest tests/unit -v
	uv run pytest tests/integration -k ping -v
	uv run python scripts/gen_protocol_doc.py --check
