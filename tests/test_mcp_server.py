"""The MCP server over real stdio, as Claude Code starts it (skipped without the 'mcp' extra)."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp.server.fastmcp")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


async def _session(fn):
    params = StdioServerParameters(command=sys.executable, args=["-m", "factory.mcp_server"], cwd=str(ROOT))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            return await fn(session, init)


def _json(result):
    return json.loads(result.content[0].text)


def test_handshake_tools_and_overview():
    async def check(session, init):
        assert init.serverInfo.name == "task-factory"
        assert "Only executed gates decide validity" in (init.instructions or "")
        names = {t.name for t in (await session.list_tools()).tools}
        assert {"run_pipeline", "validate_draft", "gate_report", "review_task", "run_benchmark",
                "read_report"} <= names
        overview = _json(await session.call_tool("factory_overview", {"pipeline_file": "configs/hackathon.toml"}))
        assert set(overview["repos"]) == {"toyshop", "textkit"}
        prompt = await session.get_prompt("author_task", {"feature": "gift cards"})
        assert "gift cards" in prompt.messages[0].content.text
    asyncio.run(_session(check))


def test_background_job_runs_a_factory_command():
    async def check(session, _):
        job = _json(await session.call_tool("run_pipeline", {"pipeline_file": "configs/no-such-file.toml"}))
        for _ in range(100):
            status = _json(await session.call_tool("job_status", {"job_id": job["job_id"]}))
            if status["status"] != "running":
                break
            await asyncio.sleep(0.2)
        assert status["status"].startswith("failed"), status  # a missing pipeline file is a clean failure
        assert "no-such-file" in status["log_tail"]
    asyncio.run(_session(check))
