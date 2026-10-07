from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

import httpx
import psycopg
import pytest
import redis

from gateway.db import Database
from gateway.opa_client import OpaClient
from gateway.pipeline import Gateway
from gateway.redis_store import RedisStore
from gateway.simulators import Simulators

def _local_database_url() -> str:
    if "DATABASE_URL" in os.environ:
        return os.environ["DATABASE_URL"]
    # Test runner provides DATABASE_URL (compose service env). No hardcoded
    # credentials here so secret scanners stay quiet.
    user = os.environ.get("POSTGRES_USER", "")
    secret = os.environ.get("POSTGRES_PASSWORD", "")
    dbname = os.environ.get("POSTGRES_DB", "")
    return f"postgresql://{user}:{secret}@localhost:5433/{dbname}"


DATABASE_URL = os.environ.get("DATABASE_URL", _local_database_url())
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6380/0")
OPA_URL = os.environ.get("OPA_URL", "http://localhost:8181")


def _infra_up() -> bool:
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute("SELECT 1")
        redis.Redis.from_url(REDIS_URL).ping()
        health = httpx.get(OPA_URL.rstrip("/") + "/health", timeout=2.0)
        return health.status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _infra_up(), reason="postgres and redis are not running")


@pytest.fixture(scope="module")
def gateway() -> Gateway:
    instance = Gateway(
        Database(DATABASE_URL),
        RedisStore(REDIS_URL),
        OpaClient(OPA_URL),
        Simulators(),
        "rego-1",
    )
    asyncio.run(instance.bootstrap())
    return instance


@pytest.fixture(autouse=True)
def _reset(gateway: Gateway):
    asyncio.run(gateway.demo_reset())
    # Audit is append-only in prod, but tests need isolation: the velocity
    # rule counts recent allows, so leftover rows would poison later tests.
    gateway.db.clear_audit()
    yield


def _run(gateway: Gateway, key: str, amount: int, agent_id: str = "travel-concierge") -> dict:
    return asyncio.run(
        gateway.execute(
            agent_id,
            "rebook_flight",
            amount,
            key,
            {"itinerary_id": "itin-test", "flight_numbers": "UA1"},
        )
    )


def test_over_agent_cap_does_not_spend(gateway: Gateway) -> None:
    asyncio.run(gateway.set_cap("travel-concierge", 10_000))
    result = asyncio.run(
        gateway.execute(
            "travel-concierge",
            "rebook_flight",
            18_000,
            "over-cap",
            {"itinerary_id": "itin-180", "flight_numbers": "UA901/UA902"},
        )
    )
    assert result["reason_code"] == "AGENT_CAP"
    assert asyncio.run(gateway.store.get_int("budget:travel-concierge:spent")) == 0
    assert gateway.sims.flight_attempts == 0


def test_unknown_action_does_not_spend(gateway: Gateway) -> None:
    result = asyncio.run(
        gateway.execute("travel-concierge", "wire_funds", 100, "bad-action", {})
    )
    assert result["reason_code"] == "ACTION_NOT_ALLOWED"
    assert asyncio.run(gateway.store.get_int("budget:fleet:spent")) == 0


def test_unknown_agent(gateway: Gateway) -> None:
    result = asyncio.run(gateway.execute("nobody", "rebook_flight", 100, "nobody", {}))
    assert result["reason_code"] == "AGENT_NOT_FOUND"


def test_concurrent_team_cap(gateway: Gateway) -> None:
    asyncio.run(gateway.set_cap("travel-benefits", 50_000))
    asyncio.run(gateway.set_cap("travel-concierge", 80_000))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda item: _run(gateway, item[0], item[1]), [("cap-a", 30_000), ("cap-b", 30_000)]))
    reasons = {result["reason_code"] for result in results}
    assert reasons == {"OK", "TEAM_CAP"}
    assert asyncio.run(gateway.store.get_int("budget:travel-benefits:spent")) == 30_000
    assert gateway.db.spent_by_node()["travel-benefits"] == 30_000


def test_sibling_trips_fleet_cap_while_agent_cap_has_room(gateway: Gateway) -> None:
    asyncio.run(gateway.set_cap("fleet", 60_000))
    sibling = asyncio.run(
        gateway.execute(
            "claims-assistant",
            "rebook_flight",
            50_000,
            "sibling",
            {"itinerary_id": "sx", "flight_numbers": "XX1"},
        )
    )
    assert sibling["reason_code"] == "OK"
    blocked = asyncio.run(
        gateway.execute(
            "travel-concierge",
            "rebook_flight",
            20_000,
            "fleet-block",
            {"itinerary_id": "fy", "flight_numbers": "UA2"},
        )
    )
    assert blocked["reason_code"] == "FLEET_CAP"
    assert asyncio.run(gateway.store.get_int("budget:travel-concierge:spent")) == 0


