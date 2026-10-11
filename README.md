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

## Configuration

All settings are read from environment variables (or `.env`):

| Env var | Default | Meaning |
|---|---|---|
| `ATC_HOST` | `0.0.0.0` | Bind address |
| `ATC_PORT` | `8000` | Bind port |
| `ATC_ENV` | `development` | `production` disables `/docs` |
| `ATC_LOG_LEVEL` | `INFO` | Log level |
| `ATC_API_KEYS` | (none) | Comma-separated Bearer keys. **Required.** |
| `ATC_MAX_STEPS` | `25` | Hard cap on hops per session |
| `ATC_MAX_REPEATED_HASHES` | `3` | Consecutive identical payloads before trip |
| `ATC_MAX_CYCLE_LENGTH` | `2` | Max cycle length scanned |
| `ATC_MAX_CYCLE_REPEATS` | `3` | Full cycle repeats before trip |
| `ATC_HISTORY_WINDOW` | `16` | Hashes retained per session |
| `ATC_MAX_SESSIONS` | `10000` | LRU cap on concurrent sessions |
| `ATC_MAX_BODY_BYTES` | `1048576` | Reserved for future body-size enforcement |

## Security Notes

- **Auth is fail-closed** — if `ATC_API_KEYS` is empty, every `/v1/route` call is rejected with 401.
- **Constant-time key comparison** — prevents timing attacks.
- **No secrets in logs** — only truncated SHA-256 fingerprints for correlation.
- **No `pickle` / `eval` / `exec`** — hashing uses canonical JSON.
- **Bounded memory** — sessions and hash histories are capped.
- **HTTPS/TLS is your responsibility** — put this behind nginx, Caddy, or a cloud LB.

## Limitations (honest list)

- **Single-process only.** Sessions live in memory; a restart loses them. For
  multi-replica deployments, back the store with Redis (not implemented yet).
- **Exact-hash loop detection only.** Semantically-equivalent-but-differently-worded
  loops are not caught. Adding embeddings is a future feature.
- **No rate limiting.** Add `slowapi` or a reverse-proxy limiter if needed.
- **No persistence.** This is a runtime shield, not a database.

## Project Layout

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
│   ├── circuit_breaker.py
│   ├── logging_config.py
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
├── demo.py                       # reference demo
├── demo_agents.py
├── run.py                        # server entrypoint
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

## License

Unlicensed. Do what you want.