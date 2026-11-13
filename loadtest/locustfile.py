"""HTTP load scenarios; model calls require an explicit IBEX_LOAD_AGENT=1 opt-in."""
import os
from uuid import uuid4
from locust import HttpUser, between, task


class IbexUser(HttpUser):
    """Probe readiness and optionally submit real sandbox tasks at a bounded pace."""
    wait_time = between(2, 5)

    @task
    def readiness(self) -> None:
        """Exercise the real dependency readiness endpoint."""
        self.client.get("/readyz")

    @task
    def issue(self) -> None:
        """Call the authenticated API only when the operator opts into model costs."""
        if os.environ.get("IBEX_LOAD_AGENT") != "1":
            return
        self.client.post("/issues/secure", json={"issue": "Reply LOAD_OK; do not use tools.", "thread_id": uuid4().hex},
                         headers={"X-API-Key": os.environ.get("IBEX_LOAD_API_KEY", "")}, name="/issues/secure")
