# Governance — system design (standalone, no concierge calls)

## What it is
The gate every money-touching agent call passes: OPA allowlist -> velocity
check -> atomic budget hold (fleet/team/agent in one Redis Lua script) ->
airline simulator -> generation check -> commit or release. Every decision is
a hash-chained audit row. Emergency stop moves the generation clock so
in-flight holds release and nothing commits.

## Flow (one rebook_flight, Rs 18,000)
```
Agent -> MCP rebook_flight -> pipeline._run
  1. agent exists? revoked / team-stopped / fleet-stopped?
  2. OPA allow? (rego-1: rebook allow, wire_funds deny)
  3. VELOCITY: >=2 allows in 10 min? -> deny VELOCITY
  4. Redis Lua reserve(fleet, team, agent) -> FLEET_CAP / TEAM_CAP / AGENT_CAP
  5. reservation row (fleet_generation, agent_generation)
  6. await airline simulator (delay window = stop opportunity)
  7. re-read stopped + generations; stale or simulator-fail -> release
  8. commit reservation + audit row (decision, reason, latency, policy_version)
```

## Components (all inside governance/)
- `gateway/server.py` — MCP tools + REST: budget, caps, revoke/restore,
  team stop, preview, emergency-stop/resume, audit, verify, export, ready,
  metrics, policy, demo scenarios.
- `gateway/pipeline.py` — the gate order above. Idempotency via invocations
  table (claim -> run -> finish; concurrent dupes wait for the winner).
- `gateway/redis_store.py` + `reserve.lua` / `release.lua` — atomic hold
  across the whole path with the exact failing node named (FLEET vs TEAM vs
  AGENT is decided here, on screen the banner quotes it).
- `gateway/db.py` + `schema.sql` — budget nodes, agents, control
  (generation+stopped), reservations, invocations, hash-chained audit.
- `gateway/opa_client.py` + `policies/authz.rego` — allowlist. `.v2.example`
  shows a tighter bundle without code change.
- `gateway/simulators.py` — airline/hotel/notify stand-ins with counters and
  injectable delay/failure for the stop demo.
- `web/` — generation clock, stage banner, budget tree with holds, blast
  radius (team vs fleet), OPA matrix, incident sheet, audit table + verify.

## API table
| Method | Path | Use |
|---|---|---|
| GET | /v1/health | liveness |
| GET | /v1/ready | pg + redis + opa checks (K8s readiness) |
| GET | /v1/metrics | Prometheus text (attempts, committed, audit total, generation, stopped) |
| GET | /v1/budget | tree caps/spent/remaining + generation + teams + bookings |
| PUT | /v1/budget/{node}/cap | set cap live |
| POST | /v1/budget/preview | dry run: would Rs 18,000 stop? nothing saved |
| POST | /v1/teams/{team}/stop | blast radius: one team only |
| POST | /v1/fleet/emergency-stop | generation++ , fleet stopped |
| POST | /v1/fleet/resume | resume, clear team stops (generation kept) |
| POST | /v1/agents/{id}/revoke|restore | per-agent kill switch |
| GET | /v1/policy | OPA matrix + policy_version |
| GET | /v1/policy/diff | live rego-1 vs rego-2 example, restart-free review |
| GET | /v1/ledger?limit= | holds vs committed vs released, newest first |
| GET | /v1/audit?limit= | tail with payloads |
| GET | /v1/audit/verify | chain ok? broken_seq? |
| GET | /v1/audit/export?limit= | .jsonl download for SIEM |
| POST | /v1/demo/scenarios/{allow,over-cap,team-stop,fleet-cap,wire-deny,inflight-stop} | one-click scenes; allow/over-cap take `?agent_id=` (404 unknown agent) |
| POST | /v1/demo/live-rebook | 8s hold you can stop mid-flight |
| POST | /v1/demo/reset | restore seed caps, clear holds |
| POST | /v1/demo/tamper | flip one audit byte (verify goes red) |
| GET | /v1/demo/bookings | simulator counters |

## Failure modes (honest)
- OPA down -> POLICY_UNAVAILABLE, airline never called.
- Redis down -> CONTROL_PLANE_UNAVAILABLE, airline never called. Demo routes
  refuse the same way (no 500): `demo_reset()` pings first, routes answer the
  deny JSON. Verified live: ready shows redis down, live-rebook denies,
  adapter `not_called`.
- Repeated allows trip VELOCITY (max 2 spend actions / agent / 10 min, counted
  off the audit trail, never cleared by reset). Pristine numbers need
  `docker compose down -v`.
- Stop mid-hold -> STALE_GENERATION, hold released, committed stays 0.
- Tampered audit -> verify names broken seq; reset does not repair it.
- Fleet-cap scene: first agent commits Rs 18k of Rs 20k fleet, second gets
  FLEET_CAP (not AGENT_CAP) — the tree difference on screen.

## Scale notes
- Gateway stateless except Redis + Postgres. Lua reserve is atomic per call;
  idempotency keys make retries safe. Velocity counts recent allows in Python
  so a tampered row degrades to "skip row", never to "gate down".
- Scrape /v1/metrics, alert on gov_stopped==1 and VELOCITY spike.
- Load (`python bench.py`, local Docker, read-path only): budget p95 ~290ms
  at 20 req, ~950ms at 50-burst (sync era: ~1460ms), zero errors. Async
  client + gathered reads (pg in threads, redis batched) + bounded fan-out
  (pool 400, semaphore 20, pg ~60 conns max). Two burst bugs found by
  measuring, both fixed: (1) demo routes 500d on redis-down (now deny JSON);
  (2) pool exhaustion + cross-loop streams (asyncio connections bind to the
  dialing loop; each pytest is a new loop — the store rebuilds its client on
  loop change and `bootstrap()` drops the pool). Single worker is deliberate
  (simulator counters live in-process); scale-out must externalize them.
  Full integration suite runs green in Docker: 17 passed.

## 3-minute college demo script
0. Actor dropdown: `refund-agent` + button 1 -> rebook `ACTION_NOT_ALLOWED`
   (notify-only agent, least privilege live). Back to `travel-concierge`.
1. Open 5173. `Preview Rs 10,000 cap` -> "would stop at travel-concierge,
   not saved." Then `2. Cap at Rs 10,000` -> deny before money moves.
2. `1. Allow Rs 18,000` -> hold on fleet+team+agent, then committed 1.
   Export audit (.jsonl) — "compliance download".
3. `3. Start a live booking` -> hold sits 8s. Mid-hold press Emergency stop:
   generation moves, hold -> Rs 0, committed 0, incident sheet appears.
4. `5. Two agents, one fleet cap` -> banner: "Blocked by the fleet cap —
   not the agent cap."
5. `6. Try wire_funds (denied)` -> `ACTION_NOT_ALLOWED`, no hold, no airline.
   Punch line: "OPA ne haan/naa bola, ped ne paise roke, chain ne gawahi di."
