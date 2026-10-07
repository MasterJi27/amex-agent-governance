from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any

import httpx
import psycopg
import redis
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from gateway.db import Database
from gateway.opa_client import ControlPlaneUnavailable, OpaClient
from gateway.pipeline import Gateway
from gateway.redis_store import RedisStore
from gateway.simulators import Simulators

_gateway: Gateway | None = None
mcp = MCPServer("Governance Gateway")


def get_gateway() -> Gateway:
    if _gateway is None:
        raise RuntimeError("gateway is not ready")
    return _gateway


def bind_gateway(gateway: Gateway) -> None:
    global _gateway
    _gateway = gateway


async def _tool(
    agent_id: str,
    action: str,
    amount_cents: int,
    idempotency_key: str,
    resource: dict[str, Any],
) -> str:
    result = await get_gateway().execute(agent_id, action, amount_cents, idempotency_key, resource)
    return json.dumps(result)


@mcp.tool(structured_output=False)
async def rebook_flight(
    agent_id: str,
    idempotency_key: str,
    fare_delta_cents: int,
    itinerary_id: str,
    flight_numbers: str,
) -> str:
    """Book a replacement itinerary. The airline credential never leaves this gateway."""
    return await _tool(
        agent_id,
        "rebook_flight",
        fare_delta_cents,
        idempotency_key,
        {"itinerary_id": itinerary_id, "flight_numbers": flight_numbers},
    )


@mcp.tool(structured_output=False)
async def change_hotel(agent_id: str, idempotency_key: str, amount_cents: int, nights: int) -> str:
    """Extend the hotel stay. The hotel credential never leaves this gateway."""
    return await _tool(
        agent_id,
        "change_hotel",
        amount_cents,
        idempotency_key,
        {"nights": nights},
    )


@mcp.tool(structured_output=False)
async def send_notification(agent_id: str, idempotency_key: str, message: str) -> str:
    """Record an in-app notification. Amount is always zero and still passes the allowlist."""
    return await _tool(
        agent_id,
        "send_notification",
        0,
        idempotency_key,
        {"message": message},
    )


def _json(payload: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status)


@mcp.custom_route("/v1/health", methods=["GET"])
async def health(_: Request) -> Response:
    return _json({"ok": True})


@mcp.custom_route("/v1/budget", methods=["GET"])
async def budget(_: Request) -> Response:
    return _json(await get_gateway().budget_view())


@mcp.custom_route("/v1/budget/{node_id}/cap", methods=["PUT"])
async def set_cap(request: Request) -> Response:
    body = await request.json()
    node_id = request.path_params["node_id"]
    cap = int(body["cap_cents"])
    await get_gateway().set_cap(node_id, cap)
    return _json(await get_gateway().budget_view())


@mcp.custom_route("/v1/agents/{agent_id}/revoke", methods=["POST"])
async def revoke(request: Request) -> Response:
    generation = await get_gateway().revoke(request.path_params["agent_id"])
    return _json({"generation": generation})


@mcp.custom_route("/v1/agents/{agent_id}/restore", methods=["POST"])
async def restore(request: Request) -> Response:
    generation = await get_gateway().restore_agent(request.path_params["agent_id"])
    return _json({"generation": generation})


@mcp.custom_route("/v1/teams/{team_id}/stop", methods=["POST"])
async def stop_team(request: Request) -> Response:
    await get_gateway().stop_team(request.path_params["team_id"])
    return _json(await get_gateway().budget_view())


@mcp.custom_route("/v1/budget/preview", methods=["POST"])
async def preview_cap(request: Request) -> Response:
    body = await request.json()
    result = await get_gateway().preview_cap(
        str(body.get("agent_id", "travel-concierge")),
        str(body["node_id"]),
        int(body["cap_cents"]),
        int(body.get("amount_cents", 18_000)),
    )
    return _json(result)


@mcp.custom_route("/v1/fleet/emergency-stop", methods=["POST"])
async def emergency_stop(_: Request) -> Response:
    generation = await get_gateway().emergency_stop()
    return _json({"fleet_generation": generation, "stopped": True})


@mcp.custom_route("/v1/fleet/resume", methods=["POST"])
async def resume(_: Request) -> Response:
    generation = await get_gateway().resume()
    return _json({"fleet_generation": generation, "stopped": False})


