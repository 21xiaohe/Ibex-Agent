"""Verify the complete isolated Compose deployment without sending external messages."""
import json
from pathlib import Path
import subprocess
import time
from uuid import uuid4
import httpx
from dotenv import dotenv_values


def docker(*args: str, timeout: int = 120) -> str:
    """Run a bounded command against only the named verification deployment."""
    result = subprocess.run(["docker", *args], capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("Docker verification command failed")
    return result.stdout.decode("utf-8", errors="replace").strip()


def main() -> None:
    """Check auth, workspace continuity, worker results, restart recovery and monitoring."""
    config = dotenv_values(".env")
    keys = list(json.loads(config["IBEX_API_KEYS"]))
    if len(keys) < 2:
        raise ValueError("Verification needs two configured tenant keys")
    base = "http://127.0.0.1:" + config.get("IBEX_HTTP_PORT", "18704")
    prometheus = "http://127.0.0.1:" + config.get("IBEX_PROMETHEUS_PORT", "18705")
    grafana = "http://127.0.0.1:" + config.get("IBEX_GRAFANA_PORT", "18706")
    report = {}
    conversation, marker = "persist-" + uuid4().hex, "PERSIST_" + uuid4().hex
    def save() -> None:
        Path("logs/completion-live.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with httpx.Client(trust_env=False, timeout=180) as client:
        def ready() -> None:
            for _ in range(60):
                try:
                    if client.get(base + "/readyz", timeout=4).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(1)
            raise RuntimeError("Deployment not ready")
        ready()
        report["unauthenticated"] = {path: client.post(base + path, json={"issue": "test", "text": "test"}).status_code for path in ("/issues", "/jobs", "/tasks", "/chat/stream", "/webhook/generic")}
        assert all(value == 401 for value in report["unauthenticated"].values())
        headers = {"X-API-Key": keys[0]}
        def issue(text: str, key: str = keys[0]) -> dict:
            response = client.post(base + "/issues", headers={"X-API-Key": key}, json={"issue": text, "thread_id": conversation})
            response.raise_for_status()
            return response.json()
        report["write"] = issue(f"Use write_file to create /home/agent/app/session_marker.txt containing exactly {marker}. Do not delegate or search. Reply SAVED only after the write succeeds.")
        save()
        docker("compose", "-p", "ibex-completion", "-f", "docker-compose.prod.yml", "restart", "web")
        ready()
        report["read_after_web_restart"] = issue("Read /home/agent/app/session_marker.txt using the read_file tool and return its exact content. Do not infer it from conversation history.")
        assert marker in report["read_after_web_restart"]["reply"]
        report["other_tenant"] = issue("Read /home/agent/app/session_marker.txt with a tool. If it does not exist return MISSING. Do not create it.", keys[1])
        assert "MISSING" in report["other_tenant"]["reply"] and marker not in report["other_tenant"]["reply"]
        response = client.post(base + "/tasks", headers=headers, json={"text": "Reply exactly QUEUE_COMPLETE_OK. Do not use tools.", "conversation_id": "queue-" + uuid4().hex})
        response.raise_for_status()
        task_id = response.json()["task_id"]
        for _ in range(90):
            row = client.get(base + "/jobs/" + task_id, headers=headers).json()
            if row.get("status") in {"done", "error"}:
                break
            time.sleep(1)
        report["queue_result"] = row
        assert row["status"] == "done" and "QUEUE_COMPLETE_OK" in row["result"]["reply"]
        report["cross_tenant_result_status"] = client.get(base + "/tasks/" + task_id, headers={"X-API-Key": keys[1]}).status_code
        assert report["cross_tenant_result_status"] == 404
        save()
        # All verification tasks have finished before creating a deliberate orphan.
        created = docker("exec", "ibex-completion-sandbox-1", "python", "-c",
            "import os,httpx; r=httpx.post('http://127.0.0.1:8003/sandboxes',headers={'Authorization':'Bearer '+os.environ['IBEX_SANDBOX_API_TOKEN']},timeout=30);r.raise_for_status();print(r.json()['id'])")
        owned = docker("ps", "-q", "--filter", "label=ibex.owner=ibex-completion")
        assert created and owned
        # Kill only the newly created test broker to simulate an ungraceful crash.
        docker("kill", "ibex-completion-sandbox-1")
        docker("start", "ibex-completion-sandbox-1")
        for _ in range(30):
            if not docker("ps", "-aq", "--filter", "label=ibex.owner=ibex-completion"):
                break
            time.sleep(1)
        report["orphan_reclaimed_after_broker_crash"] = not docker("ps", "-aq", "--filter", "label=ibex.owner=ibex-completion")
        assert report["orphan_reclaimed_after_broker_crash"]
        for _ in range(30):
            targets = client.get(prometheus + "/api/v1/targets").json()["data"]["activeTargets"]
            if len(targets) == 3 and all(target["health"] == "up" for target in targets):
                break
            time.sleep(2)
        report["prometheus_targets"] = {item["labels"]["job"]: item["health"] for item in targets}
        assert len(targets) == 3 and all(value == "up" for value in report["prometheus_targets"].values())
        dashboard = client.get(grafana + "/api/dashboards/uid/ibex-overview", auth=("admin", config["IBEX_GRAFANA_PASSWORD"]))
        report["grafana_dashboard_status"] = dashboard.status_code
        assert dashboard.status_code == 200
        report["grafana_panels"] = len(dashboard.json()["dashboard"]["panels"])
        save()
    print(json.dumps({key: value for key, value in report.items() if key not in {"write", "read_after_web_restart", "other_tenant", "queue_result"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
