pip install "agenttrafficcop[langgraph]"

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

## LangGraph Integration

If you're using [LangGraph](https://langchain-ai.github.io/langgraph/),
the SDK ships an optional adapter that wraps any node so its handoff
passes through AgentTrafficCop before the node runs.

### Install the extra

```bash
pip install -e "./sdk[langgraph]"
```

### Wrap any node in one line

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
        # Both of these can be static values OR callables that take
        # the LangGraph state and return the value.
        session_id=lambda state: state["run_id"],
        payload=lambda state: {"draft": state["draft"]},
    ),
)
```

That's it. Every time LangGraph invokes the `reviewer` node,
AgentTrafficCop sees a handoff. If the breaker trips, the node raises
before doing any work.

### What if I don't supply `payload=`?

The wrapper falls back to hashing the entire state dict (minus a small
denylist of known-noise keys like `messages`, `history`, `trace`). This
works for small graphs, but for real graphs you should supply an
explicit `payload=` extractor — otherwise a growing message history
could cause false-positive trips.

### Sync nodes

`guarded_node` supports sync and async node functions. Sync nodes are
called directly; async nodes are awaited. The guard call itself is
always async, so if you use a sync node you must call it from a sync
context (no running event loop). For anything async-first, make the
node `async def` and everything works.

### Handling trips without raising

By default, a breaker trip raises the SDK exception (e.g.
`LoopDetected`) and propagates out of the node. If you'd rather handle
trips via state — for example, to route to a human-review node — pass:

```python
guarded_node(..., raise_on_trip=False)
```

The node becomes a no-op on a trip, and the graph continues.

### Full example

See [`examples/langgraph_demo.py`](../examples/langgraph_demo.py)
for a runnable two-node graph that visibly trips the breaker.

## Compatibility

- Python 3.11+
- `httpx` 0.27.x
- LangGraph 1.2.x (optional)
- Works against any AgentTrafficCop server 1.0.0+

## License

MIT