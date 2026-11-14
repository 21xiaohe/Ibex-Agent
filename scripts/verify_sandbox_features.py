"""Check real Docker isolation, pool lifecycle and reviewer file permissions."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys

from sandbox.docker_manager import create_one_sandbox, create_readonly_sandbox, destroy_sandbox, seed_project
from sandbox.pool import SandboxPool


async def main() -> None:
    """Write observed results, including failures, without contacting outside services."""
    results = {}
    sandbox = await create_one_sandbox()
    reviewer = None
    try:
        await seed_project(sandbox)
        config = json.loads(subprocess.check_output(["docker", "inspect", sandbox.id]))[0]
        host = config["HostConfig"]
        results["isolation"] = {
            "user": config["Config"]["User"], "network": host["NetworkMode"],
            "read_only_root": host["ReadonlyRootfs"], "cap_drop": host["CapDrop"],
            "memory_bytes": host["Memory"], "pids_limit": host["PidsLimit"],
            "model_key_env_present": any("API_KEY=" in entry for entry in config["Config"]["Env"]),
        }
        check = await sandbox.aexecute("id -u; touch /etc/ibex-verification-probe", timeout=10)
        results["root_write_rejected"] = check.exit_code != 0
        await sandbox.aupload_files([("/home/agent/review.txt", b"review-me")])
        reviewer = await create_readonly_sandbox(sandbox)
        read = await reviewer.adownload_files(["/home/agent/review.txt"])
        results["reviewer_snapshot_readable"] = read[0].content == b"review-me"
        attempts = {}
        for name, command in {
            "overwrite": "echo hacked > /home/agent/review.txt",
            "create": "touch /home/agent/new.txt",
            "chmod_bypass": "chmod u+w /home/agent/review.txt && echo hacked > /home/agent/review.txt",
            "delete": "rm /home/agent/review.txt",
        }.items():
            response = await reviewer.aexecute(command, timeout=10)
            attempts[name] = response.exit_code != 0
        results["reviewer_denials"] = attempts
    finally:
        if reviewer is not None:
            await destroy_sandbox(reviewer)
        await destroy_sandbox(sandbox)
    pool = SandboxPool(2)
    active = peak = 0
    identifiers = []
    async def task(index: int) -> None:
        nonlocal active, peak
        async with pool.acquire() as (backend, workdir):
            active += 1
            peak = max(peak, active)
            identifiers.append(backend.id)
            assert (await backend.aexecute("test ! -f /home/agent/task-marker", timeout=10)).exit_code == 0
            await backend.aupload_files([(f"{workdir}/task-marker", str(index).encode())])
            await asyncio.sleep(.2)
            active -= 1
    await asyncio.gather(*(task(index) for index in range(3)))
    remaining = subprocess.check_output(["docker", "ps", "-a", "--format", "{{.Names}}"], text=True).splitlines()
    results["pool"] = {"limit": 2, "peak": peak, "unique_instances": len(set(identifiers)),
                       "all_removed": not any(name in remaining for name in identifiers)}
    Path("logs/sandbox-features.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
