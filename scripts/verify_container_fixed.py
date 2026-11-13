"""Exercise a containerized API and an authenticated Docker broker, without channel sends."""
import json
from pathlib import Path
import secrets
import subprocess
import tempfile
import time

import httpx
from dotenv import dotenv_values


def docker(*args: str) -> str:
    """Run Docker without printing environment configuration or credentials."""
    result = subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8", timeout=90)
    if result.returncode:
        raise RuntimeError("Docker command failed: " + result.stderr[:400])
    return result.stdout.strip()


def main() -> None:
    """Verify actual remote execution and model coding, then remove owned containers."""
    tag = secrets.token_hex(5)
    broker, web = "ibex-broker-" + tag, "ibex-web-" + tag
    token = secrets.token_urlsafe(32)
    report: dict = {}
    started = []
    with tempfile.TemporaryDirectory(prefix="ibex-env-") as directory:
        env = {k: v for k, v in dotenv_values(".env").items() if v is not None}
        env.update(IBEX_DATABASE_URL="postgresql://ibex:ibex@host.docker.internal:15432/ibex",
                   IBEX_REDIS_URL="redis://host.docker.internal:16379/0", IBEX_SANDBOX_PROVIDER="docker",
                   IBEX_SANDBOX_API_URL="http://host.docker.internal:18703", IBEX_SANDBOX_API_TOKEN=token)
        config = Path(directory) / "web.env"
        config.write_text("\n".join(f"{k}={v}" for k, v in env.items()), encoding="utf-8")
        broker_config = Path(directory) / "broker.env"
        broker_config.write_text(f"IBEX_SANDBOX_API_TOKEN={token}\nIBEX_SANDBOX_IMAGE=ibex-sandbox:local\nIBEX_API_KEY=broker-does-not-call-models\n", encoding="utf-8")
        try:
            docker("run", "-d", "--name", broker, "--env-file", str(broker_config), "-p", "127.0.0.1:18703:8003",
                   "-v", "/var/run/docker.sock:/var/run/docker.sock", "ibex-app:fixed", "uvicorn", "sandbox.service:app", "--host", "0.0.0.0", "--port", "8003")
            started.append(broker)
            docker("run", "-d", "--name", web, "--env-file", str(config), "-p", "127.0.0.1:18702:8000",
                   "ibex-app:fixed", "uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000")
            started.append(web)
            with httpx.Client(timeout=240, trust_env=False) as client:
                for _ in range(60):
                    try:
                        if client.get("http://127.0.0.1:18702/readyz", timeout=4).status_code == 200 and client.post("http://127.0.0.1:18703/sandboxes", timeout=4).status_code == 401:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(1)
                else:
                    raise RuntimeError("Container readiness failed")
                report["ready"] = True
                report["unauthenticated_broker_status"] = client.post("http://127.0.0.1:18703/sandboxes").status_code
                mounts = json.loads(docker("inspect", "--format", "{{json .Mounts}}", web))
                report["web_has_docker_socket"] = any("docker.sock" in item["Destination"] for item in mounts)
                response = client.post("http://127.0.0.1:18702/issues", json={"issue": "Do not delegate. Use execute to run python -c 'print(17*19)' in the sandbox. Report the actual numeric output. Do not guess.", "thread_id": tag})
                report["issue_status"] = response.status_code
                report["issue_reply"] = response.json()
                assert response.status_code == 200 and "323" in response.text
                # Run independent artifact acceptance inside the app container using the same broker.
                result = subprocess.run(["docker", "exec", web, "python", "-m", "scripts.smoke_local"], capture_output=True, timeout=360)
                Path("logs/container-coding-fixed.txt").write_bytes(result.stdout + result.stderr)
                report["coding_smoke_exit_code"] = result.returncode
                assert result.returncode == 0
                report["coding_result"] = json.loads(docker("exec", web, "cat", "logs/coding-smoke-2/result.json"))
                assert report["unauthenticated_broker_status"] == 401 and not report["web_has_docker_socket"]
        finally:
            for name in reversed(started):
                docker("stop", "-t", "10", name)
                docker("rm", name)
            Path("logs/container-fixed.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
