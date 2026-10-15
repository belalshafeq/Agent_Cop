# AgentTrafficCop

[![PyPI version](https://img.shields.io/pypi/v/agenttrafficcop.svg)](https://pypi.org/project/agenttrafficcop/)
[![Python versions](https://img.shields.io/pypi/pyversions/agenttrafficcop.svg)](https://pypi.org/project/agenttrafficcop/)
[![CI](https://github.com/belalshafeq/Agent_Cop/actions/workflows/ci.yml/badge.svg)](https://github.com/belalshafeq/Agent_Cop/actions/workflows/ci.yml)

A zero-config **Multi-Agent Orchestration & Governance Middleware**.

Sits between communicating AI agents, validates every handoff against a
strict schema, fingerprints the payload with SHA-256, and trips a
circuit breaker before an infinite loop burns your budget.

## What It Does

- **Schema shield** — every message validated against a strict Pydantic v2 contract.
- **Loop detection** — SHA-256 payload hashing catches identical and cyclic (A↔B) loops.
- **Circuit breaker** — step limits, consecutive-repeat limits, cycle limits.
- **Structured errors** — HTTP 422 with stable error codes and diagnostics.
- **Bearer authentication** — constant-time API key comparison.
- **Bounded memory** — LRU-capped sessions, fixed-length hash history.
- **Redis-backed sessions** — optional; survives restarts, scales horizontally.
- **Prometheus metrics** — `/metrics` endpoint, zero external dependencies.
- **Audit log** — durable JSON Lines record of every governance event.
- **Admin API** — list, inspect, and clean up active sessions.
- **Session TTL** — optional auto-expiry of idle in-memory sessions.
- **Usage metering** — per-API-key message counting and tier limits.
- **LangGraph integration** — wrap any node in one line.
- **194 automated tests** — the whole stack, verified in CI on every push.

## See It In Action

Two agents get stuck in a loop. Without AgentTrafficCop, nothing stops them.
With it, the breaker trips in seconds.

```bash
# Terminal 1: start the middleware
python run.py

# Terminal 2: run the demo
python demo.py
```

Output:

```
════════════════════════════════════════════════════════════════════════
RUN 1: WITHOUT AgentTrafficCop
════════════════════════════════════════════════════════════════════════
Two agents loop. Nothing stops them. Watch the cost.

  Coder     → Reviewer : {'diff': 'rename foo to bar in app.py', ...}
  Reviewer  → Coder    : {'feedback': 'please add a docstring', ...}
  Coder     → Reviewer : {'diff': 'add null check in handle()', ...}
  Reviewer  → Coder    : {'feedback': 'please handle the empty case', ...}
  ... [repeats for 12 rounds] ...

  No breaker. Loop ran 12 rounds (24 messages).
  Estimated cost: $0.10  (fake model, for illustration)

════════════════════════════════════════════════════════════════════════
RUN 2: WITH AgentTrafficCop
════════════════════════════════════════════════════════════════════════
Same agents. Every message goes through the cop at http://localhost:8000.

  Coder    → Reviewer : {...}  [APPROVED step=1]
  Reviewer → Coder    : {...}  [APPROVED step=2]
  Coder    → Reviewer : {...}  [APPROVED step=3]
  Reviewer → Coder    : {...}  [APPROVED step=4]
  Coder    → Reviewer : {...}  [APPROVED step=5]
  Reviewer → Coder    : {...}  [APPROVED step=6]
  Coder    → Reviewer : {...}  [APPROVED step=7]
  Reviewer → Coder    : {...}  [CYCLIC_PAYLOAD_LOOP]

  🛑 CIRCUIT BREAKER TRIPPED
  Error code: CYCLIC_PAYLOAD_LOOP
  Session:    demo-loop
    step: 8
    cycle_length: 2
    cycle_repeats: 3
    max_cycle_repeats: 3

  Session halted after 8 messages. Estimated cost: $0.03
  Saved vs Run 1: $0.07

Demo complete.
```

The demo is deterministic and needs no LLM API key — the two agents
are simulated so the loop trips the same way every run.

## Install & Run

```bash
pip install -r requirements.txt
cp .env.example .env          # then edit .env and set ATC_API_KEYS
python run.py
```

Server: `http://localhost:8000` · Docs: `http://localhost:8000/docs`

## Try It

First, generate an API key and put it in `.env`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Set `ATC_API_KEYS=<the-key>` in `.env`, then restart the server.

### Health check (no auth)

```bash
curl http://localhost:8000/health
```

### Approve a message

```bash
curl -X POST http://localhost:8000/v1/route \
  -H 'content-type: application/json' \
  -H "authorization: Bearer $ATC_KEY" \
  -d '{
    "session_id": "run-42",
    "sender": "coder",
    "recipient": "reviewer",
    "intent": "handoff",
    "payload": {"diff": "fix typo", "file": "app.py"}
  }'
```

Response:

```json
{
  "approved": true,
  "session_id": "run-42",
  "step": 1,
  "payload_hash": "3a7f...",
  "message": "Message approved and forwarded."
}
```

### Trip the breaker

Send the same payload 3 more times in the same session:

```bash
for i in 1 2 3; do
  curl -s -X POST http://localhost:8000/v1/route \
    -H 'content-type: application/json' \
    -H "authorization: Bearer $ATC_KEY" \
    -d '{"session_id":"run-42","sender":"coder","recipient":"reviewer","intent":"handoff","payload":{"diff":"fix typo","file":"app.py"}}'
  echo
done
```

Fourth call returns HTTP 422:

```json
{
  "error": "REPEATED_PAYLOAD_LOOP",
  "message": "Session 'run-42' produced identical payload hash 3 times consecutively.",
  "session_id": "run-42",
  "diagnostics": {
    "step": 4,
    "repeated_hash": "3a7f...",
    "consecutive_repeats": 3,
    "max_repeated_hashes": 3
  }
}
```

### Reset a session

```bash
curl -X DELETE http://localhost:8000/v1/sessions/run-42 \
  -H "authorization: Bearer $ATC_KEY"
```

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/v1/route` | Bearer | Evaluate and approve an inter-agent message |
| `GET` | `/health` | None | Liveness and uptime probe |
| `GET` | `/v1/usage` | Bearer | Return this key's tier and monthly usage |
| `GET` | `/v1/sessions` | Bearer | List active sessions (admin) |
| `GET` | `/v1/sessions/{id}` | Bearer | Inspect one session (admin) |
| `DELETE` | `/v1/sessions/{id}` | Bearer | Reset one session |
| `POST` | `/v1/admin/cleanup` | Bearer | Expire idle sessions in bulk |
| `GET` | `/metrics` | None | Prometheus metrics endpoint |

## LangGraph Support

AgentTrafficCop ships with a first-class
[LangGraph](https://langchain-ai.github.io/langgraph/) adapter. Wrap any
node in your graph and every handoff passes through the cop:

```python
from agenttrafficcop import Guard
from agenttrafficcop.langgraph import guarded_node

guard = Guard(url="http://localhost:8000", api_key="your-key")

graph.add_node(
    "reviewer",
    guarded_node(
        reviewer_fn,
        guard=guard,
        sender="coder",
        recipient="reviewer",
        session_id=lambda s: s["run_id"],
        payload=lambda s: {"draft": s["draft"]},
    ),
)
```

Install the extra:

```bash
pip install -e "./sdk[langgraph]"
```

Run the full working example:

```bash
# Terminal 1
python run.py

# Terminal 2
export ATC_KEY=$(grep '^ATC_API_KEYS=' .env | cut -d= -f2-)
python examples/langgraph_demo.py
```

The demo builds a real LangGraph with a coder and reviewer node and
watches the breaker trip around step 7. See
[`examples/langgraph_demo.py`](examples/langgraph_demo.py) for the full
source.

## Wiring It Into Your Orchestrator

AgentTrafficCop does not call your agents. **You** call AgentTrafficCop
before each handoff:

```python
import httpx

ATC_URL = "http://localhost:8000"
ATC_KEY = "your-api-key"

async def safe_handoff(sender, recipient, payload, session_id):
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{ATC_URL}/v1/route",
            headers={"authorization": f"Bearer {ATC_KEY}"},
            json={
                "session_id": session_id,
                "sender": sender,
                "recipient": recipient,
                "intent": "handoff",
                "payload": payload,
            },
        )
    if r.status_code == 422:
        err = r.json()
        raise RuntimeError(f"Governance halted session: {err['error']}")
    if r.status_code == 401:
        raise RuntimeError("Invalid API key")
    return r.json()
```

Or use the Python SDK for a cleaner interface:

```python
from agenttrafficcop import Guard, LoopDetected

guard = Guard(url=ATC_URL, api_key=ATC_KEY)

try:
    result = await guard.check(
        session_id="run-42",
        sender="coder",
        recipient="reviewer",
        payload={"diff": "fix typo"},
    )
    print("approved at step", result.step)
except LoopDetected as e:
    print("loop caught:", e.error_code, e.diagnostics)
```

## Tiers & Usage

Every API key is assigned a tier, and every tier has a monthly message
limit. The server counts messages per key and rejects further requests
once a key exceeds its limit.

### Default tiers

| Tier | Monthly message limit |
|---|---|
| `free` | 10,000 |
| `pro` | 250,000 |
| `team` | 2,000,000 |
| `enterprise` | Unlimited (0 in config means no cap) |

Limits are configured via `ATC_TIER_*_LIMIT` in `.env`.

### Assigning tiers to keys

By default, every key listed in `ATC_API_KEYS` is treated as the `team`
tier. To assign a specific tier to a key, list it in one of the
tier-specific variables:

```bash
# In .env
ATC_FREE_API_KEYS=freekey1,freekey2
ATC_PRO_API_KEYS=prokey1
ATC_TEAM_API_KEYS=teamkey1
ATC_ENTERPRISE_API_KEYS=acmecorpkey
```

If a key appears in multiple tiers, the **most permissive** tier wins
(enterprise > team > pro > free).

### Checking usage

Any key can query its own usage:

```bash
curl http://localhost:8000/v1/usage \
  -H "authorization: Bearer $ATC_KEY"
```

Response:

```json
{
  "api_key_fingerprint": "dee50c67",
  "tier": "team",
  "messages_this_month": 4217,
  "tier_limit": 2000000,
  "remaining": 1995783,
  "reset_at": "2026-11-01T00:00:00Z",
  "enforcement_enabled": true
}
```

The endpoint never returns the raw API key — only its 8-character
truncated SHA-256 fingerprint.

### What counts toward the limit

Only **approved** messages count. Rejected messages do **not** increment
the counter:

| Outcome | Counts against limit? |
|---|---|
| `200 OK` (approved) | ✅ Yes |
| `422 SCHEMA_VALIDATION_FAILED` | ❌ No |
| `422 REPEATED_PAYLOAD_LOOP` | ❌ No |
| `422 CYCLIC_PAYLOAD_LOOP` | ❌ No |
| `422 STEP_LIMIT_EXCEEDED` | ❌ No |
| `401` (bad auth) | ❌ No |
| `429 RATE_LIMIT_EXCEEDED` | ❌ No |

This is deliberate. Governance rejections should never cost the customer.

### When a key exceeds its limit

The next request returns HTTP 429:

```json
{
  "error": "TIER_LIMIT_EXCEEDED",
  "message": "Tier 'free' limit of 10000 messages per month reached. Resets on 2026-11-01T00:00:00Z.",
  "session_id": null,
  "diagnostics": {
    "tier": "free",
    "used": 10000,
    "limit": 10000,
    "reset_at": "2026-11-01T00:00:00Z"
  }
}
```

The response also carries a `Retry-After` header with the number of
seconds until the next calendar month (UTC).

### Track-only mode

Set `ATC_USAGE_ENFORCEMENT_ENABLED=false` to run in track-only mode.
Messages are still counted and visible at `/v1/usage`, but no request
is ever rejected for exceeding a limit. Useful for dry runs before
opening a service to real traffic.

### Storage backend

Usage counters follow the same backend as sessions:

- **Redis** when `ATC_REDIS_URL` is set — counters are shared across
  replicas and survive restarts.
- **In-memory** otherwise — counters reset on restart.

Counters are keyed by `(api_key_fingerprint, year-month)`. A new month
starts at zero automatically — no explicit reset needed.

### Manually upgrading a customer

Until a Stripe integration exists, upgrading a customer is a two-step
operation:

1. Add their API key to the appropriate tier variable in `.env`:
   ```bash
   ATC_PRO_API_KEYS=existing_free_key,new_pro_key
   ```
2. Restart the server.

Their new limit takes effect immediately. No code changes, no
database migration, no dashboard required.

### What's NOT included yet

- **Stripe integration** — no payment processing. Upgrades are manual.
- **Self-serve tier changes** — a customer cannot upgrade themselves.
- **Invoices / receipts / tax** — handled outside the middleware.
- **Historical usage** — the API only returns the current month's count.

All four are future features, triggered by real customer demand.

## Metrics

AgentTrafficCop exposes Prometheus metrics at `GET /metrics`. No
external dependencies — the exposition format is generated directly.

### What's exposed

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `atc_requests_total` | counter | `endpoint`, `method`, `status` | Every HTTP request, by endpoint and outcome |
| `atc_request_duration_seconds` | histogram | `endpoint` | Request latency distribution |
| `atc_messages_approved_total` | counter | — | Total messages that passed governance |
| `atc_governance_trips_total` | counter | `reason` | Circuit breaker trips (`repeated_payload`, `cyclic_payload`, `step_limit`) |
| `atc_sessions_active` | gauge | — | Current active session count |
| `atc_uptime_seconds` | gauge | — | Process uptime |

**Cardinality is bounded.** Session IDs, API keys, and payload contents
never appear as labels — only fixed, low-cardinality values.

### Scraping with Prometheus

Add this to your `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: agenttrafficcop
    scrape_interval: 15s
    static_configs:
      - targets: ["your-atc-host:8000"]
    metrics_path: /metrics
```

No authentication is required for `/metrics`.

### Example PromQL queries

```promql
# Messages approved per second
rate(atc_messages_approved_total[1m])

# Trip rate, grouped by reason
rate(atc_governance_trips_total[5m])

# p95 latency of /v1/route
histogram_quantile(
  0.95,
  rate(atc_request_duration_seconds_bucket{endpoint="/v1/route"}[5m])
)

# HTTP error rate
sum(rate(atc_requests_total{status=~"4..|5.."}[5m]))
  /
sum(rate(atc_requests_total[5m]))
```

### Disabling metrics

Set `ATC_METRICS_ENABLED=false`. The `/metrics` endpoint returns a stub
body (`# metrics disabled`), and the middleware stops recording.

### Customizing the path

Set `ATC_METRICS_PATH=/internal/metrics` if `/metrics` clashes with
another service.

## Audit Log

AgentTrafficCop can persist every governance event to a JSON Lines
file for compliance, debugging, and post-incident analysis.

**Disabled by default.** Enable it by setting `ATC_AUDIT_LOG_ENABLED=true`
and `ATC_AUDIT_LOG_PATH=/path/to/audit.log`.

### What gets logged

Only **governance events** — not every approval.

| Event | Written when |
|---|---|
| `trip` | The circuit breaker fires |
| `reset` | A session is deleted |
| `auth_failed` | An API key is missing, malformed, or invalid |
| `schema_failed` | An incoming message fails Pydantic validation |
| `rate_limited` | A client exceeds the rate limit |

### File format

Every line is a complete JSON object. Newline-delimited.

```json
{"ts":"2026-10-12T10:47:05.954123Z","event":"trip","error_code":"REPEATED_PAYLOAD_LOOP","session_id":"audit-test","step":3,"diagnostics":{"repeated_hash":"5041bf...","consecutive_repeats":3},"api_key_fingerprint":"dee50c67"}
{"ts":"2026-10-12T10:47:06.011085Z","event":"reset","session_id":"audit-test","removed":true,"api_key_fingerprint":"dee50c67"}
```

**Security notes:**
- API keys are recorded as **fingerprints only** — never the raw key.
- Payload contents are **never** logged — only their SHA-256 hash.

### Querying the log

```bash
# Count trips today
grep '"event":"trip"' audit.log | wc -l

# Find all events for one session
grep '"session_id":"demo-loop"' audit.log

# Extract trip reasons with jq
jq -r 'select(.event == "trip") | .error_code' audit.log | sort | uniq -c
```

### Retention

Use `logrotate` in production:

```
/path/to/audit.log {
    daily
    rotate 90
    compress
    missingok
    notifempty
    copytruncate
}
```

## Session TTL

Sessions can expire automatically after a period of inactivity. This
is **opt-in** and applies to the in-memory backend only — Redis
sessions use Redis's own `EXPIRE` mechanism.

### Enabling TTL

```bash
# In .env
ATC_SESSION_TTL_SECONDS=3600    # expire idle sessions after 1 hour
```

Default is `0`, which disables TTL. Sessions then live until explicitly
reset or evicted by the LRU cap (`ATC_MAX_SESSIONS`).

### How it works

TTL uses **lazy eviction**: expiry is checked when a session is accessed,
not on a background timer.

- `get_or_create()` on an expired session treats it as brand new
- `get()` on an expired session returns `None`
- `record()` on an expired session starts fresh at step 1
- `list_sessions()` filters expired sessions from its output

### Recommended TTL by workload

| Workload | Recommended TTL |
|---|---|
| Interactive workflows | 300–3600 seconds |
| Batch processing | 86400 (24 hours) |
| Long-lived chat sessions | `0` or 604800 (7 days) |

## Admin API

Three endpoints for inspecting and managing active sessions.
All require Bearer authentication.

### `GET /v1/sessions`

List every active session, sorted by most-recently-active.

```bash
curl "http://localhost:8000/v1/sessions" \
  -H "authorization: Bearer $ATC_KEY"
```

Query parameters:

| Param | Default | Meaning |
|---|---|---|
| `limit` | 100 | Max sessions to return (1–10,000) |
| `offset` | 0 | Pagination offset |
| `min_step` | (none) | Only sessions with `step >= min_step` |

### `GET /v1/sessions/{session_id}`

Return a single session's full state, including the recent payload hashes.

Returns **404** if the session doesn't exist. Read-only — never creates
a session.

### `POST /v1/admin/cleanup`

Expire every session whose `last_seen` is older than `older_than_seconds`.

```bash
curl -X POST "http://localhost:8000/v1/admin/cleanup" \
  -H 'content-type: application/json' \
  -H "authorization: Bearer $ATC_KEY" \
  -d '{"older_than_seconds": 3600}'
```

Response: `{"expired": 12, "remaining": 35}`

Every expired session is written to the audit log as a `reset` event.

## Docker Deployment

Two compose files ship with this repo, for two environments.

### On a real Linux host (production)

```bash
docker compose up -d --build
```

Standard bridge networking. Works on Fly.io, Railway, AWS, VPS, bare metal.

### On GitHub Codespaces (development)

```bash
docker compose -f docker-compose.codespaces.yml up -d --build
```

Uses host networking because GitHub Codespaces' Docker daemon **cannot
create working bridge networks**. Every bridge interface comes up in
`NO-CARRIER` / `DOWN` state, so containers cannot reach each other.

Diagnostic:

```bash
ip link show type bridge
# → docker0: <NO-CARRIER,BROADCAST,MULTICAST,UP> ... state DOWN
```

**Rule of thumb:** if `ip link show type bridge` shows `NO-CARRIER`,
use the `.codespaces.yml` file.

## Development

### Running the tests

```bash
pip install -r requirements.txt
pip install -e ./sdk
pytest
```

The full suite runs in about 15 seconds. It covers hashing, security,
the circuit breaker, all HTTP routes, the metrics system, the audit
log, the Admin API, session TTL, usage metering, and the SDK.

**Test counts by module:**

| File | Tests |
|---|---|
| `tests/test_hashing.py` | 12 |
| `tests/test_security.py` | 19 |
| `tests/test_circuit_breaker.py` | 13 (+1 skip) |
| `tests/test_routes.py` | 24 |
| `tests/test_integration.py` | 7 |
| `tests/test_metrics.py` | 22 |
| `tests/test_middleware.py` | 5 |
| `tests/test_audit.py` | 15 |
| `tests/test_admin_api.py` | 17 |
| `tests/test_state_ttl.py` | 12 |
| `tests/test_usage.py` | 26 |
| `sdk/tests/test_client.py` | 15 |
| `sdk/tests/test_langgraph.py` | 7 |
| **Total** | **194 passed, 1 skipped** |

### Continuous Integration

Every push to `main` and every pull request runs the test suite via
GitHub Actions. See `.github/workflows/ci.yml`.

### Project layout

```
agenttrafficcop/
├── app/                          # server
│   ├── __init__.py
│   ├── config.py
│   ├── security.py
│   ├── schemas.py
│   ├── exceptions.py
│   ├── hashing.py
│   ├── state.py
│   ├── state_backends.py
│   ├── circuit_breaker.py
│   ├── logging_config.py
│   ├── middleware.py
│   ├── metrics.py
│   ├── audit.py
│   ├── usage.py
│   ├── routes.py
│   └── main.py
├── sdk/                          # Python client
│   ├── pyproject.toml
│   ├── README.md
│   ├── agenttrafficcop/
│   │   ├── __init__.py
│   │   ├── exceptions.py
│   │   ├── models.py
│   │   ├── client.py
│   │   ├── langgraph.py
│   │   └── py.typed
│   └── tests/
│       ├── conftest.py
│       ├── test_client.py
│       └── test_langgraph.py
├── examples/
│   └── langgraph_demo.py         # runnable LangGraph demo
├── landing/                      # static landing page
│   └── index.html
├── tests/                        # server test suite
│   ├── conftest.py
│   ├── test_hashing.py
│   ├── test_security.py
│   ├── test_circuit_breaker.py
│   ├── test_routes.py
│   ├── test_integration.py
│   ├── test_metrics.py
│   ├── test_middleware.py
│   ├── test_audit.py
│   ├── test_admin_api.py
│   ├── test_state_ttl.py
│   └── test_usage.py
├── demo.py                       # reference demo
├── demo_agents.py
├── run.py                        # server entrypoint
├── requirements.txt
├── pyproject.toml                # pytest config
├── Dockerfile
├── docker-compose.yml
├── docker-compose.codespaces.yml
├── .github/workflows/ci.yml
├── .env.example
├── .gitignore
└── README.md
```

## Configuration

All settings are read from environment variables (or `.env`):

| Env var | Default | Meaning |
|---|---|---|
| `ATC_HOST` | `0.0.0.0` | Bind address |
| `ATC_PORT` | `8000` | Bind port |
| `ATC_ENV` | `development` | `production` disables `/docs` |
| `ATC_LOG_LEVEL` | `INFO` | Log level |
| `ATC_API_KEYS` | (none) | Comma-separated Bearer keys. **Required.** Treated as `team` tier. |
| `ATC_FREE_API_KEYS` | (none) | Keys assigned to the `free` tier |
| `ATC_PRO_API_KEYS` | (none) | Keys assigned to the `pro` tier |
| `ATC_TEAM_API_KEYS` | (none) | Keys assigned to the `team` tier |
| `ATC_ENTERPRISE_API_KEYS` | (none) | Keys assigned to the `enterprise` tier |
| `ATC_TIER_FREE_LIMIT` | `10000` | Monthly message limit for the free tier |
| `ATC_TIER_PRO_LIMIT` | `250000` | Monthly limit for the pro tier |
| `ATC_TIER_TEAM_LIMIT` | `2000000` | Monthly limit for the team tier |
| `ATC_TIER_ENTERPRISE_LIMIT` | `0` | Monthly limit for enterprise (0 = unlimited) |
| `ATC_USAGE_ENFORCEMENT_ENABLED` | `true` | Reject requests when over tier limit |
| `ATC_USAGE_KEY_PREFIX` | `atc:usage:` | Redis key prefix for usage counters |
| `ATC_RATE_LIMIT_PER_MINUTE` | `120` | Per-client request limit |
| `ATC_MAX_BODY_BYTES` | `1048576` | Maximum request body size |
| `ATC_METRICS_ENABLED` | `true` | Enable `/metrics` endpoint |
| `ATC_METRICS_PATH` | `/metrics` | Path for the metrics endpoint |
| `ATC_AUDIT_LOG_ENABLED` | `false` | Enable audit log writing |
| `ATC_AUDIT_LOG_PATH` | (none) | File path for the audit log |
| `ATC_REDIS_URL` | (none) | If set, use Redis for sessions and usage |
| `ATC_REDIS_TTL_SECONDS` | `86400` | Session TTL in Redis |
| `ATC_REDIS_KEY_PREFIX` | `atc:` | Redis key namespace |
| `ATC_SESSION_TTL_SECONDS` | `0` | In-memory session TTL. `0` disables expiry. |
| `ATC_MAX_STEPS` | `25` | Hard cap on hops per session |
| `ATC_MAX_REPEATED_HASHES` | `3` | Consecutive identical payloads before trip |
| `ATC_MAX_CYCLE_LENGTH` | `2` | Max cycle length scanned |
| `ATC_MAX_CYCLE_REPEATS` | `3` | Full cycle repeats before trip |
| `ATC_HISTORY_WINDOW` | `16` | Hashes retained per session |
| `ATC_MAX_SESSIONS` | `10000` | LRU cap on concurrent sessions |

## Security Notes

- **Auth is fail-closed** — if no API keys are configured, every `/v1/route` call is rejected with 401.
- **Constant-time key comparison** — prevents timing attacks.
- **No secrets in logs** — only truncated SHA-256 fingerprints for correlation.
- **No `pickle` / `eval` / `exec`** — hashing uses canonical JSON.
- **Bounded memory** — sessions and hash histories are capped.
- **Bounded metric cardinality** — no user-controlled values in labels.
- **Body size limit** — rejects oversized requests before reading them.
- **HTTPS/TLS is your responsibility** — put this behind nginx, Caddy, or a cloud LB.

## Limitations (honest list)

- **In-memory backend is single-process.** Sessions and usage counters
  do not survive restarts and are not shared across replicas. Use Redis
  for multi-replica deployments.
- **Exact-hash loop detection only.** Semantically-equivalent-but-differently-worded
  loops are not caught. Adding embeddings is a future feature.
- **Rate limiting is per-process.** With multiple replicas, each has
  its own counter. A shared Redis-backed limiter is a future feature.
- **No multi-tenancy.** One API key list for all users; sessions are
  global. This is a future feature.
- **No self-serve tier changes.** Upgrades are manual (edit `.env` and
  restart). Stripe integration is a future feature.

## License

Unlicensed. Do what you want.