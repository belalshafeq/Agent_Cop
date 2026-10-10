# agenttrafficcop (Python SDK)

A thin, typed async client for the [AgentTrafficCop](../README.md)
governance middleware.

- **Async-first** — no blocking calls, no sync/async confusion.
- **Typed exceptions** — catch `LoopDetected`, not `HTTP 422`.
- **Connection pooling** — one `httpx.AsyncClient` per `Guard` instance.
- **No surprises** — the SDK mirrors the server's contracts exactly.

## Install

```bash
pip install -e ./sdk
```

(Once this repo is published, `pip install agenttrafficcop` will also work.)

## Quick Start

```python
import asyncio
from agenttrafficcop import Guard, LoopDetected

async def main():
    async with Guard(
        url="http://localhost:8000",
        api_key="your-api-key",
    ) as guard:
        try:
            result = await guard.check(
                session_id="run-42",
                sender="coder",
                recipient="reviewer",
                payload={"diff": "fix typo", "file": "app.py"},
            )
            print("approved at step", result.step)
        except LoopDetected as e:
            print("loop caught:", e.error_code)
            print("  session:", e.session_id)
            print("  step:", e.diagnostics.get("step"))

asyncio.run(main())
```

## API Reference

### `Guard(url, api_key, *, timeout=10.0)`

Async client. Use as a context manager so the underlying HTTP client
is closed cleanly.

| Method | Purpose |
|---|---|
| `await guard.check(session_id, sender, recipient, payload, intent="handoff", metadata=None)` | Route one message. Returns `RouteResult` on success. |
| `await guard.health()` | Call `GET /health`. No auth. |
| `await guard.reset_session(session_id)` | Delete a session. Returns `bool`. |
| `await guard.close()` | Close the HTTP client. Called by `__aexit__`. |

### `RouteResult`

| Attribute | Type | Meaning |
|---|---|---|
| `approved` | `bool` | Always `True` on success. |
| `session_id` | `str` | Session this message belongs to. |
| `step` | `int` | 1-indexed step number. |
| `payload_hash` | `str` | SHA-256 hex digest (64 chars). |
| `message` | `str` | Human-readable confirmation. |
| `raw` | `dict` | Original response body. |

### Exceptions

All inherit from `AgentTrafficCopError`:

| Exception | When |
|---|---|
| `LoopDetected` | Repeated or cyclic payload. |
| `StepLimitExceeded` | Session hit the step cap. |
| `SchemaRejected` | Server rejected the message schema. |
| `AuthError` | Missing or invalid API key (401). |
| `ConnectionError` | Could not reach the server. |
| `AgentTrafficCopError` | Base class — catch this for everything. |

Every exception exposes `.error_code`, `.session_id`, and `.diagnostics`.

## Catching Everything

```python
try:
    await guard.check(...)
except AgentTrafficCopError as e:
    log.error("governance: %s session=%s", e.error_code, e.session_id)
    # ... decide what to do
```

## Compatibility

- Python 3.11+
- `httpx` 0.27.x
- Works against any AgentTrafficCop server 1.0.0+

## License

MIT