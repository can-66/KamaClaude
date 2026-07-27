"""
End-to-end integration test for the S1 agent pipeline.

Requires a real ANTHROPIC_API_KEY — skipped automatically when absent.
Run explicitly:
    uv run pytest tests/integration/test_run_e2e.py -v
Or with the marker:
    uv run pytest -m integration -v
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from kama_claude.core.config import KamaConfig
from kama_claude.core.runner import AgentRunner

# 这是 S1 唯一会访问真实 Anthropic API 的集成测试；没有 key 时会明确 skip。
# 它直接调用 AgentRunner，刻意绕过当前 main 的 S2 IPC 与 S4 session 外壳。

# 不经过 get_config()，因此在模块加载时单独读取项目 .env 中的 API key
load_dotenv(Path(__file__).parent.parent.parent / ".env", override=False)

pytestmark = pytest.mark.integration


@pytest.fixture()
# 创建只含一个可识别数字的临时文件，供真实模型请求 read_file
def sample_file(tmp_path: Path) -> Path:
    f = tmp_path / "sample.txt"
    f.write_text(
        "# Test Document\n\nThe magic number mentioned in this file is 7391.\n",
        encoding="utf-8",
    )
    return f


# 功能：验证真实 LLM 会发起 read_file，run 能成功结束并写出基础事件
# 设计：检查 read_file started 与任意 finished；未按 ID 证明其成功，也未断言回答含 7391
async def test_run_e2e_reads_file_and_succeeds(
    sample_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")

    # ReadFileTool 按当前工作目录解释相对路径，因此让 CWD 指向 sample.txt 所在目录。
    monkeypatch.chdir(tmp_path)

    goal = (
        "Use the read_file tool to read the file 'sample.txt' "
        "and report the magic number it mentions."
    )
    runs_dir = tmp_path / "runs"

    config = KamaConfig()
    config.agent.max_steps = 5

    runner = AgentRunner(config, runs_dir=runs_dir)
    await runner.run(goal)

    # ── events.jsonl must exist ──────────────────────────────────────────────
    jsonl_files = list(runs_dir.rglob("events.jsonl"))
    assert len(jsonl_files) == 1, "expected exactly one events.jsonl"

    events = [
        json.loads(line)
        for line in jsonl_files[0].read_text(encoding="utf-8").splitlines()
        if line
    ]
    types = [e["type"] for e in events]

    # ── event sequence assertions (from §6.4) ────────────────────────────────
    assert types[0] == "run.started"
    assert types[-1] == "run.finished"
    assert "step.started" in types
    assert "tool.call_started" in types
    assert "tool.call_finished" in types
    assert "llm.usage" in types

    # ── run completed successfully ────────────────────────────────────────────
    finished = events[-1]
    assert finished["status"] == "success", (
        f"run finished with status={finished['status']!r}, reason={finished.get('reason')!r}"
    )

    # ── 模型确实发起过 read_file；started 本身不证明该工具已成功返回 ──────────
    tool_starts = [e for e in events if e["type"] == "tool.call_started"]
    assert any(e["tool_name"] == "read_file" for e in tool_starts), (
        "expected at least one read_file tool call"
    )

    # ── run_id is consistent across the event stream ─────────────────────────
    run_id = events[0]["run_id"]
    assert all(e["run_id"] == run_id for e in events), "run_id must be the same in every event"

    # ── LLM cache stats are present ──────────────────────────────────────────
    usage_events = [e for e in events if e["type"] == "llm.usage"]
    assert len(usage_events) >= 1
    for ue in usage_events:
        assert "input_tokens" in ue
        assert "output_tokens" in ue
        assert "cache_read_input_tokens" in ue
