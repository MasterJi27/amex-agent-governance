from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from gateway.hashchain import GENESIS, link_hash

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

SEED_CAPS = {
    "fleet": 200_000,
    "travel-benefits": 80_000,
    "card-servicing": 100_000,
    "travel-concierge": 50_000,
    "claims-assistant": 50_000,
    "refund-agent": 30_000,
}


class Database:
    def __init__(self, url: str) -> None:
        self.url = url

    def _connect(self) -> psycopg.Connection[dict[str, Any]]:
        return psycopg.connect(self.url, row_factory=dict_row)

    def apply_schema(self) -> None:
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        with self._connect() as conn:
            conn.execute(sql)
            conn.commit()

    def agent(self, agent_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT id, team_id, status, generation FROM governance.agents WHERE id = %s",
                (agent_id,),
            ).fetchone()

    def agents(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return list(
                conn.execute(
                    "SELECT id, team_id, status, generation FROM governance.agents ORDER BY id"
                ).fetchall()
            )

    def budget_nodes(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return list(
                conn.execute(
                    "SELECT id, parent_id, cap_cents FROM governance.budget_nodes ORDER BY id"
                ).fetchall()
            )

    def set_cap(self, node_id: str, cap_cents: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE governance.budget_nodes SET cap_cents = %s WHERE id = %s",
                (cap_cents, node_id),
            )
            conn.commit()

    def set_agent(self, agent_id: str, status: str, generation: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE governance.agents SET status = %s, generation = %s WHERE id = %s",
                (status, generation, agent_id),
            )
            conn.commit()

    def control(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT fleet_generation, stopped FROM governance.control WHERE id = 1"
            ).fetchone()
        if row is None:
            raise RuntimeError("control row missing")
        return row

    def set_control(self, fleet_generation: int, stopped: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE governance.control SET fleet_generation = %s, stopped = %s WHERE id = 1",
                (fleet_generation, stopped),
            )
            conn.commit()

    def claim_invocation(self, key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO governance.invocations (idempotency_key, status)
                VALUES (%s, 'processing')
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING idempotency_key
                """,
                (key,),
            ).fetchone()
            conn.commit()
        return row is not None

    def invocation(self, key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT idempotency_key, status, result_json FROM governance.invocations WHERE idempotency_key = %s",
                (key,),
            ).fetchone()

    def finish_invocation(self, key: str, result_json: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE governance.invocations
                SET status = 'done', result_json = %s
                WHERE idempotency_key = %s
                """,
                (result_json, key),
            )
            conn.commit()

    def insert_reservation(
        self,
        reservation_id: str,
        idempotency_key: str,
        agent_id: str,
        action: str,
        amount_cents: int,
        path: list[str],
        fleet_generation: int,
        agent_generation: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO governance.reservations (
                  id, idempotency_key, agent_id, action, amount_cents, path,
                  fleet_generation, agent_generation, state
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'reserved')
                """,
                (
                    reservation_id,
                    idempotency_key,
                    agent_id,
                    action,
                    amount_cents,
                    path,
                    fleet_generation,
                    agent_generation,
                ),
            )
            conn.commit()

    def transition_reservation(self, reservation_id: str, new_state: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                UPDATE governance.reservations
                SET state = %s
                WHERE id = %s AND state = 'reserved'
                RETURNING id
                """,
                (new_state, reservation_id),
            ).fetchone()
            conn.commit()
        return row is not None

    def release_all(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE governance.reservations
                SET state = 'released'
                WHERE state IN ('reserved', 'committed')
                """
            )
            conn.commit()

    def spent_by_node(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT node, COALESCE(SUM(amount_cents), 0) AS spent
                FROM governance.reservations, unnest(path) AS node
                WHERE state IN ('reserved', 'committed')
                GROUP BY node
                """
            ).fetchall()
        return {row["node"]: int(row["spent"]) for row in rows}

    def append_audit(self, payload_text: str) -> tuple[int, str]:
        with self._connect() as conn:
            with conn.transaction():
                head = conn.execute(
                    "SELECT hash FROM governance.chain_head WHERE id = 1 FOR UPDATE"
                ).fetchone()
                if head is None:
                    raise RuntimeError("chain head missing")
                prev_hash = str(head["hash"])
                digest = link_hash(prev_hash, payload_text)
                row = conn.execute(
                    """
                    INSERT INTO governance.audit_events (prev_hash, hash, payload)
                    VALUES (%s, %s, %s)
                    RETURNING seq
                    """,
                    (prev_hash, digest, payload_text),
                ).fetchone()
                conn.execute(
                    "UPDATE governance.chain_head SET hash = %s WHERE id = 1",
                    (digest,),
                )
            conn.commit()
        if row is None:
            raise RuntimeError("audit insert failed")
        return int(row["seq"]), digest

    def audit_rows_asc(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT seq, prev_hash, hash, payload, created_at
                    FROM governance.audit_events
                    ORDER BY seq ASC
                    """
                ).fetchall()
            )

    def audit_tail(self, limit: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT seq, prev_hash, hash, payload, created_at
                    FROM governance.audit_events
                    ORDER BY seq DESC
                    LIMIT %s
                    """,
                    (limit,),
                ).fetchall()
            )

    def ping(self) -> None:
        with self._connect() as conn:
            conn.execute("SELECT 1")

    def create_override(
        self,
        override_id: str,
        agent_id: str,
        action: str,
        amount_cents: int,
        resource_json: str,
        node_id: str,
        reason: str,
        requested_by: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO governance.overrides
                  (id, agent_id, action, amount_cents, resource, node_id, reason, requested_by, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending')
                """,
                (override_id, agent_id, action, amount_cents, resource_json, node_id, reason, requested_by),
            )
            conn.commit()

    def get_override(self, override_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM governance.overrides WHERE id = %s",
                (override_id,),
            ).fetchone()

    def list_overrides(self, status: str = "pending", limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, agent_id, action, amount_cents, resource, node_id,
                       reason, requested_by, approved_by, status, created_at
                FROM governance.overrides
                WHERE status = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (status, limit),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "agent_id": row["agent_id"],
                "action": row["action"],
                "amount_cents": row["amount_cents"],
                "resource": json.loads(str(row["resource"])),
                "node_id": row["node_id"],
                "reason": row["reason"],
                "requested_by": row["requested_by"],
                "approved_by": row["approved_by"],
                "status": row["status"],
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        ]

    def approve_override(self, override_id: str, approved_by: str, result_json: str) -> bool:
        """pending -> approved, only if still pending. Returns True if changed."""
        with self._connect() as conn:
            changed = conn.execute(
                """
                UPDATE governance.overrides
                SET status = 'approved', approved_by = %s, result_json = %s
                WHERE id = %s AND status = 'pending'
                """,
                (approved_by, result_json, override_id),
            ).rowcount
            conn.commit()
        return bool(changed)

    def audit_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM governance.audit_events").fetchone()
        return int(row["n"]) if row else 0

    def list_reservations(self, limit: int = 50) -> list[dict[str, Any]]:
        """Money ledger: every hold with its final state. ctid order is
        insertion order, newest first — the table has no clock of its own."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, agent_id, action, amount_cents, path, state
                FROM governance.reservations
                ORDER BY ctid DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "id": str(row["id"])[:8],
                "agent_id": str(row["agent_id"]),
                "action": str(row["action"]),
                "amount_cents": int(row["amount_cents"]),
                "path": list(row["path"] or []),
                "state": str(row["state"]),
            }
            for row in rows
        ]

    def recent_allow_count(self, agent_id: str, action: str, minutes: int = 10, limit: int = 300) -> int:
        """How many allow decisions for this agent+action in the last N minutes.

        Counts in Python (not SQL jsonb) so one tampered audit row can never
        break the guard. Used by the velocity rule: max 2 spend actions
        per agent per 10 minutes.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload, created_at FROM governance.audit_events ORDER BY seq DESC LIMIT %s",
                (limit,),
            ).fetchall()
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        count = 0
        for row in rows:
            created = row["created_at"]
            if created is not None and created < cutoff:
                break
            try:
                payload = json.loads(str(row["payload"]))
            except (json.JSONDecodeError, TypeError):
                continue
            if (
                payload.get("agent_id") == agent_id
                and payload.get("action") == action
                and payload.get("decision") == "allow"
            ):
                count += 1
        return count

    def tamper_latest(self) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT seq, payload FROM governance.audit_events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            payload = str(row["payload"])
            index = min(20, len(payload) - 1)
            flipped = "0" if payload[index] != "0" else "1"
            mutated = payload[:index] + flipped + payload[index + 1 :]
            conn.execute(
                "UPDATE governance.audit_events SET payload = %s WHERE seq = %s",
                (mutated, row["seq"]),
            )
            conn.commit()
        return int(row["seq"])

    def restore_seed_caps_and_agents(self) -> None:
        with self._connect() as conn:
            for node_id, cap in SEED_CAPS.items():
                conn.execute(
                    "UPDATE governance.budget_nodes SET cap_cents = %s WHERE id = %s",
                    (cap, node_id),
                )
            conn.execute("UPDATE governance.agents SET status = 'active'")
            conn.commit()

    def clear_audit(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM governance.audit_events")
            conn.execute("UPDATE governance.chain_head SET hash = %s WHERE id = 1", (GENESIS,))
            conn.commit()

    def clear_invocations(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM governance.invocations")
            conn.commit()