@mcp.custom_route("/v1/policy", methods=["GET"])
async def policy(_: Request) -> Response:
    gateway = get_gateway()
    agents = [str(agent["id"]) for agent in gateway.db.agents()]
    actions = ["rebook_flight", "change_hotel", "send_notification", "wire_funds"]
    checks = []
    for agent_id in agents:
        for action in actions:
            try:
                allowed = gateway.opa.allow(agent_id, action)
            except Exception:
                allowed = None
            checks.append({"agent_id": agent_id, "action": action, "allowed": allowed})
    return _json({"policy_version": gateway.policy_version, "checks": checks})


@mcp.custom_route("/v1/audit", methods=["GET"])
async def audit(request: Request) -> Response:
    limit = int(request.query_params.get("limit", "50"))
    return _json({"events": get_gateway().audit_tail(limit)})


@mcp.custom_route("/v1/audit/verify", methods=["GET"])
async def verify(_: Request) -> Response:
    return _json(get_gateway().verify())


@mcp.custom_route("/v1/ledger", methods=["GET"])
async def ledger(request: Request) -> Response:
    """Money ledger: holds vs committed vs released, newest first."""
    limit = max(1, min(int(request.query_params.get("limit", "50")), 200))
    return _json({"reservations": get_gateway().db.list_reservations(limit)})


V2_EXAMPLE_ALLOW = {
    ("travel-concierge", "rebook_flight"): False,
    ("travel-concierge", "change_hotel"): False,
    ("travel-concierge", "send_notification"): True,
    ("travel-concierge", "wire_funds"): False,
    ("claims-assistant", "rebook_flight"): True,
    ("claims-assistant", "change_hotel"): True,
    ("claims-assistant", "send_notification"): True,
    ("claims-assistant", "wire_funds"): False,
    ("refund-agent", "rebook_flight"): False,
    ("refund-agent", "send_notification"): True,
    ("refund-agent", "wire_funds"): False,
}


@mcp.custom_route("/v1/policy/diff", methods=["GET"])
async def policy_diff(_: Request) -> Response:
    """Policy review without restart: live OPA vs the rego-2 example bundle
    (policies/authz.v2.rego.example, not loaded). The one cell that flips is
    travel-concierge + rebook_flight: allow -> deny."""
    gateway = get_gateway()
    cells = [
        ("travel-concierge", "rebook_flight"),
        ("travel-concierge", "wire_funds"),
        ("claims-assistant", "rebook_flight"),
    ]
    live = []
    for agent_id, action in cells:
        try:
            allowed = gateway.opa.allow(agent_id, action)
        except Exception:
            allowed = None
        live.append({"agent_id": agent_id, "action": action, "allowed": allowed})
    v2 = [
        {"agent_id": agent_id, "action": action, "allowed": V2_EXAMPLE_ALLOW[(agent_id, action)]}
        for agent_id, action in cells
    ]
    return _json(
        {
            "live_version": gateway.policy_version,
            "example_version": "rego-2 (example, not loaded)",
            "live": live,
            "v2_example": v2,
        }
    )


@mcp.custom_route("/v1/overrides", methods=["GET"])
async def list_overrides(request: Request) -> Response:
    status = str(request.query_params.get("status", "pending"))
    if status not in ("pending", "approved", "rejected", "failed"):
        return _json({"error": f"unknown status {status}"}, status=400)
    return _json({"overrides": get_gateway().db.list_overrides(status)})


@mcp.custom_route("/v1/overrides", methods=["POST"])
async def propose_override(request: Request) -> Response:
    body = await request.json()
    result = get_gateway().request_override(
        str(body.get("agent_id", "")),
        str(body.get("action", "")),
        int(body.get("amount_cents", 0)),
        body.get("resource", {}),
        str(body.get("node_id", "")),
        str(body.get("reason", "")),
        str(body.get("requested_by", "")),
    )
    return _json(result, status=200 if result["ok"] else 400)


@mcp.custom_route("/v1/overrides/{override_id}/approve", methods=["POST"])
async def approve_override(request: Request) -> Response:
    body = await request.json()
    result = await get_gateway().approve_override(
        request.path_params["override_id"], str(body.get("approved_by", ""))
    )
    return _json(result, status=200 if result["ok"] else 400)


