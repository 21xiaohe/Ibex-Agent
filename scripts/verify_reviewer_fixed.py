"""Force writes through the real Docker-backed team reviewer and check the source."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch
from langchain.messages import AIMessage
from agent import main as builder
from subagents import profiles
from sandbox.lifecycle import create_backend, release_backend
from tests_service.test_agent_mechanisms import ScriptedModel


async def main() -> None:
    """Attempt write_file and shell writes through the actual team task tool."""
    sandbox = await create_backend()
    try:
        await sandbox.aupload_files([("/home/agent/latest.txt", b"latest-code")])
        parent = ScriptedModel(responses=[
            AIMessage(content="", tool_calls=[{"name": "task", "args": {"subagent_type": "reviewer", "description": "Review current code"}, "id": "r"}]), AIMessage(content="done")])
        reviewer = ScriptedModel(responses=[
            AIMessage(content="", tool_calls=[{"name": "write_file", "args": {"file_path": "/home/agent/forbidden.txt", "content": "bad"}, "id": "w"}]),
            AIMessage(content="", tool_calls=[{"name": "execute", "args": {"command": "echo bad > /home/agent/latest.txt; touch /home/agent/forbidden-shell.txt"}, "id": "e"}]),
            AIMessage(content="", tool_calls=[{"name": "ReviewResult", "args": {"approved": False, "summary": "readonly checked", "issues": [], "security_notes": []}, "id": "v"}])])
        with patch.object(builder, "create_backend", AsyncMock(return_value=sandbox)), patch.object(builder, "build_model", return_value=parent), patch.object(profiles, "get_model_for_role", return_value=reviewer):
            graph, _ = await builder.build_agent_for_service(team=True)
            result = await graph.ainvoke({"messages": [{"role": "user", "content": "Review"}]})
        files = await sandbox.adownload_files(["/home/agent/latest.txt", "/home/agent/forbidden.txt", "/home/agent/forbidden-shell.txt"])
        assert files[0].content == b"latest-code" and files[1].error and files[2].error
        assert result["messages"][-1].content == "done"
        report = {"source_unchanged": True, "write_file_blocked": True, "shell_write_blocked": True, "final": "done"}
        Path("logs/reviewer-docker-fixed.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))
    finally:
        await release_backend(sandbox)


if __name__ == "__main__":
    asyncio.run(main())
