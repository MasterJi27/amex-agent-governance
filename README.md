# Governance Layer for Financial Agents

**Jab AI agents paisa chhoo sakte hain, bank ko ek gate chahiye. Har airline, hotel aur notify call pehle is gate se guzarti hai — permission, budget, emergency stop, audit. Ye wahi gate hai.**

## Purpose — ye kya solve karta hai?

As autonomous AI agents multiply across financial services, even well-designed
agents create systemic risk at scale: one mispriced rebooking loop can burn a
budget in minutes, with no record of who allowed what. This project is the
**safety infrastructure that lets a bank deploy fleets of agents
responsibly** — granular per-agent permissions, real-time spend caps, instant
revocation, a hash-chained action log, and an emergency stop that halts the
entire fleet. Without it, autonomy is a liability. With it, autonomy is shippable.

What the judge remembers in 90 seconds:

1. **Permission dikhta hai** — OPA allowlist: `rebook_flight` allow,
   `wire_funds` deny. Matrix screen pe, version ke saath.
2. **Paisa rukta hai** — ₹18,000 hold tree pe dikhta hai; stop pe release,
   committed 0. Do agents, ek fleet cap → `FLEET_CAP`, not `AGENT_CAP`.
3. **Deny ke baad process hai** — human reason + doosra approver → targeted cap
   raise → retry allow. Audit me sab likha hai.

## Demo (Actor dropdown + buttons, order me)

```bash
cd deploy
docker compose up --build
```

Dashboard: **http://localhost:5173** · API: http://localhost:8001

1. Actor `refund-agent` + `1. Allow` → rebook `ACTION_NOT_ALLOWED`
   (notify-only agent — least privilege, live).
2. `Preview a ₹10,000 cap` → "yahi rukega, save nahi hoga."
3. `2. Cap at ₹10,000` → ₹18,000 deny, paisa move nahi hua.
4. `1. Allow` → hold fleet+team+agent pe, phir committed. Ledger row
   `reserved → committed`.
5. `3. Start a live booking`, beech me **Emergency stop** → generation badli,
   hold ₹0, committed 0, incident sheet khud bani.
6. `5. Two agents, one fleet cap` → doosra agent `FLEET_CAP`.
7. `6. Try wire_funds` → allowlist deny, airline tak call gayi hi nahi.
8. Cap-deny ke baad **Overrides** section: reason + naam → pending →
   doosre naam se approve → cap badha → retry allow. Same naam pe 400.

## How it works (one call, 8 steps)

```
Agent -> MCP rebook_flight -> gate
  1. agent exists? revoked / team-stopped / fleet-stopped?
  2. OPA allow? (rego-1: rebook allow, wire_funds deny)
  3. VELOCITY: 10 min me 2 allows ke baad teesra deny
  4. Redis Lua: fleet+team+agent hold EK atomic script me (FLEET/TEAM/AGENT_CAP)
  5. reservation row (fleet + agent generation ke saath)
  6. airline simulator (delay window = stop ka mauka)
  7. generation re-check: stale/fail -> release, committed 0
  8. commit + hash-chain audit row (decision, reason, latency, policy version)
```

- **Fail-closed**: OPA down → `POLICY_UNAVAILABLE`, Redis down →
  `CONTROL_PLANE_UNAVAILABLE`. Airline kabhi call nahi hota.
- **Async core**: `redis.asyncio` + gathered reads + bounded fan-out
  (pool 400, max 20 concurrent views). Loop-aware client rebuild.
- **Override ≠ bypass**: human sirf cap raise kar sakta hai (maker-checker),
  allowlist kabhi nahi. Full design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## API

| Method | Path | Use |
|---|---|---|
| GET | /v1/health · /v1/ready · /v1/metrics | liveness · pg+redis+opa · Prometheus |
| GET | /v1/budget | tree caps/spent + generation + teams + bookings |
| PUT | /v1/budget/{node}/cap | set cap live |
| POST | /v1/budget/preview | dry run, kuch save nahi |
| POST | /v1/teams/{t}/stop · /v1/fleet/emergency-stop · /v1/fleet/resume | blast radius |
| POST | /v1/agents/{id}/revoke · /restore | per-agent kill switch |
| GET | /v1/policy · /v1/policy/diff | OPA matrix · rego-1 vs rego-2 review |
| GET | /v1/ledger · /v1/audit · /v1/audit/verify · /v1/audit/export | holds · chain · verify · .jsonl |
| POST | /v1/overrides · /v1/overrides/{id}/approve · GET /v1/overrides | maker-checker cap raise |
| POST | /v1/demo/scenarios/{allow,over-cap,team-stop,fleet-cap,wire-deny,inflight-stop} | one-click scenes (?agent_id= on allow/over-cap) |
| POST | /v1/demo/live-rebook · /v1/demo/reset · /v1/demo/tamper | 8s hold · clean · chain break |

## File structure

```
fleet-governance/
├── gateway/            # the gate: pipeline, server (MCP+REST), db, redis_store, opa_client, simulators
│   ├── pipeline.py     # allow → velocity → Lua hold → adapter → generation check → commit/release
│   ├── reserve.lua     # atomic fleet/team/agent hold, failing node ka naam
│   └── schema.sql      # budget nodes, agents, control, reservations, audit chain, overrides
├── policies/           # authz.rego (rego-1) + authz.v2.rego.example (tighter, bina code)
├── web/                # fleet dashboard (React + Vite): generation, tree, blast radius, ledger, audit
├── tests/              # 17 integration tests — Docker me chalte hain (neeche dekho)
├── deploy/             # docker-compose.yml + Dockerfile (pg 5433, redis 6380, opa 8181, api 8001, web 5173)
├── docs/               # ARCHITECTURE.md — full system design
├── bench.py            # read-path load (stdlib only)
└── requirements.txt    # uvicorn, redis, psycopg, httpx, pytest, mcp
```

## Tests & load

```bash
# integration suite (needs the stack up):
docker compose -f deploy/docker-compose.yml run --rm --no-deps \
  -v "$PWD/tests:/tests" governance \
  python -m pytest /tests -q -o pythonpath="/app" -p no:cacheprovider
# 17 passed — caps, fleet-cap, e-stop, idempotency, velocity isolation,
# audit tamper, rebuild, overrides, OPA-down.

python bench.py 50 50   # read-path: budget p95 ~950ms burst, zero 500s
```

## Tech

Python (MCP + Starlette) · Open Policy Agent · Redis 7 (Lua) · Postgres 16 ·
React + Vite · Docker Compose.
