from __future__ import annotations

import httpx


class PolicyUnavailable(Exception):
    pass


class ControlPlaneUnavailable(Exception):
    pass


class OpaClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.allow_url = self.base_url + "/v1/data/governance/authz/allow"

    def allow(self, agent_id: str, action: str) -> bool:
        try:
            response = httpx.post(
                self.allow_url,
                json={"input": {"agent_id": agent_id, "action": action}},
                timeout=2.0,
            )
        except httpx.HTTPError as exc:
            raise PolicyUnavailable(str(exc)) from exc
        if response.status_code != 200:
            raise PolicyUnavailable(f"opa status {response.status_code}")
        body = response.json()
        if "result" not in body:
            raise PolicyUnavailable("opa response missing result")
        return bool(body["result"])