@mcp.custom_route("/v1/audit/export", methods=["GET"])
async def audit_export(request: Request) -> Response:
    """Compliance download: one JSON object per line (seq + payload).

    Usable straight into Splunk/Loki: curl API/v1/audit/export?limit=500 > audit.jsonl
    """
    from starlette.responses import PlainTextResponse

    limit = max(1, min(int(request.query_params.get("limit", "500")), 5000))
    lines = [
        json.dumps({"seq": row["seq"], "created_at": row["created_at"], "payload": row["payload"]})
        for row in get_gateway().audit_tail(limit)
    ]
    return PlainTextResponse(
        "\n".join(lines),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": "attachment; filename=audit.jsonl"},
    )


@mcp.custom_route("/v1/ready", methods=["GET"])
async def ready(_: Request) -> Response:
    """Kubernetes readiness: postgres + redis + opa all checked, not just the process."""
    gateway = get_gateway()
    checks: dict[str, str] = {}
    try:
        gateway.db.ping()
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"down: {exc}"
    try:
        await gateway.store.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"down: {exc}"
    try:
        response = httpx.get(gateway.opa.base_url + "/health", timeout=2.0)
        checks["opa"] = "ok" if response.status_code == 200 else f"down: status {response.status_code}"
    except Exception as exc:
        checks["opa"] = f"down: {exc}"
    return _json({"ready": all(value == "ok" for value in checks.values()), "checks": checks})


@mcp.custom_route("/v1/metrics", methods=["GET"])
async def metrics(_: Request) -> Response:
    """Prometheus text for the fleet. Scrape this, don't scrape the dashboard."""
    from starlette.responses import PlainTextResponse

    gateway = get_gateway()
    try:
        audit_total = gateway.db.audit_count()
    except Exception:
        audit_total = -1
    try:
        generation = await gateway.store.get_int("fleet:generation")
        stopped = 1 if (await gateway.store.get_text("fleet:stopped")) == "1" else 0
    except Exception:
        generation, stopped = -1, -1
    body = "\n".join(
        [
            "# HELP gov_flight_attempts Airline simulator calls (holds).",
            "# TYPE gov_flight_attempts counter",
            f"gov_flight_attempts {gateway.sims.flight_attempts}",
            "# HELP gov_flight_committed Committed flight bookings.",
            "# TYPE gov_flight_committed counter",
            f"gov_flight_committed {gateway.sims.flight_committed}",
            "# HELP gov_audit_events_total Hash-chained audit rows.",
            "# TYPE gov_audit_events_total counter",
            f"gov_audit_events_total {audit_total}",
            "# HELP gov_fleet_generation Control-plane generation clock.",
            "# TYPE gov_fleet_generation gauge",
            f"gov_fleet_generation {generation}",
            "# HELP gov_stopped 1 when the fleet is stopped.",
            "# TYPE gov_stopped gauge",
            f"gov_stopped {stopped}",
        ]
    )
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4")


@mcp.custom_route("/v1/demo/scenarios/{name}", methods=["POST"])
async def run_scenario(request: Request) -> Response:
    name = request.path_params["name"]
    gateway = get_gateway()
    try:
        await gateway.demo_reset()
    except ControlPlaneUnavailable:
        return _json(
            {"decision": "deny", "reason_code": "CONTROL_PLANE_UNAVAILABLE", "adapter_status": "not_called"}
        )
    agent_id = str(request.query_params.get("agent_id", "travel-concierge"))
    if gateway.db.agent(agent_id) is None:
        return _json({"error": f"unknown agent {agent_id}"}, status=404)
    key = f"demo-{name}-{uuid.uuid4()}"
    resource = {"itinerary_id": "demo-itin", "flight_numbers": "AI111/BA173", "passenger": "Ananya Sharma"}
    if name == "allow":
        gateway.sims.flight_delay_ms = 1600
        result = await gateway.execute(agent_id, "rebook_flight", 18_000, key, resource)
        gateway.sims.flight_delay_ms = 0
        return _json(result)
    if name == "team-stop":
        await gateway.stop_team("travel-benefits")
        result = await gateway.execute("travel-concierge", "rebook_flight", 18_000, key, resource)
        return _json(result)
    if name == "over-cap":
        await gateway.set_cap(agent_id, 10_000)
        result = await gateway.execute(agent_id, "rebook_flight", 18_000, key, resource)
        return _json(result)
    if name == "fleet-cap":
        await gateway.set_cap("fleet", 20_000)
        first = await gateway.execute(
            "travel-concierge", "rebook_flight", 18_000, f"{key}-first", resource
        )
        second_resource = {"itinerary_id": "claim-itin", "flight_numbers": "AI111/BA173", "passenger": "Claim top-up"}
        second = await gateway.execute(
            "claims-assistant", "rebook_flight", 18_000, f"{key}-second", second_resource
        )
        return _json({"first": first, "second": second})
    if name == "wire-deny":
        result = await gateway.execute("travel-concierge", "wire_funds", 5_000, key, {"to": "demo"})
        return _json(result)
    if name == "inflight-stop":
        gateway.sims.flight_delay_ms = 2000

        async def hit_stop() -> None:
            await asyncio.sleep(0.3)
            await gateway.emergency_stop()

        asyncio.get_running_loop().create_task(hit_stop())
        result = await gateway.execute("travel-concierge", "rebook_flight", 18_000, key, resource)
        return _json(result)
    return _json({"error": "unknown scenario"}, status=404)


