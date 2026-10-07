CREATE SCHEMA IF NOT EXISTS governance;

CREATE TABLE IF NOT EXISTS governance.budget_nodes (
  id TEXT PRIMARY KEY,
  parent_id TEXT,
  cap_cents INTEGER NOT NULL,
  CHECK (cap_cents >= 0)
);

CREATE TABLE IF NOT EXISTS governance.agents (
  id TEXT PRIMARY KEY,
  team_id TEXT NOT NULL,
  status TEXT NOT NULL,
  generation INTEGER NOT NULL DEFAULT 0,
  CHECK (status IN ('active', 'revoked'))
);

CREATE TABLE IF NOT EXISTS governance.control (
  id INTEGER PRIMARY KEY,
  fleet_generation INTEGER NOT NULL,
  stopped BOOLEAN NOT NULL
);

CREATE TABLE IF NOT EXISTS governance.reservations (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  action TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  path TEXT[] NOT NULL,
  fleet_generation INTEGER NOT NULL,
  agent_generation INTEGER NOT NULL,
  state TEXT NOT NULL,
  CHECK (state IN ('reserved', 'committed', 'released'))
);

CREATE TABLE IF NOT EXISTS governance.invocations (
  idempotency_key TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  result_json TEXT,
  CHECK (status IN ('processing', 'done'))
);

CREATE TABLE IF NOT EXISTS governance.overrides (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  action TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  resource TEXT NOT NULL,
  node_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  approved_by TEXT,
  status TEXT NOT NULL,
  result_json TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (status IN ('pending', 'approved', 'rejected', 'failed'))
);

CREATE TABLE IF NOT EXISTS governance.chain_head (
  id INTEGER PRIMARY KEY,
  hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS governance.audit_events (
  seq BIGSERIAL PRIMARY KEY,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS audit_events_seq_desc ON governance.audit_events (seq DESC);

INSERT INTO governance.control (id, fleet_generation, stopped)
VALUES (1, 0, FALSE)
ON CONFLICT (id) DO NOTHING;

INSERT INTO governance.chain_head (id, hash)
VALUES (1, '0000000000000000000000000000000000000000000000000000000000000000')
ON CONFLICT (id) DO NOTHING;

INSERT INTO governance.budget_nodes (id, parent_id, cap_cents) VALUES
  ('fleet', NULL, 200000),
  ('travel-benefits', 'fleet', 80000),
  ('card-servicing', 'fleet', 100000),
  ('travel-concierge', 'travel-benefits', 50000),
  ('claims-assistant', 'card-servicing', 50000),
  ('refund-agent', 'card-servicing', 30000)
ON CONFLICT (id) DO NOTHING;

INSERT INTO governance.agents (id, team_id, status, generation) VALUES
  ('travel-concierge', 'travel-benefits', 'active', 0),
  ('refund-agent', 'card-servicing', 'active', 0),
  ('claims-assistant', 'card-servicing', 'active', 0)
ON CONFLICT (id) DO NOTHING;
