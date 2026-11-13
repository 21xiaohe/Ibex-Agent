"""Run a real model coding task and independently check its sandbox artifact."""
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

from agent.main import build_agent_for_service
from infra.persistence import open_persistence
from sandbox.lifecycle import release_backend as destroy_sandbox

PROMPT = """Complete this small task yourself, without delegation, web search or Git operations.
Create /home/agent/app/pricing.py with a public function
apply_discount(amount: Decimal, rate: Decimal) -> Decimal.
Reject negative amount or rate outside [0, 1] with ValueError.
Return amount * (1 - rate), rounded to two decimal places using ROUND_HALF_UP.
Use Decimal only. Include annotations and docstring. Add pytest coverage in
/home/agent/tests/test_pricing.py and run all tests under /home/agent/tests.
Read the existing project before changing it. Report the actual test result.
"""

CHECKS = '''from decimal import Decimal
import pytest
from app.pricing import apply_discount

@pytest.mark.parametrize("amount,rate,expected", [
    ("100", ".15", "85.00"), ("10.05", ".5", "5.03"),
    ("0", ".8", "0.00"), ("12.34", "0", "12.34"),
    ("12.34", "1", "0.00"), ("999999.99", ".3333", "666699.99"),
])
def test_result(amount, rate, expected):
    actual = apply_discount(Decimal(amount), Decimal(rate))
    assert isinstance(actual, Decimal)
    assert actual == Decimal(expected)
    assert actual.as_tuple().exponent == -2

@pytest.mark.parametrize("amount,rate", [("-1", "0"), ("10", "-.01"), ("10", "1.01")])
def test_invalid(amount, rate):
    with pytest.raises(ValueError):
        apply_discount(Decimal(amount), Decimal(rate))
'''


async def main() -> None:
    """Save code, model transcript and independently executed pytest results locally."""
    output = Path("logs/coding-smoke-2")
    output.mkdir(parents=True, exist_ok=True)
    pool, saver, store = await open_persistence()
    sandbox = None
    try:
        agent, sandbox = await build_agent_for_service(saver, store, user_id="local-smoke")
        baseline = await sandbox.aexecute("python -m pytest -q /home/agent/tests", timeout=60)
        print("BASELINE", baseline.exit_code, baseline.output, flush=True)
        assert baseline.exit_code == 0
        result = await asyncio.wait_for(agent.ainvoke(
            {"messages": [{"role": "user", "content": PROMPT}]},
            config={"configurable": {"thread_id": f"coding-smoke-{uuid4().hex}"}, "recursion_limit": 60},
        ), timeout=300)
        transcript = [{"type": msg.type, "content": msg.content,
                       "tool_calls": getattr(msg, "tool_calls", [])} for msg in result["messages"]]
        (output / "transcript.json").write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
        files = await sandbox.adownload_files(["/home/agent/app/pricing.py", "/home/agent/tests/test_pricing.py"])
        for item in files:
            if item.error or item.content is None:
                raise RuntimeError(f"Missing coding artifact: {item.path}")
            (output / Path(item.path).name).write_bytes(item.content)
        uploaded = await sandbox.aupload_files([("/home/agent/acceptance/test_independent.py", CHECKS.encode())])
        assert not uploaded[0].error
        checked = await sandbox.aexecute("python -m pytest -q /home/agent/tests /home/agent/acceptance", timeout=60)
        report = {"baseline_exit_code": baseline.exit_code, "baseline_output": baseline.output,
                  "verification_exit_code": checked.exit_code, "verification_output": checked.output,
                  "reply": result["messages"][-1].content,
                  "input_tokens": result.get("total_input_tokens"), "output_tokens": result.get("total_output_tokens"),
                  "sandbox": sandbox.id}
        (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("VERIFICATION", checked.exit_code, checked.output, flush=True)
        assert checked.exit_code == 0
    finally:
        if sandbox is not None:
            await destroy_sandbox(sandbox)
        await pool.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