@mcp.custom_route("/v1/demo/live-rebook", methods=["POST"])
async def live_rebook(_: Request) -> Response:
    gateway = get_gateway()
    try:
        await gateway.demo_reset()
    except ControlPlaneUnavailable:
        return _json(
            {"decision": "deny", "reason_code": "CONTROL_PLANE_UNAVAILABLE", "adapter_status": "not_called"}
        )
    gateway.sims.flight_delay_ms = 8000
    key = f"demo-live-{uuid.uuid4()}"
    resource = {"itinerary_id": "itin-180", "flight_numbers": "AI111/BA173", "route": "DEL T3–LHR–JFK T8", "passenger": "Ananya Sharma"}
    result = await gateway.execute("travel-concierge", "rebook_flight", 18_000, key, resource)
    gateway.sims.flight_delay_ms = 0
    return _json(result)


@mcp.custom_route("/v1/demo/reset", methods=["POST"])
async def demo_reset(_: Request) -> Response:
    try:
        await get_gateway().demo_reset()
    except ControlPlaneUnavailable:
        return _json({"ok": False, "reason_code": "CONTROL_PLANE_UNAVAILABLE"})
    return _json({"ok": True})


@mcp.custom_route("/v1/demo/tamper", methods=["POST"])
async def tamper(_: Request) -> Response:
    seq = get_gateway().tamper_latest()
    return _json({"tampered_seq": seq, "demo_only": True, "verify": get_gateway().verify()})


@mcp.custom_route("/v1/demo/bookings", methods=["GET"])
async def bookings(_: Request) -> Response:
    sims = get_gateway().sims
    return _json(
        {
            "flight_attempts": sims.flight_attempts,
            "flight_committed": sims.flight_committed,
        }
    )


def build_app():
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return app


def wait_until_ready(database_url: str, redis_url: str, opa_url: str) -> None:
    last_error = "not started"
    for attempt in range(60):
        try:
            with psycopg.connect(database_url, connect_timeout=3) as conn:
                conn.execute("SELECT 1")
            redis.Redis.from_url(redis_url, socket_connect_timeout=3).ping()
            response = httpx.get(opa_url.rstrip("/") + "/health", timeout=2.0)
            if response.status_code == 200:
                print("dependencies ready", flush=True)
                return
            last_error = f"opa status {response.status_code}"
        except Exception as exc:
            last_error = str(exc)
        print(f"waiting for dependencies ({attempt + 1}): {last_error}", flush=True)
        time.sleep(1)
    raise RuntimeError(f"postgres, redis, or opa did not become ready: {last_error}")


def _local_database_url() -> str:
    try:
        user = os.environ["POSTGRES_USER"]
        secret = os.environ["POSTGRES_PASSWORD"]
        dbname = os.environ["POSTGRES_DB"]
    except KeyError as exc:
        raise RuntimeError(
            "Set DATABASE_URL or copy deploy/.env.example to deploy/.env "
            "(compose always sets DATABASE_URL, so this only affects bare-metal runs)"
        ) from exc
    return f"postgresql://{user}:{secret}@localhost:5433/{dbname}"


def main() -> None:
    database_url = os.environ.get("DATABASE_URL", _local_database_url())
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6380/0")
    opa_url = os.environ.get("OPA_URL", "http://localhost:8181")
    policy_version = os.environ.get("POLICY_VERSION", "rego-1")
    wait_until_ready(database_url, redis_url, opa_url)
    gateway = Gateway(Database(database_url), RedisStore(redis_url), OpaClient(opa_url), Simulators(), policy_version)
    asyncio.run(gateway.bootstrap())
    bind_gateway(gateway)
    uvicorn.run(build_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8001")))


if __name__ == "__main__":
    main()
