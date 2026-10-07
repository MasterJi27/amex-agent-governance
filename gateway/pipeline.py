from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

import redis

from gateway.db import SEED_CAPS, Database
from gateway.hashchain import canonical_json, verify_chain
from gateway.opa_client import ControlPlaneUnavailable, OpaClient, PolicyUnavailable
from gateway.redis_store import RedisStore
from gateway.simulators import Simulators

CAP_REASONS = ("AGENT_CAP", "TEAM_CAP", "FLEET_CAP")
HARD_STOPS = {
    "REVOKED",
    "EMERGENCY_STOP",
    "STALE_GENERATION",
    "ACTION_NOT_ALLOWED",
    "POLICY_UNAVAILABLE",
    "CONTROL_PLANE_UNAVAILABLE",
    "AGENT_NOT_FOUND",
}


class Gateway:
    def __init__(self, database: Database, store: RedisStore, opa: OpaClient, sims: Simulators, policy_version: str) -> None:
        self.db = database
        self.store = store
        self.opa = opa
        self.sims = sims
        self.policy_version = policy_version
        # Bounds read fan-out (budget_view opens 3 pg conns + ~16 redis
        # checkouts). 20 concurrent views = 60 pg conns max, inside the
        # Postgres 100 default. The money path (execute) never waits here.
        self._read_limit = asyncio.Semaphore(20)

    async def bootstrap(self) -> None:
        self.db.apply_schema()
        await self.rebuild_redis()
        # The pool may have connected on a throwaway loop (asyncio.run at
        # startup, one per test). Drop it so the serving loop dials fresh —
        # otherwise every op fails over to a reconnect, and bursts exhaust
        # the pool.
        await self.store.drop()

    async def rebuild_redis(self) -> None:
        spent = self.db.spent_by_node()
        for node in self.db.budget_nodes():
            node_id = str(node["id"])
            await self._guard(self.store.set_value(f"budget:{node_id}:cap", str(node["cap_cents"])))
            await self._guard(self.store.set_value(f"budget:{node_id}:spent", str(spent.get(node_id, 0))))
        control = self.db.control()
        await self._guard(self.store.set_value("fleet:generation", str(control["fleet_generation"])))
        await self._guard(self.store.set_value("fleet:stopped", "1" if control["stopped"] else "0"))
        for agent in self.db.agents():
            agent_id = str(agent["id"])
            await self._guard(self.store.set_value(f"agent:{agent_id}:generation", str(agent["generation"])))
            await self._guard(self.store.set_value(f"agent:{agent_id}:status", str(agent["status"])))

    def path_for(self, agent_id: str, team_id: str) -> tuple[list[str], list[str]]:
        return [ "fleet", team_id, agent_id ], ["FLEET_CAP", "TEAM_CAP", "AGENT_CAP"]

    async def _guard(self, coro):
        try:
            return await coro
        except redis.RedisError as exc:
            raise ControlPlaneUnavailable(str(exc)) from exc

    async def fleet_stopped(self) -> bool:
        return (await self._guard(self.store.get_text("fleet:stopped"))) == "1"

    async def team_stopped(self, team_id: str) -> bool:
        return (await self._guard(self.store.get_text(f"team:{team_id}:stopped"))) == "1"

    async def stop_team(self, team_id: str) -> None:
        await self._guard(self.store.set_value(f"team:{team_id}:stopped", "1"))

    async def clear_team_stops(self) -> None:
        teams = {str(agent["team_id"]) for agent in self.db.agents()}
        for team_id in teams:
            await self._guard(self.store.set_value(f"team:{team_id}:stopped", "0"))

    async def read_generation(self, agent_id: str) -> tuple[int, int]:
        return (
            await self._guard(self.store.get_int("fleet:generation")),
            await self._guard(self.store.get_int(f"agent:{agent_id}:generation")),
        )

    async def set_cap(self, node_id: str, cap_cents: int) -> None:
        if cap_cents < 0:
            raise ValueError("cap must be >= 0")
        await self._guard(self.store.set_value(f"budget:{node_id}:cap", str(cap_cents)))
        self.db.set_cap(node_id, cap_cents)

    async def emergency_stop(self) -> int:
        generation = await self._guard(self.store.incr("fleet:generation"))
        await self._guard(self.store.set_value("fleet:stopped", "1"))
        self.db.set_control(generation, True)
        return generation

    async def resume(self) -> int:
        generation = await self._guard(self.store.get_int("fleet:generation"))
        await self._guard(self.store.set_value("fleet:stopped", "0"))
        await self.clear_team_stops()
        self.db.set_control(generation, False)
        return generation

    async def revoke(self, agent_id: str) -> int:
        generation = await self._guard(self.store.incr(f"agent:{agent_id}:generation"))
        await self._guard(self.store.set_value(f"agent:{agent_id}:status", "revoked"))
        self.db.set_agent(agent_id, "revoked", generation)
        return generation

    async def restore_agent(self, agent_id: str) -> int:
        generation = await self._guard(self.store.get_int(f"agent:{agent_id}:generation"))
        await self._guard(self.store.set_value(f"agent:{agent_id}:status", "active"))
        self.db.set_agent(agent_id, "active", generation)
        return generation

    async def budget_view(self) -> dict[str, Any]:
        # Independent reads go out together: sync pg calls run in threads so
        # they never block the loop, redis reads gather into ~3 batches.
        # Any redis failure still raises ControlPlaneUnavailable (fail-closed).
        async with self._read_limit:
            return await self._budget_view_inner()

    async def _budget_view_inner(self) -> dict[str, Any]:
        meta_nodes, control, agents_meta = await asyncio.gather(
            asyncio.to_thread(self.db.budget_nodes),
            asyncio.to_thread(self.db.control),
            asyncio.to_thread(self.db.agents),
        )
        node_ids = [str(node["id"]) for node in meta_nodes]
        caps = await asyncio.gather(
            *[self._guard(self.store.get_int(f"budget:{node_id}:cap")) for node_id in node_ids]
        )
        spents = await asyncio.gather(
            *[self._guard(self.store.get_int(f"budget:{node_id}:spent")) for node_id in node_ids]
        )
        nodes = [
            {
                "id": node_id,
                "parent_id": node["parent_id"],
                "cap_cents": cap,
                "spent_cents": spent,
                "remaining_cents": cap - spent,
            }
            for node, node_id, cap, spent in zip(meta_nodes, node_ids, caps, spents)
        ]
        gen_and_flags = await asyncio.gather(
            self._guard(self.store.get_int("fleet:generation")),
            self.fleet_stopped(),
            *[
                self._guard(self.store.get_text(f"agent:{agent['id']}:status"))
                for agent in agents_meta
            ],
            *[
                self._guard(self.store.get_int(f"agent:{agent['id']}:generation"))
                for agent in agents_meta
            ],
            *[
                self.team_stopped(team_id)
                for team_id in sorted({str(agent["team_id"]) for agent in agents_meta})
            ],
        )
        control_generation = gen_and_flags[0]
        stopped = gen_and_flags[1]
        agents = []
        for index, agent in enumerate(agents_meta):
            agents.append(
                {
                    "id": agent["id"],
                    "team_id": agent["team_id"],
                    "status": gen_and_flags[2 + index] or agent["status"],
                    "generation": gen_and_flags[2 + len(agents_meta) + index],
                }
            )
        team_ids = sorted({str(agent["team_id"]) for agent in agents_meta})
        teams = [
            {"id": team_id, "stopped": gen_and_flags[2 + 2 * len(agents_meta) + index]}
            for index, team_id in enumerate(team_ids)
        ]
        return {
            "nodes": nodes,
            "fleet_generation": control_generation,
            "stopped": stopped,
            "agents": agents,
            "bookings": {
                "flight_attempts": self.sims.flight_attempts,
                "flight_committed": self.sims.flight_committed,
            },
            "teams": teams,
        }

    def verify(self) -> dict[str, Any]:
        broken = verify_chain(self.db.audit_rows_asc())
        if broken is None:
            return {"ok": True, "broken_seq": None}
        return {"ok": False, "broken_seq": broken}

    def request_override(
        self,
        agent_id: str,
        action: str,
        amount_cents: int,
        resource: dict[str, Any],
        node_id: str,
        reason: str,
        requested_by: str,
    ) -> dict[str, Any]:
        """Propose a targeted cap raise with a human reason. Fixes *CAP denies
        only — the OPA allowlist can never be overridden."""
        if amount_cents < 0:
            return {"ok": False, "error": "amount must be >= 0"}
        if not reason.strip() or not requested_by.strip():
            return {"ok": False, "error": "reason and requested_by are required"}
        agent = self.db.agent(agent_id)
        if agent is None:
            return {"ok": False, "error": f"unknown agent {agent_id}"}
        path, _reasons = self.path_for(agent_id, str(agent["team_id"]))
        if node_id not in path:
            return {"ok": False, "error": f"{node_id} is not on {agent_id}'s budget path"}
        try:
            if not self.opa.allow(agent_id, action):
                return {"ok": False, "error": "action not allowed for agent — overrides cannot lift the allowlist"}
        except PolicyUnavailable as exc:
            return {"ok": False, "error": f"policy unavailable: {exc}"}
        override_id = str(uuid.uuid4())
        self.db.create_override(
            override_id, agent_id, action, amount_cents,
            json.dumps(resource), node_id, reason.strip(), requested_by.strip(),
        )
        return {"ok": True, "override_id": override_id, "status": "pending"}

    async def approve_override(self, override_id: str, approved_by: str) -> dict[str, Any]:
        """Second human approves: raise that node to spent+amount, then retry
        the original call through the normal gate (still audited)."""
        if not approved_by.strip():
            return {"ok": False, "error": "approved_by is required"}
        ov = self.db.get_override(override_id)
        if ov is None:
            return {"ok": False, "error": f"unknown override {override_id}"}
        if str(ov["status"]) != "pending":
            return {"ok": False, "error": f"override is already {ov['status']}"}
        if approved_by.strip() == str(ov["requested_by"]):
            return {"ok": False, "error": "approver must differ from requester (maker-checker)"}
        agent_id, action = str(ov["agent_id"]), str(ov["action"])
        try:
            if not self.opa.allow(agent_id, action):
                return {"ok": False, "error": "action no longer allowed — override refused"}
        except PolicyUnavailable as exc:
            return {"ok": False, "error": f"policy unavailable: {exc}"}
        try:
            spent = await self._guard(self.store.get_int(f"budget:{ov['node_id']}:spent"))
        except ControlPlaneUnavailable as exc:
            return {"ok": False, "error": f"control plane unavailable: {exc}"}
        new_cap = spent + int(ov["amount_cents"])
        await self.set_cap(str(ov["node_id"]), new_cap)
        try:
            resource = json.loads(str(ov["resource"]))
        except json.JSONDecodeError:
            resource = {}
        result = await self.execute(
            agent_id, action, int(ov["amount_cents"]), f"{override_id}:exec", resource,
        )
        self.db.approve_override(override_id, approved_by.strip(), json.dumps(result))
        return {"ok": True, "override_id": override_id, "new_cap_cents": new_cap, "execution": result}

    def audit_tail(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = []
        for row in self.db.audit_tail(limit):
            payload_text = str(row["payload"])
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError:
                payload = {"raw": payload_text}
            rows.append(
                {
                    "seq": row["seq"],
                    "created_at": row["created_at"].isoformat(),
                    "payload": payload,
                }
            )
        return rows

    async def demo_reset(self) -> None:
        # Fail fast with a domain error (not a raw redis traceback) when the
        # control plane is down, so demo routes can answer CONTROL_PLANE_UNAVAILABLE.
        await self._guard(self.store.ping())
        self.db.release_all()
        self.db.clear_invocations()
        self.db.restore_seed_caps_and_agents()
        generation = await self._guard(self.store.get_int("fleet:generation"))
        for agent in self.db.agents():
            agent_generation = await self._guard(self.store.get_int(f"agent:{agent['id']}:generation"))
            self.db.set_agent(str(agent["id"]), "active", agent_generation)
        self.db.set_control(generation, False)
        self.sims.reset_counters()
        await self.clear_team_stops()
        await self.rebuild_redis()

    def tamper_latest(self) -> int | None:
        return self.db.tamper_latest()

    async def preview_cap(self, agent_id: str, node_id: str, cap_cents: int, amount_cents: int) -> dict[str, Any]:
        agent = self.db.agent(agent_id)
        if agent is None:
            return {"ok": False, "reason_code": "AGENT_NOT_FOUND", "node_id": node_id, "amount_cents": amount_cents}
        path, reasons = self.path_for(agent_id, str(agent["team_id"]))
        for node, reason in zip(path, reasons, strict=True):
            cap = cap_cents if node == node_id else await self._guard(self.store.get_int(f"budget:{node}:cap"))
            spent = await self._guard(self.store.get_int(f"budget:{node}:spent"))
            if spent + amount_cents > cap:
                return {
                    "ok": False,
                    "reason_code": reason,
                    "node_id": node,
                    "cap_cents": cap,
                    "spent_cents": spent,
                    "amount_cents": amount_cents,
                    "applied": False,
                }
        return {
            "ok": True,
            "reason_code": "OK",
            "node_id": node_id,
            "cap_cents": cap_cents,
            "amount_cents": amount_cents,
            "applied": False,
        }

    async def execute(
        self,
        agent_id: str,
        action: str,
        amount_cents: int,
        idempotency_key: str,
        resource: dict[str, Any],
    ) -> dict[str, Any]:
        if amount_cents < 0:
            raise ValueError("amount_cents must be >= 0")
        existing = await self._existing_result(idempotency_key)
        if existing is not None:
            return existing
        if not self.db.claim_invocation(idempotency_key):
            return await self._wait_result(idempotency_key)

        started = time.perf_counter()
        try:
            result = await self._run(agent_id, action, amount_cents, idempotency_key, resource, started)
        except Exception as exc:
            result = self._deny(
                agent_id,
                action,
                amount_cents,
                idempotency_key,
                resource,
                "CONTROL_PLANE_UNAVAILABLE",
                started,
                adapter_status="not_called",
                detail=str(exc),
            )
        self.db.finish_invocation(idempotency_key, json.dumps(result))
        return result

    async def _existing_result(self, key: str) -> dict[str, Any] | None:
        row = self.db.invocation(key)
        if row is None:
            return None
        if row["status"] == "done" and row["result_json"]:
            return json.loads(row["result_json"])
        return await self._wait_result(key)

    async def _wait_result(self, key: str) -> dict[str, Any]:
        for _ in range(100):
            row = self.db.invocation(key)
            if row and row["status"] == "done" and row["result_json"]:
                return json.loads(row["result_json"])
            await asyncio.sleep(0.05)
        raise TimeoutError(f"invocation {key} did not finish")

    async def _run(
        self,
        agent_id: str,
        action: str,
        amount_cents: int,
        idempotency_key: str,
        resource: dict[str, Any],
        started: float,
    ) -> dict[str, Any]:
        agent = self.db.agent(agent_id)
        if agent is None:
            return self._deny(
                agent_id, action, amount_cents, idempotency_key, resource, "AGENT_NOT_FOUND", started, "not_called"
            )
        try:
            team_id = str(agent["team_id"])
            fleet_halted = await self.fleet_stopped()
            team_halted = await self.team_stopped(team_id)
            revoked = (await self._guard(self.store.get_text(f"agent:{agent_id}:status"))) == "revoked"
            if fleet_halted or team_halted or revoked:
                if fleet_halted:
                    reason = "EMERGENCY_STOP"
                elif team_halted:
                    reason = "TEAM_STOPPED"
                else:
                    reason = "REVOKED"
                return self._deny(
                    agent_id, action, amount_cents, idempotency_key, resource, reason, started, "not_called"
                )
        except ControlPlaneUnavailable:
            return self._deny(
                agent_id,
                action,
                amount_cents,
                idempotency_key,
                resource,
                "CONTROL_PLANE_UNAVAILABLE",
                started,
                "not_called",
            )

        try:
            allowed = self.opa.allow(agent_id, action)
        except PolicyUnavailable:
            return self._deny(
                agent_id, action, amount_cents, idempotency_key, resource, "POLICY_UNAVAILABLE", started, "not_called"
            )
        if not allowed:
            return self._deny(
                agent_id, action, amount_cents, idempotency_key, resource, "ACTION_NOT_ALLOWED", started, "not_called"
            )

        if action in ("rebook_flight", "change_hotel"):
            try:
                if self.db.recent_allow_count(agent_id, action, 10) >= 2:
                    return self._deny(
                        agent_id, action, amount_cents, idempotency_key, resource, "VELOCITY", started, "not_called"
                    )
            except Exception:
                pass

        path, reasons = self.path_for(agent_id, str(agent["team_id"]))
        try:
            ok, reason, remaining = await self._guard(self.store.reserve(path, amount_cents, reasons))
            fleet_generation, agent_generation = await self.read_generation(agent_id)
        except ControlPlaneUnavailable:
            return self._deny(
                agent_id,
                action,
                amount_cents,
                idempotency_key,
                resource,
                "CONTROL_PLANE_UNAVAILABLE",
                started,
                "not_called",
            )
        if not ok:
            return self._deny(
                agent_id,
                action,
                amount_cents,
                idempotency_key,
                resource,
                reason,
                started,
                "not_called",
                remaining_cents=remaining,
                budget_node=reason,
            )

        reservation_id = str(uuid.uuid4())
        self.db.insert_reservation(
            reservation_id,
            idempotency_key,
            agent_id,
            action,
            amount_cents,
            path,
            fleet_generation,
            agent_generation,
        )
        try:
            booked = await self._adapter(action, resource)
        except Exception:
            await self._release(path, amount_cents, reservation_id)
            return self._deny(
                agent_id,
                action,
                amount_cents,
                idempotency_key,
                resource,
                "ADAPTER_FAILED",
                started,
                "released",
                reservation_id=reservation_id,
            )

        try:
            stopped = await self.fleet_stopped()
            current_fleet, current_agent = await self.read_generation(agent_id)
        except ControlPlaneUnavailable:
            await self._release(path, amount_cents, reservation_id)
            return self._deny(
                agent_id,
                action,
                amount_cents,
                idempotency_key,
                resource,
                "CONTROL_PLANE_UNAVAILABLE",
                started,
                "released",
                reservation_id=reservation_id,
            )

        stale = stopped or current_fleet != fleet_generation or current_agent != agent_generation
        if stale or not booked:
            await self._release(path, amount_cents, reservation_id)
            reason = "STALE_GENERATION" if stale else "ADAPTER_FAILED"
            return self._deny(
                agent_id,
                action,
                amount_cents,
                idempotency_key,
                resource,
                reason,
                started,
                "released",
                reservation_id=reservation_id,
                generation=current_fleet,
            )

        if not self.db.transition_reservation(reservation_id, "committed"):
            await self._release(path, amount_cents, reservation_id)
            return self._deny(
                agent_id,
                action,
                amount_cents,
                idempotency_key,
                resource,
                "STALE_GENERATION",
                started,
                "released",
                reservation_id=reservation_id,
            )
        self._confirm(action)
        return self._allow(
            agent_id,
            action,
            amount_cents,
            idempotency_key,
            resource,
            started,
            reservation_id,
            remaining,
            fleet_generation,
        )

    async def _adapter(self, action: str, resource: dict[str, Any]) -> bool:
        if action == "rebook_flight":
            return await self.sims.book_flight(resource)
        if action == "change_hotel":
            return await self.sims.book_hotel(resource)
        if action == "send_notification":
            return await self.sims.send_notification(resource)
        return False

    def _confirm(self, action: str) -> None:
        if action == "rebook_flight":
            self.sims.confirm_flight()
        elif action == "change_hotel":
            self.sims.confirm_hotel()
        elif action == "send_notification":
            self.sims.confirm_notification()

    async def _release(self, path: list[str], amount_cents: int, reservation_id: str) -> None:
        changed = self.db.transition_reservation(reservation_id, "released")
        if changed:
            await self._guard(self.store.release(path, amount_cents))

    def _payload(
        self,
        agent_id: str,
        action: str,
        amount_cents: int,
        idempotency_key: str,
        resource: dict[str, Any],
        decision: str,
        reason_code: str,
        started: float,
        adapter_status: str,
        reservation_id: str | None = None,
        remaining_cents: int | None = None,
        budget_node: str | None = None,
        generation: int | None = None,
    ) -> dict[str, Any]:
        return {
            "adapter_status": adapter_status,
            "agent_id": agent_id,
            "amount_cents": amount_cents,
            "action": action,
            "budget_node": budget_node,
            "decision": decision,
            "generation": generation,
            "idempotency_key": idempotency_key,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "policy_version": self.policy_version,
            "reason_code": reason_code,
            "remaining_cents": remaining_cents,
            "reservation_id": reservation_id,
            "resource": resource,
        }

    def _record(
        self,
        agent_id: str,
        action: str,
        amount_cents: int,
        idempotency_key: str,
        resource: dict[str, Any],
        decision: str,
        reason_code: str,
        started: float,
        adapter_status: str,
        reservation_id: str | None = None,
        remaining_cents: int | None = None,
        budget_node: str | None = None,
        generation: int | None = None,
    ) -> dict[str, Any]:
        payload = self._payload(
            agent_id,
            action,
            amount_cents,
            idempotency_key,
            resource,
            decision,
            reason_code,
            started,
            adapter_status,
            reservation_id,
            remaining_cents,
            budget_node,
            generation,
        )
        seq, _digest = self.db.append_audit(canonical_json(payload))
        return {
            "decision": decision,
            "reason_code": reason_code,
            "reservation_id": reservation_id,
            "remaining_cents": remaining_cents,
            "policy_version": self.policy_version,
            "audit_seq": seq,
            "adapter_status": adapter_status,
        }

    def _deny(
        self,
        agent_id: str,
        action: str,
        amount_cents: int,
        idempotency_key: str,
        resource: dict[str, Any],
        reason_code: str,
        started: float,
        adapter_status: str,
        reservation_id: str | None = None,
        remaining_cents: int | None = None,
        budget_node: str | None = None,
        generation: int | None = None,
        detail: str | None = None,
    ) -> dict[str, Any]:
        del detail
        return self._record(
            agent_id,
            action,
            amount_cents,
            idempotency_key,
            resource,
            "deny",
            reason_code,
            started,
            adapter_status,
            reservation_id,
            remaining_cents,
            budget_node,
            generation,
        )

    def _allow(
        self,
        agent_id: str,
        action: str,
        amount_cents: int,
        idempotency_key: str,
        resource: dict[str, Any],
        started: float,
        reservation_id: str,
        remaining_cents: int,
        generation: int,
    ) -> dict[str, Any]:
        return self._record(
            agent_id,
            action,
            amount_cents,
            idempotency_key,
            resource,
            "allow",
            "OK",
            started,
            "committed",
            reservation_id,
            remaining_cents,
            None,
            generation,
        )


def seed_caps() -> dict[str, int]:
    return dict(SEED_CAPS)
