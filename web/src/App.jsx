import { useEffect, useState } from "react";

const API = import.meta.env.VITE_API_URL || "http://localhost:8001";

function rupees(amount) {
  return `₹${Math.round(amount || 0).toLocaleString("en-IN")}`;
}

const NODE_LABEL = {
  fleet: "Fleet",
  "card-servicing": "Card servicing",
  "claims-assistant": "Claims assistant",
  "travel-benefits": "Travel benefits",
  "travel-concierge": "Travel concierge · Ananya Sharma",
};

function heldCents(budget) {
  const node = (budget?.nodes || []).find((item) => item.id === "travel-concierge");
  return node?.spent_cents || 0;
}

function stageCopy(budget, audit, watch) {
  const held = heldCents(budget);
  const committed = budget?.bookings?.flight_committed ?? 0;
  const attempts = budget?.bookings?.flight_attempts ?? 0;
  const latest = audit[0]?.payload;
  if (watch === "live" && held > 0 && !budget?.stopped) {
    return {
      tone: "hold",
      title: `${rupees(held)} is on hold`,
      body: "The allowlist passed. Air India AI111 and BA173 for Ananya Sharma are waiting on the airline. Stop the fleet before it commits.",
    };
  }
  if (watch === "live" && budget?.stopped) {
    return {
      tone: "stop",
      title: "Stop landed",
      body: "The generation moved. This hold releases when the airline call comes back.",
    };
  }
  if (watch === "allow" && held > 0) {
    return {
      tone: "hold",
      title: `${rupees(held)} is on hold`,
      body: "The airline is confirming Ananya Sharma's reissue. This one is allowed to commit.",
    };
  }
  if (watch) {
    return { tone: "run", title: "Opening the gate", body: "Checking who this agent is, then whether ₹18,000 fits the tree." };
  }
  if (latest?.reason_code === "STALE_GENERATION" || latest?.reason_code === "EMERGENCY_STOP") {
    return {
      tone: "stop",
      title: "Released. Nothing was booked.",
      body: `The airline answer was thrown away. Attempts ${attempts}. Committed flights ${committed}.`,
    };
  }
  if (latest?.decision === "allow" && latest?.action === "rebook_flight") {
    return {
      tone: "ok",
      title: `Booked ${latest.resource?.flight_numbers || "the replacement"}`,
      body: `${rupees(latest.amount_cents)} stayed on the fleet, the travel-benefits team, and this agent. Committed flights ${committed}.`,
    };
  }
  if (latest?.reason_code === "TEAM_STOPPED") {
    return {
      tone: "stop",
      title: "Travel benefits is stopped. Card servicing is not.",
      body: "The reissue never reached the airline. Claims can still spend. Committed flights stayed 0.",
    };
  }
  if (latest?.reason_code === "AGENT_CAP") {
    return {
      tone: "deny",
      title: "Blocked by the ₹10,000 agent cap",
      body: "The ₹18,000 reissue never reserved money. Committed flights stayed 0.",
    };
  }
  if (latest?.reason_code === "FLEET_CAP") {
    return {
      tone: "deny",
      title: "Blocked by the fleet cap — not the agent cap",
      body: `Concierge already holds ${rupees(18000)} of a ₹20,000 fleet. ${latest?.agent_id || "The second agent"} still has room on its own cap, so the tree stops it with FLEET_CAP. First booking stays committed.`,
    };
  }
  if (latest?.reason_code === "ACTION_NOT_ALLOWED") {
    return {
      tone: "deny",
      title: "Forbidden action — OPA said no",
      body: "wire_funds is not on this agent's allowlist. Denied before any hold, before the airline. Policy decides, not the agent.",
    };
  }
  if (latest?.reason_code === "VELOCITY") {
    return {
      tone: "deny",
      title: "Speed limit hit — 3rd reissue in 10 minutes",
      body: "Caps have room, but the agent is spending too fast. VELOCITY stops it before the airline is called.",
    };
  }
  return { tone: "idle", title: "Fleet is quiet", body: "Start a booking. The hold shows up on the tree before anything is booked." };
}

