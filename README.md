# AgentTrafficCop

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
- **LangGraph integration** — wrap any node in one line.

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

No authentication is required for `/metrics` — Prometheus scrapers
typically don't support Bearer auth, and metrics contain no sensitive
data (only aggregate counts and latencies).

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

Set `ATC_METRICS_ENABLED=false` in your environment. The `/metrics`
endpoint returns a stub body (`# metrics disabled`), and the middleware
stops recording.

### Customizing the path

Set `ATC_METRICS_PATH=/internal/metrics` if `/metrics` clashes with
another service.

### What's NOT exposed

Metrics never include session IDs, API key fingerprints, payload
contents, client IPs, or any user-controlled string. Unbounded
cardinality crashes Prometheus; bounded cardinality is the whole point.

## Docker Deployment

Two compose files ship with this repo, for two different environments.

### On a real Linux host (production)

```bash
docker compose up -d --build
```

Uses standard bridge networking. Works on Fly.io, Railway, AWS, VPS, bare metal.

### On GitHub Codespaces (development)

```bash
docker compose -f docker-compose.codespaces.yml up -d --build
```

Uses host networking because GitHub Codespaces' Docker daemon
**cannot create working bridge networks**. Every bridge interface on
Codespaces comes up in `NO-CARRIER` / `DOWN` state, so containers
attached to a bridge can never reach each other. This is a
Codespaces-level limitation, not a bug in this project.

Diagnostic signature of the Codespaces issue:

```bash
ip link show type bridge
# → docker0: <NO-CARRIER,BROADCAST,MULTICAST,UP> ... state DOWN
# → br-xxxxxxxx: <NO-CARRIER,BROADCAST,MULTICAST,UP> ... state DOWN
```

**Rule of thumb:** if `ip link show type bridge` shows `NO-CARRIER`,
you're on Codespaces (or a similar sandbox) and should use the
`.codespaces.yml` file.

## Development

### Running the tests

```bash
pip install -r requirements.txt
pytest
```

The full suite runs in about 1 second. It covers hashing, security,
the circuit breaker, all HTTP routes, the metrics system, and
end-to-end scenarios.

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
│   ├── routes.py
│   └── main.py
├── sdk/                          # Python client
│   ├── pyproject.toml
│   ├── README.md
│   └── agenttrafficcop/
│       ├── __init__.py
│       ├── exceptions.py
│       ├── models.py
│       ├── client.py
│       ├── langgraph.py
│       └── py.typed
├── examples/
│   └── langgraph_demo.py         # runnable LangGraph demo
├── tests/                        # pytest suite
│   ├── conftest.py
│   ├── test_hashing.py
│   ├── test_security.py
│   ├── test_circuit_breaker.py
│   ├── test_routes.py
│   ├── test_integration.py
│   ├── test_metrics.py
│   └── test_middleware.py
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
| `ATC_API_KEYS` | (none) | Comma-separated Bearer keys. **Required.** |
| `ATC_RATE_LIMIT_PER_MINUTE` | `120` | Per-client request limit |
| `ATC_MAX_BODY_BYTES` | `1048576` | Maximum request body size |
| `ATC_METRICS_ENABLED` | `true` | Enable `/metrics` endpoint |
| `ATC_METRICS_PATH` | `/metrics` | Path for the metrics endpoint |
| `ATC_REDIS_URL` | (none) | If set, use Redis for sessions |
| `ATC_REDIS_TTL_SECONDS` | `86400` | Session TTL in Redis |
| `ATC_REDIS_KEY_PREFIX` | `atc:` | Redis key namespace |
| `ATC_MAX_STEPS` | `25` | Hard cap on hops per session |
| `ATC_MAX_REPEATED_HASHES` | `3` | Consecutive identical payloads before trip |
| `ATC_MAX_CYCLE_LENGTH` | `2` | Max cycle length scanned |
| `ATC_MAX_CYCLE_REPEATS` | `3` | Full cycle repeats before trip |
| `ATC_HISTORY_WINDOW` | `16` | Hashes retained per session |
| `ATC_MAX_SESSIONS` | `10000` | LRU cap on concurrent sessions |

## Security Notes

- **Auth is fail-closed** — if `ATC_API_KEYS` is empty, every `/v1/route` call is rejected with 401.
- **Constant-time key comparison** — prevents timing attacks.
- **No secrets in logs** — only truncated SHA-256 fingerprints for correlation.
- **No `pickle` / `eval` / `exec`** — hashing uses canonical JSON.
- **Bounded memory** — sessions and hash histories are capped.
- **Bounded metric cardinality** — no user-controlled values in labels.
- **Body size limit** — rejects oversized requests before reading them.
- **HTTPS/TLS is your responsibility** — put this behind nginx, Caddy, or a cloud LB.

## Limitations (honest list)

- **In-memory backend is single-process.** Sessions do not survive restarts
  and are not shared across replicas. Use Redis for multi-replica deployments.
- **Exact-hash loop detection only.** Semantically-equivalent-but-differently-worded
  loops are not caught. Adding embeddings is a future feature.
- **Rate limiting is per-process.** With multiple replicas, each has its own
  counter. A shared Redis-backed limiter is a future feature.
- **No audit log.** Governance events are logged but not persisted to disk.
- **No admin API.** Sessions can be reset individually by ID, but there is no
  listing or bulk-cleanup endpoint.
- **No multi-tenancy.** One API key list for all users; sessions are global.

## License

Unlicensed. Do what you want.