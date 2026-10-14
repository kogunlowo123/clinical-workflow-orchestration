"""Load profile for a deployed orchestrator.

Run against a staging deployment backed by a synthetic-data FHIR server,
never production:

    CWO_LOAD_USER_TOKEN=<care-coordinator access token> \
    CWO_LOAD_AGENT_TOKEN=<agent credential> \
    CWO_LOAD_ENCOUNTER_ID=<synthetic encounter id> \
    locust -f tests/performance/locustfile.py --host https://cwo.staging.internal \
           --users 50 --spawn-rate 5 --run-time 15m --headless

Care teams read worklists far more often than they start workflows, and
agents poll task status, so the mix weights reads over starts.
"""

from __future__ import annotations

import os
import uuid

from locust import HttpUser, between, task

USER_TOKEN = os.environ.get("CWO_LOAD_USER_TOKEN", "")
AGENT_TOKEN = os.environ.get("CWO_LOAD_AGENT_TOKEN", "")
ENCOUNTER = os.environ.get("CWO_LOAD_ENCOUNTER_ID", "")


class CareTeam(HttpUser):
    wait_time = between(0.5, 2.0)

    @task(10)
    def worklist(self) -> None:
        self.client.get(
            "/v1/workflows?state=awaiting_approval&limit=50",
            headers={"Authorization": f"Bearer {USER_TOKEN}"},
            name="/v1/workflows",
        )

    @task(1)
    def start(self) -> None:
        self.client.post(
            "/v1/workflows",
            json={"encounter_id": ENCOUNTER},
            headers={
                "Authorization": f"Bearer {USER_TOKEN}",
                "Idempotency-Key": f"load-{uuid.uuid4()}",
            },
            name="/v1/workflows [start]",
        )


class PeerAgent(HttpUser):
    wait_time = between(0.2, 1.0)

    @task
    def pending(self) -> None:
        self.client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "message/send",
                "params": {
                    "message": {
                        "role": "user",
                        "messageId": str(uuid.uuid4()),
                        "parts": [
                            {
                                "kind": "data",
                                "data": {
                                    "skill": "list-pending-approvals",
                                    "purpose_of_use": "HOPERAT",
                                },
                            }
                        ],
                    }
                },
            },
            headers={"Authorization": f"Bearer {AGENT_TOKEN}"},
            name="/a2a list-pending-approvals",
        )