function orderNodes(nodes) {
  const byParent = new Map();
  for (const node of nodes) {
    const key = node.parent_id || "root";
    const list = byParent.get(key) || [];
    list.push(node);
    byParent.set(key, list);
  }
  const ordered = [];
  const walk = (parent, depth) => {
    for (const node of byParent.get(parent) || []) {
      ordered.push({ ...node, depth });
      walk(node.id, depth + 1);
    }
  };
  walk("root", 0);
  return ordered;
}

export default function App() {
  const [budget, setBudget] = useState(null);
  const [audit, setAudit] = useState([]);
  const [chain, setChain] = useState(null);
  const [caps, setCaps] = useState({});
  const [error, setError] = useState("");
  const [watch, setWatch] = useState("");
  const [preview, setPreview] = useState(null);
  const [policy, setPolicy] = useState(null);
  const [ready, setReady] = useState(null);
  const [ledger, setLedger] = useState([]);
  const [poldiff, setPoldiff] = useState(null);
  const [overrides, setOverrides] = useState([]);
  const [ovReason, setOvReason] = useState("");
  const [ovBy, setOvBy] = useState("");

  async function refresh() {
    try {
      const [budgetRes, auditRes, verifyRes, policyRes, readyRes, ledgerRes, diffRes] = await Promise.all([
        fetch(`${API}/v1/budget`),
        fetch(`${API}/v1/audit?limit=50`),
        fetch(`${API}/v1/audit/verify`),
        fetch(`${API}/v1/policy`).catch(() => null),
        fetch(`${API}/v1/ready`).catch(() => null),
        fetch(`${API}/v1/ledger?limit=20`).catch(() => null),
        fetch(`${API}/v1/policy/diff`).catch(() => null),
      ]);
      const budgetBody = await budgetRes.json();
      const auditBody = await auditRes.json();
      const verifyBody = await verifyRes.json();
      setBudget(budgetBody);
      setAudit(auditBody.events || []);
      setChain(verifyBody);
      if (policyRes && policyRes.ok) setPolicy(await policyRes.json());
      if (readyRes && readyRes.ok) setReady(await readyRes.json());
      if (ledgerRes && ledgerRes.ok) {
        const ledgerBody = await ledgerRes.json();
        setLedger(ledgerBody.reservations || []);
      }
      if (diffRes && diffRes.ok) setPoldiff(await diffRes.json());
      const ovRes = await fetch(`${API}/v1/overrides?status=pending`).catch(() => null);
      if (ovRes && ovRes.ok) {
        const ovBody = await ovRes.json();
        setOverrides(ovBody.overrides || []);
      }
      setError("");
    } catch (err) {
      setError(String(err));
    }
  }

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 400);
    return () => clearInterval(timer);
  }, []);

  async function post(path, body) {
    await fetch(`${API}${path}`, {
      method: body ? "PUT" : "POST",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    await refresh();
  }

  async function previewCap() {
    const response = await fetch(`${API}/v1/budget/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node_id: "travel-concierge", cap_cents: 10000, amount_cents: 18000 }),
    });
    setPreview(await response.json());
  }

  const [actor, setActor] = useState("travel-concierge");

  async function play(kind) {
    setWatch(kind);
    let path = kind === "live" ? "/v1/demo/live-rebook" : `/v1/demo/scenarios/${kind}`;
    if (kind === "allow" || kind === "over-cap") path += `?agent_id=${actor}`;
    try {
      await fetch(`${API}${path}`, { method: "POST" });
    } catch (err) {
      setError(String(err));
    } finally {
      setWatch("");
      await refresh();
    }
  }

  function latestDeny() {
    const row = audit.find((r) => (r.payload?.reason_code || "").endsWith("_CAP"));
    return row ? row.payload : null;
  }

  function nodeFor(reason, agentId) {
    if (reason === "AGENT_CAP") return agentId;
    if (reason === "TEAM_CAP") {
      const agent = (budget?.agents || []).find((a) => a.id === agentId);
      return agent ? agent.team_id : "";
    }
    if (reason === "FLEET_CAP") return "fleet";
    return "";
  }

  async function proposeOverride() {
    const deny = latestDeny();
    if (!deny) {
      setError("Koi cap-deny nahi hai — pehle 2. Cap wala button dabao.");
      return;
    }
    const response = await fetch(`${API}/v1/overrides`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        agent_id: deny.agent_id,
        action: deny.action,
        amount_cents: deny.amount_cents,
        resource: deny.resource || {},
        node_id: nodeFor(deny.reason_code, deny.agent_id),
        reason: ovReason,
        requested_by: ovBy,
      }),
    });
    const body = await response.json();
    if (!body.ok) setError(body.error || "Override propose nahi hua");
    else {
      setOvReason("");
      setError("");
    }
    await refresh();
  }

  async function approveOverride(id, approvedBy) {
    const response = await fetch(`${API}/v1/overrides/${id}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approved_by: approvedBy }),
    });
    const body = await response.json();
    if (!body.ok) setError(body.error || "Approve nahi hua");
    else setError("");
    await refresh();
  }

  async function saveCap(nodeId) {
    await fetch(`${API}/v1/budget/${nodeId}/cap`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cap_cents: Number(caps[nodeId]) }),
    });
    await refresh();
  }

  const nodes = budget ? orderNodes(budget.nodes) : [];
  const stage = stageCopy(budget, audit, watch);
  const pathIds = new Set(["fleet", "travel-benefits", "travel-concierge"]);

  return (
    <main>
      <header>
        <div>
          <h1>Fleet control</h1>
          <p>Every airline, hotel, and notify call has to pass this gate.</p>
        </div>
        <div className="generation">
          <span>Generation</span>
          <strong>{budget ? budget.fleet_generation : "–"}</strong>
          <span>{budget?.stopped ? "Stopped" : "Running"}</span>
          <span>{ready ? (ready.ready ? "● ready (pg+redis+opa)" : "○ not ready") : ""}</span>
        </div>
      </header>
      {error ? <p>{error}</p> : null}
      <section className={`stage ${stage.tone}`}>
        <div>
          <p className="route-chip">Ananya Sharma · Platinum ···· 4429 · DEL T3 – LHR – JFK T8 · AI111/BA173 · ₹18,000</p>
          <h2>{stage.title}</h2>
          <p>{stage.body}</p>
        </div>
        <button className="stop big" onClick={() => post("/v1/fleet/emergency-stop")}>
          Emergency stop
        </button>
      </section>
      <div className="row">
        <label>
          Actor{" "}
          <select aria-label="Scenario actor" value={actor} onChange={(e) => setActor(e.target.value)}>
            {(budget?.agents?.length ? budget.agents : [{ id: actor }]).map((a) => (
              <option key={a.id} value={a.id}>
                {a.id}
              </option>
            ))}
          </select>
        </label>
        <button disabled={Boolean(watch)} onClick={() => play("allow")}>1. Allow a ₹18,000 reissue</button>
        <button disabled={Boolean(watch)} onClick={() => play("over-cap")}>2. Cap at ₹10,000</button>
        <button disabled={Boolean(watch)} onClick={() => play("live")}>3. Start a live booking</button>
        <button disabled={Boolean(watch)} onClick={() => play("team-stop")}>4. Stop only travel benefits</button>
        <button disabled={Boolean(watch)} onClick={() => play("fleet-cap")}>5. Two agents, one fleet cap</button>
        <button disabled={Boolean(watch)} onClick={() => play("wire-deny")}>6. Try wire_funds (denied)</button>
        <button disabled={Boolean(watch)} onClick={previewCap}>Preview a ₹10,000 cap</button>
      </div>
      {preview ? (
        <p className={preview.ok ? "chain ok" : "chain bad"}>
          {preview.ok
            ? "Preview only. ₹18,000 would still pass. Nothing was saved."
            : `Preview only. ₹18,000 would stop at ${preview.node_id} (${preview.reason_code}). The cap was not saved.`}
        </p>
      ) : null}
      <div className="row">
        <button onClick={() => post("/v1/fleet/resume")}>Resume</button>
        <button className="ghost" onClick={() => post("/v1/demo/tamper")}>
          Corrupt one audit row
        </button>
        <a href={`${API}/v1/audit/export?limit=500`}>
          <button type="button">Export audit (.jsonl)</button>
        </a>
      </div>
      <p className="chain ok">APIs: GET /v1/policy · GET /v1/ready · GET /v1/metrics · GET /v1/audit/export</p>
      <section>
        <h2>Budget tree</h2>
        {nodes.map((node) => (
          <div className={`node ${pathIds.has(node.id) && node.spent_cents > 0 ? "hot" : ""}`} key={node.id} style={{ paddingLeft: node.depth * 18 }}>
            <strong>{NODE_LABEL[node.id] || node.id}</strong>
            <span>
              <i className="meter" aria-hidden="true">
                <b style={{ width: `${node.cap_cents ? Math.min(100, (node.spent_cents / node.cap_cents) * 100) : 0}%` }} />
              </i>
              Spent {rupees(node.spent_cents)}
            </span>
            <span>Left {rupees(node.remaining_cents)}</span>
            <span>
              <input
                aria-label={`${node.id} cap`}
                value={caps[node.id] ?? node.cap_cents}
                onChange={(event) => setCaps({ ...caps, [node.id]: event.target.value })}
              />
              <button onClick={() => saveCap(node.id)}>Save cap</button>
            </span>
          </div>
        ))}
      </section>
      <section>
        <h2>Blast radius — team stop vs fleet stop</h2>
        <p>
          Team stop sirf ek team rokta hai, fleet stop generation bada ke sab rokta hai.
          Ye panel isi project ke Redis flags se aata hai, concierge se nahi.
        </p>
        {(budget?.teams || []).map((team) => (
          <div className="agent" key={team.id}>
            <strong>{team.id}</strong>
            <span>{team.stopped ? "STOPPED — airline tak call nahi jayegi" : "running"}</span>
            <span>{budget?.stopped ? `fleet gen ${budget.fleet_generation} (stopped)` : `fleet gen ${budget?.fleet_generation}`}</span>
            <span>
              <button onClick={() => post(`/v1/teams/${team.id}/stop`)}>Stop only this team</button>
            </span>
          </div>
        ))}
      </section>
      <section>
        <h2>Agents</h2>
        {(budget?.agents || []).map((agent) => (
          <div className="agent" key={agent.id}>
            <strong>{agent.id}</strong>
            <span>{agent.team_id}</span>
            <span>
              {agent.status} · gen {agent.generation}
            </span>
            <span>
              {agent.status === "revoked" ? (
                <button onClick={() => post(`/v1/agents/${agent.id}/restore`)}>Restore</button>
              ) : (
                <button onClick={() => post(`/v1/agents/${agent.id}/revoke`)}>Revoke</button>
              )}
            </span>
          </div>
        ))}
        <p>Committed flight bookings: {budget?.bookings?.flight_committed ?? 0}</p>
      </section>
      <section>
        <h2>Overrides — deny ke baad ka process</h2>
        <p>Cap deny hota hai to human reason likh ke propose karta hai, doosra human approve karta hai. Tabhi us node ka cap spent+amount hota hai aur original call retry hoti hai. Allowlist override nahi hoti.</p>
        <div className="row">
          <input aria-label="Override reason" placeholder="Reason (e.g. stranded passenger, duty manager ok)" value={ovReason} onChange={(e) => setOvReason(e.target.value)} style={{ minWidth: 260 }} />
          <input aria-label="Requested by" placeholder="Requested by (name)" value={ovBy} onChange={(e) => setOvBy(e.target.value)} />
          <button disabled={Boolean(watch)} onClick={proposeOverride}>Propose override for latest deny</button>
        </div>
        {overrides.length === 0 ? (
          <p>No pending overrides.</p>
        ) : (
          overrides.map((o) => (
            <div className="agent" key={o.id}>
              <strong>{o.agent_id} · {rupees(o.amount_cents)}</strong>
              <span>
                {o.node_id} · {o.reason} · by {o.requested_by}
              </span>
              <span>
                <input aria-label={`Approve ${o.id}`} placeholder="Approve as (different name)" id={`ap-${o.id}`} />
              </span>
              <span>
                <button
                  onClick={() => {
                    const input = document.getElementById(`ap-${o.id}`);
                    approveOverride(o.id, input ? input.value : "");
                  }}
                >
                  Approve + retry
                </button>
              </span>
            </div>
          ))
        )}
      </section>
      <section>
        <h2>Policy — OPA says it directly ({policy?.policy_version || "rego-1"})</h2>
        <p>travel-concierge can rebook, but wire_funds is denied without any code change.</p>
        <table>
          <thead>
            <tr>
              <th>Agent</th>
              <th>Action</th>
              <th>OPA</th>
            </tr>
          </thead>
          <tbody>
            {(policy?.checks || [])
              .filter((c) => c.agent_id === "travel-concierge" && (c.action === "rebook_flight" || c.action === "wire_funds"))
              .map((c, i) => (
                <tr key={i}>
                  <td>{c.agent_id}</td>
                  <td>{c.action}</td>
                  <td className={c.allowed ? "allow" : "deny"}>{c.allowed ? "allow" : "deny"}</td>
                </tr>
              ))}
          </tbody>
        </table>
      </section>
      {budget?.stopped ? (
        <section>
          <h2>Incident sheet — auto-built from the chain, not hand-written</h2>
          <p>
            Agent travel-concierge · hold {rupees(heldCents(budget))} · fleet generation{" "}
            {budget.fleet_generation} · latest audit seq {audit[0]?.seq ?? "–"} (
            {audit[0]?.payload?.reason_code || audit[0]?.payload?.decision || "stopped"}) ·
            committed flights {budget?.bookings?.flight_committed ?? 0}.
          </p>
          <p>
            <a href={`${API}/v1/audit/export?limit=500`}>Download this incident (.jsonl)</a>
          </p>
        </section>
      ) : null}
      <section>
        <h2>Ledger — holds vs committed vs released ({ledger.length})</h2>
        {ledger.length === 0 ? (
          <p>No holds yet. Run button 1 or 3, then this table fills.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Hold</th>
                <th>Agent</th>
                <th>Amount</th>
                <th>State</th>
              </tr>
            </thead>
            <tbody>
              {ledger.map((r) => (
                <tr key={r.id}>
                  <td>{r.id}</td>
                  <td>{r.agent_id}</td>
                  <td>{rupees(r.amount_cents)}</td>
                  <td className={r.state === "committed" ? "allow" : r.state === "released" ? "deny" : ""}>{r.state}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      {poldiff ? (
        <section>
          <h2>Policy diff — {poldiff.live_version} live vs {poldiff.example_version}</h2>
          <table>
            <thead>
              <tr>
                <th>Agent</th>
                <th>Action</th>
                <th>Live</th>
                <th>rego-2</th>
              </tr>
            </thead>
            <tbody>
              {(poldiff.live || []).map((cell, i) => {
                const v2 = (poldiff.v2_example || [])[i];
                const flipped = v2 && cell.allowed !== v2.allowed;
                return (
                  <tr key={i}>
                    <td>{cell.agent_id}</td>
                    <td>{cell.action}</td>
                    <td className={cell.allowed ? "allow" : "deny"}>{String(cell.allowed)}</td>
                    <td className={v2?.allowed ? "allow" : "deny"}>
                      {String(v2?.allowed)}{flipped ? " ← flips" : ""}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </section>
      ) : null}
      <section>
        <h2>Audit chain</h2>
        <p className={chain == null ? "" : chain.ok ? "chain ok" : "chain bad"}>
          {chain == null ? "Checking the chain." : chain.ok ? "Chain intact" : `Chain broken at row ${chain.broken_seq}`}
        </p>
        <table>
          <thead>
            <tr>
              <th>Seq</th>
              <th>Action</th>
              <th>Amount</th>
              <th>Decision</th>
              <th>Reason</th>
              <th>ms</th>
            </tr>
          </thead>
          <tbody>
            {audit.map((row) => (
              <tr key={row.seq}>
                <td>{row.seq}</td>
                <td>{row.payload.action}</td>
                <td>{rupees(row.payload.amount_cents)}</td>
                <td className={row.payload.decision}>{row.payload.decision}</td>
                <td>{row.payload.reason_code}</td>
                <td>{row.payload.latency_ms}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </main>
  );
}
