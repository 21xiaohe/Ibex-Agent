"""Exercise real MCP discovery, invocation and per-server failure isolation."""
import asyncio
from pathlib import Path
import sys
import pytest
from tools.mcp import MCPManager


def test_real_local_mcp_transport() -> None:
    """An unreachable server does not prevent calling a functioning stdio tool."""
    async def run() -> None:
        manager = MCPManager({
            "local": {"transport": "stdio", "command": sys.executable, "args": [str(Path(__file__).with_name("mcp_echo_server.py"))]},
            "unreachable": {"transport": "streamable_http", "url": "http://127.0.0.1:1/mcp"},
        })
        tools = await manager.get_tools()
        echo = next(tool for tool in tools if tool.name == "verification_echo")
        result = await echo.ainvoke({"text": "IBEX_MCP_OK"})
        assert "IBEX_MCP_OK" in str(result)
    factory = asyncio.ProactorEventLoop if sys.platform == "win32" else asyncio.new_event_loop
    with asyncio.Runner(loop_factory=factory) as runner:
        runner.run(run())


def test_mcp_through_service_builder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The actual service graph invokes stdio MCP even on Windows' database-compatible loop."""
    from infra.settings import get_settings
    from agent import main
    from langchain.messages import AIMessage
    from tests_service.test_agent_mechanisms import ScriptedModel
    monkeypatch.setattr(get_settings(), "sandbox_provider", "none")
    monkeypatch.setattr(main, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(get_settings(), "mcp_servers", {"local": {"transport": "stdio", "command": sys.executable, "args": [str(Path(__file__).with_name("mcp_echo_server.py"))]}})
    model = ScriptedModel(responses=[AIMessage(content="", tool_calls=[{"name": "verification_echo", "args": {"text": "SERVICE_MCP_OK"}, "id": "m"}]), AIMessage(content="done")])
    monkeypatch.setattr(main, "build_model", lambda: model)
    async def run() -> None:
        graph, sandbox = await main.build_agent_for_service()
        result = await graph.ainvoke({"messages": [{"role": "user", "content": "call echo"}]})
        assert sandbox is not None
        from sandbox.lifecycle import release_backend
        await release_backend(sandbox)
        assert any("SERVICE_MCP_OK" in str(msg.content) for msg in result["messages"] if msg.type == "tool")
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else asyncio.new_event_loop
    with asyncio.Runner(loop_factory=factory) as runner:
        runner.run(run())