def test_stop_during_delay_discards_the_booking(gateway: Gateway) -> None:
    gateway.sims.flight_delay_ms = 400

    async def scenario() -> dict:
        async def hit_stop() -> None:
            await asyncio.sleep(0.1)
            await gateway.emergency_stop()

        asyncio.get_running_loop().create_task(hit_stop())
        return await gateway.execute(
            "travel-concierge",
            "rebook_flight",
            18_000,
            "inflight",
            {"itinerary_id": "itin-180", "flight_numbers": "UA901/UA902"},
        )

    result = asyncio.run(scenario())
    assert result["reason_code"] == "STALE_GENERATION"
    assert result["adapter_status"] == "released"
    assert gateway.sims.flight_committed == 0
    assert asyncio.run(gateway.store.get_int("budget:travel-concierge:spent")) == 0


def test_emergency_stop_before_call_skips_the_simulator(gateway: Gateway) -> None:
    asyncio.run(gateway.emergency_stop())
    result = _run(gateway, "stopped", 18_000)
    assert result["reason_code"] == "EMERGENCY_STOP"
    assert gateway.sims.flight_attempts == 0


def test_idempotent_retry_does_not_book_twice(gateway: Gateway) -> None:
    first = _run(gateway, "same-key", 18_000)
    second = _run(gateway, "same-key", 18_000)
    assert first["reason_code"] == "OK"
    assert second == first
    assert gateway.sims.flight_attempts == 1
    assert gateway.sims.flight_committed == 1


def test_release_restores_cap_when_adapter_fails(gateway: Gateway) -> None:
    gateway.sims.fail_flights = True
    result = _run(gateway, "fail-flight", 18_000)
    assert result["reason_code"] == "ADAPTER_FAILED"
    assert asyncio.run(gateway.store.get_int("budget:fleet:spent")) == 0
    assert gateway.sims.flight_committed == 0


def test_revoke_blocks_the_next_call(gateway: Gateway) -> None:
    asyncio.run(gateway.revoke("travel-concierge"))
    result = _run(gateway, "revoked", 18_000)
    assert result["reason_code"] == "REVOKED"
    assert gateway.sims.flight_attempts == 0


def test_audit_chain_verify_and_tamper(gateway: Gateway) -> None:
    gateway.db.clear_audit()
    _run(gateway, "audit-1", 18_000)
    assert gateway.verify()["ok"] is True
    seq = gateway.tamper_latest()
    verified = gateway.verify()
    assert verified["ok"] is False
    assert verified["broken_seq"] == seq
    gateway.db.clear_audit()


def test_rebuild_restores_spent_after_redis_flush(gateway: Gateway) -> None:
    _run(gateway, "keep", 18_000)
    asyncio.run(gateway.store.flush())
    asyncio.run(gateway.rebuild_redis())
    assert asyncio.run(gateway.store.get_int("budget:travel-concierge:spent")) == 18_000
    assert asyncio.run(gateway.store.get_int("budget:fleet:spent")) == 18_000


def test_override_lifts_cap_but_not_allowlist(gateway: Gateway) -> None:
    asyncio.run(gateway.set_cap("travel-concierge", 10_000))
    denied = _run(gateway, "ov-denied", 18_000)
    assert denied["reason_code"] == "AGENT_CAP"
    proposed = gateway.request_override(
        "travel-concierge",
        "rebook_flight",
        18_000,
        {"itinerary_id": "itin-180", "flight_numbers": "UA901/UA902"},
        "travel-concierge",
        "stranded passenger, duty manager ok",
        "Rohan",
    )
    assert proposed["ok"] is True
    same = asyncio.run(gateway.approve_override(proposed["override_id"], "Rohan"))
    assert same["ok"] is False
    done = asyncio.run(gateway.approve_override(proposed["override_id"], "Priya"))
    assert done["ok"] is True
    assert done["new_cap_cents"] == 18_000
    assert done["execution"]["reason_code"] == "OK"
    assert asyncio.run(gateway.store.get_int("budget:travel-concierge:spent")) == 18_000


def test_override_refuses_forbidden_action(gateway: Gateway) -> None:
    refused = gateway.request_override(
        "travel-concierge",
        "wire_funds",
        5_000,
        {"to": "demo"},
        "travel-concierge",
        "please",
        "Rohan",
    )
    assert refused["ok"] is False


def test_opa_down_does_not_reserve(gateway: Gateway) -> None:
    broken = Gateway(gateway.db, gateway.store, OpaClient("http://127.0.0.1:1"), gateway.sims, "rego-1")
    result = asyncio.run(
        broken.execute("travel-concierge", "rebook_flight", 18_000, "opa-down", {"itinerary_id": "z", "flight_numbers": "UA9"})
    )
    assert result["reason_code"] == "POLICY_UNAVAILABLE"
    assert asyncio.run(gateway.store.get_int("budget:fleet:spent")) == 0
    assert gateway.sims.flight_attempts == 0
