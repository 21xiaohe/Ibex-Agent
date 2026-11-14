"""Resuming an evaluation never retries or removes a completed failed attempt."""
import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from eval import resume_objective as resume


def report() -> dict:
    """Build a partial report ending with a recorded account failure."""
    config = {"repeats": 2, "timeout_seconds": 300, "model": "test"}
    return {"complete": False, "stop_reason": "provider_account_unavailable",
            "config": config, "suite_sha256": resume.runner.suite_hash(),
            "runner_sha256": hashlib.sha256(Path(resume.runner.__file__).read_bytes()).hexdigest(),
            "planned_trials": len(resume.runner.CASES) * 2,
            "samples": [{"id": resume.runner.CASES[0]["id"], "repeat": 1,
                         "passed": False, "elapsed_seconds": 1, "account_blocked": True}]}


def test_resume_preserves_failure_and_only_runs_remaining(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The original source and failed sample survive a successful continuation."""
    original = report()
    source, output = tmp_path / "old.json", tmp_path / "new.json"
    raw = json.dumps(original)
    source.write_text(raw, encoding="utf-8")
    async def trial(case: dict, factory: object, timeout: int) -> dict:
        return {"id": case["id"], "passed": True, "elapsed_seconds": 1}
    mock = AsyncMock(side_effect=trial)
    monkeypatch.setattr(resume.runner, "run_trial", mock)
    assert asyncio.run(resume.continue_report(source, output, original["config"], object())) == 1
    final = json.loads(output.read_text(encoding="utf-8"))
    assert final["complete"] and final["summary"]["passed"] == 11
    assert final["samples"][0] == original["samples"][0]
    assert mock.await_count == 11 and source.read_text(encoding="utf-8") == raw
    with pytest.raises(FileExistsError):
        asyncio.run(resume.continue_report(source, output, original["config"], object()))


@pytest.mark.parametrize("change", ["config", "runner", "prefix", "complete"])
def test_resume_rejects_incomparable_or_reordered_evidence(change: str) -> None:
    """Reject accidental model/scorer changes and sample selection before any calls."""
    data = report()
    config = dict(data["config"])
    if change == "config": config["model"] = "different"
    if change == "runner": data["runner_sha256"] = "changed"
    if change == "prefix": data["samples"][0]["repeat"] = 2
    if change == "complete": data["complete"] = True
    with pytest.raises(ValueError):
        resume.continuation_plan(data, config)


def test_resume_stops_on_another_account_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A new account fault stops after one additional attempt, retaining both failures."""
    data = report()
    source, output = tmp_path / "old.json", tmp_path / "new.json"
    source.write_text(json.dumps(data), encoding="utf-8")
    mock = AsyncMock(return_value={"id": resume.runner.CASES[1]["id"], "passed": False,
                                  "elapsed_seconds": 1, "account_blocked": True})
    monkeypatch.setattr(resume.runner, "run_trial", mock)
    assert asyncio.run(resume.continue_report(source, output, data["config"], object())) == 2
    final = json.loads(output.read_text(encoding="utf-8"))
    assert not final["complete"] and final["stop_reason"] == "provider_account_unavailable"
    assert len(final["samples"]) == 2 and mock.await_count == 1
