"""Probe the shipped five-role graph with deterministic tool calls in a temporary directory."""
import asyncio
import json
from pathlib import Path
import tempfile
from unittest.mock import patch, AsyncMock

from deepagents.backends import FilesystemBackend
from langchain.messages import AIMessage
from tests_service.test_agent_mechanisms import ScriptedModel
from agent import main as agent_main
from subagents import profiles


async def main() -> None:
    """Record role dispatch and whether the existing reviewer can actually write."""
    roles = ["planner", "researcher", "coder", "tester", "reviewer"]
    models = {role: ScriptedModel(responses=[AIMessage(content=f"{role}-ok")]) for role in roles}
    models["reviewer"] = ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "write_file", "args": {"file_path": "/reviewer-write-probe.txt", "content": "reviewer wrote this"}, "id": "review-write"}]),
        AIMessage(content="", tool_calls=[{"name": "ReviewResult", "args": {"approved": True, "summary": "reviewer-ok", "issues": [], "security_notes": []}, "id": "structured-review"}]),
    ])
    parent = ScriptedModel(responses=[
        *(AIMessage(content="", tool_calls=[{"name": "task", "args": {"subagent_type": role, "description": "Return your role result."}, "id": f"task-{role}"}]) for role in roles),
        AIMessage(content="team-done"),
    ])
    with tempfile.TemporaryDirectory(prefix="ibex-team-verification-") as directory:
        backend = FilesystemBackend(root_dir=directory, virtual_mode=True)
        backend.workdir = "/"
        with patch.object(agent_main, "create_backend", AsyncMock(return_value=backend)), \
             patch.object(agent_main, "build_model", return_value=parent), \
             patch.object(profiles, "get_model_for_role", side_effect=lambda role: models[role]):
            graph, _, _ = await agent_main.build_team_agent("verification-team")
            result = await graph.ainvoke({"messages": [{"role": "user", "content": "Verify role routing only."}]},
                                        {"configurable": {"thread_id": "verification-team"}, "recursion_limit": 60})
        tool_results = [str(message.content) for message in result["messages"] if message.type == "tool"]
        output = {"scope": "Actual graph and role profiles; deterministic models; temporary filesystem backend",
                  "role_results": tool_results, "final_reply": result["messages"][-1].content,
                  "reviewer_was_able_to_write": (Path(directory) / "reviewer-write-probe.txt").exists()}
    Path("logs/team-graph-verification.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
