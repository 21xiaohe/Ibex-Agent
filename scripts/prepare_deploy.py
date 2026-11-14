"""Generate local deployment credentials and Prometheus secret files without printing them."""
from pathlib import Path
import secrets
import json
from dotenv import dotenv_values, set_key


def prepare(root: Path) -> None:
    """Create missing deployment secrets while preserving existing configured values."""
    envfile = root / ".env"
    values = dotenv_values(envfile)
    for name in ("IBEX_PG_PASSWORD", "IBEX_SANDBOX_API_TOKEN", "IBEX_METRICS_TOKEN", "IBEX_GRAFANA_PASSWORD"):
        if not values.get(name):
            values[name] = secrets.token_urlsafe(32)
            set_key(str(envfile), name, values[name])
    if not json.loads(values.get("IBEX_API_KEYS") or "{}"):
        set_key(str(envfile), "IBEX_API_KEYS", json.dumps({secrets.token_urlsafe(32): "local-owner"}))
    directory = root / "deploy" / "secrets"
    directory.mkdir(parents=True, exist_ok=True)
    for filename, key in (("sandbox_token", "IBEX_SANDBOX_API_TOKEN"), ("web_metrics_token", "IBEX_METRICS_TOKEN")):
        (directory / filename).write_text(values[key], encoding="utf-8")
    print("Deployment secret files are ready; credential values are not displayed.")


if __name__ == "__main__":
    prepare(Path(__file__).resolve().parent.parent)
