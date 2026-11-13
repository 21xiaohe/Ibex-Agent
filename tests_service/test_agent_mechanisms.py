"""Exercise actual agent middleware with deterministic models and harmless tools."""
import asyncio
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain.messages import AIMessage
from langchain.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend


class ScriptedModel(FakeMessagesListChatModel):
    """A deterministic model supporting the tool binding used by the real graph."""
    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Keep the predefined calls unchanged."""
        return self


class FailingModel(ScriptedModel):
    """Raise a controlled upstream error without making a network request."""
    def _generate(self, *args: Any, **kwargs: Any) -> Any:
        """Simulate a provider failure."""
        raise RuntimeError("injected provider outage")


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_hitl_pauses_and_resumes(decision: str) -> None:
    """A tool is not executed before approval, and rejection prevents execution."""
    calls = []
    @tool
    def record_test_action(note: str) -> str:
        """Record a harmless in-memory verification action."""
        calls.append(note)
        return "recorded"
    model = ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "record_test_action", "args": {"note": "test"}, "id": "one"}]),
        AIMessage(content="done"),
    ])
    graph = create_deep_agent(model=model, tools=[record_test_action], checkpointer=InMemorySaver(), interrupt_on={"record_test_action": True})
    async def run() -> None:
        config = {"configurable": {"thread_id": decision}}
        paused = await graph.ainvoke({"messages": [{"role": "user", "content": "test"}]}, config)
        assert paused.get("__interrupt__") and calls == []
        await graph.ainvoke(Command(resume={"decisions": [{"type": decision}]}), config)
        assert calls == (["test"] if decision == "approve" else [])
    asyncio.run(run())


def test_fallback_reaches_second_backup(monkeypatch: pytest.MonkeyPatch) -> None:
    """The configured fallback middleware recovers after two model failures."""
    from infra import fallback
    from langchain.agents import create_agent
    failed = FailingModel(responses=[AIMessage(content="unused")])
    recovered = ScriptedModel(responses=[AIMessage(content="fallback-ok")])
    monkeypatch.setattr(fallback, "get_model", lambda tier: failed if tier == "standard" else recovered)
    from obs.metrics import AGENT_FALLBACK
    before = AGENT_FALLBACK.labels("standard", "cheap")._value.get()
    graph = create_agent(model=failed, middleware=[fallback.build_fallback_middleware()])
    result = asyncio.run(graph.ainvoke({"messages": [{"role": "user", "content": "test"}]}))
    assert result["messages"][-1].content == "fallback-ok"
    assert AGENT_FALLBACK.labels("standard", "cheap")._value.get() == before + 1


def test_reviewer_filesystem_permission_blocks_write(tmp_path: Path) -> None:
    """A forced model tool call cannot write through the filesystem permission layer."""
    from subagents.reviewer import REVIEWER_PERMISSIONS
    graph = create_deep_agent(
        model=ScriptedModel(responses=[
            AIMessage(content="", tool_calls=[{"name": "write_file", "args": {"file_path": "/blocked.txt", "content": "forbidden"}, "id": "write"}]),
            AIMessage(content="done"),
        ]),
        backend=FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True),
        permissions=REVIEWER_PERMISSIONS,
    )
    result = asyncio.run(graph.ainvoke({"messages": [{"role": "user", "content": "test"}]}))
    assert not (tmp_path / "blocked.txt").exists()
    tool_results = [message for message in result["messages"] if message.type == "tool"]
    assert tool_results and any("denied" in str(message.content).lower() for message in tool_results)


def test_readonly_initialization_failure_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """Snapshot import failures cannot leave a reviewer container behind."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from sandbox import docker_manager
    monkeypatch.setattr(docker_manager, "_run", AsyncMock(return_value=(0, "created")))
    monkeypatch.setattr(docker_manager.subprocess, "run", Mock(side_effect=RuntimeError("export failed")))
    cleanup = AsyncMock()
    monkeypatch.setattr(docker_manager, "destroy_sandbox", cleanup)
    with pytest.raises(RuntimeError, match="export failed"):
        asyncio.run(docker_manager.create_readonly_sandbox(SimpleNamespace(id="source", workdir="/home/agent")))
    cleanup.assert_awaited_once()


def test_local_worker_close_without_unix_signal() -> None:
    """An idle local worker closes without SIGUSR1 on Windows."""
    from scripts.run_local import LocalWorker
    async def harmless(ctx: dict) -> None:
        """Provide a registered function without performing work."""
    async def run() -> None:
        worker = LocalWorker(functions=[harmless], handle_signals=False)
        await worker.close()
    asyncio.run(run())


@pytest.mark.parametrize("existing", [False, True])
def test_daytona_lookup_and_creation_contract(monkeypatch: pytest.MonkeyPatch, existing: bool) -> None:
    """Use the current SDK iterator and put deletion policy on creation parameters."""
    from types import SimpleNamespace
    from unittest.mock import Mock
    import daytona
    import langchain_daytona
    from sandbox import manager
    sandbox = SimpleNamespace(get_workspace_root_dir=lambda: "/home/daytona")
    client = SimpleNamespace(list=Mock(return_value=iter([sandbox] if existing else [])), create=Mock(return_value=sandbox))
    monkeypatch.setattr(daytona, "Daytona", Mock(return_value=client))
    monkeypatch.setattr(langchain_daytona, "DaytonaSandbox", Mock(return_value=object()))
    monkeypatch.setattr(manager, "seed_project_into_sandbox", Mock(return_value="/home/daytona"))
    prepare = Mock()
    monkeypatch.setattr(manager, "prepare_dependencies", prepare)
    _, actual, _, _ = manager.get_or_create_sandbox_backend("verification-thread")
    assert actual is sandbox
    assert client.list.call_args.args[0].labels == {"thread_id": "verification-thread"}
    if existing:
        client.create.assert_not_called()
    else:
        assert client.create.call_args.args[0].auto_delete_interval == 60
        prepare.assert_called_once()


def test_eval_gate_returns_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A degraded score must produce a nonzero result usable by CI."""
    import json
    from unittest.mock import AsyncMock
    from eval import run_eval
    monkeypatch.chdir(tmp_path)
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"categories": {"exec": 1.0}}), encoding="utf-8")
    monkeypatch.setattr(run_eval, "BASELINE_FILE", str(baseline))
    monkeypatch.setattr(run_eval, "CODE_EVAL_CASES", [{}])
    monkeypatch.setattr(run_eval, "EVAL_CASES", [])
    monkeypatch.setattr(run_eval, "_eval_repeated", AsyncMock(return_value={"id": "test", "category": "exec", "mean": 0.0, "std": 0.0, "scores": [0.0]}))
    assert asyncio.run(run_eval.main()) == 1
