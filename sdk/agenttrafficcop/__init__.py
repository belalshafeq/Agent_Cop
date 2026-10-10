"""AgentTrafficCop Python SDK.

A thin, typed client for the AgentTrafficCop governance middleware.
Use it to route agent-to-agent messages through a running cop instance
and receive typed exceptions when the circuit breaker trips.

Quick start:

    from agenttrafficcop import Guard

    guard = Guard(url="http://localhost:8000", api_key="your-key")

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

Public API:
    Guard              — the async client
    RouteResult        — the 200 response shape
    AgentTrafficCopError  — base exception
    LoopDetected       — repeated or cyclic payload detected
    StepLimitExceeded  — session hit the step cap
    SchemaRejected     — server rejected the message schema
    AuthError          — invalid or missing API key
    ConnectionError    — could not reach the server
"""

from .client import Guard, RouteResult
from .exceptions import (
    AgentTrafficCopError,
    AuthError,
    ConnectionError,
    LoopDetected,
    SchemaRejected,
    StepLimitExceeded,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "Guard",
    "RouteResult",
    "AgentTrafficCopError",
    "LoopDetected",
    "StepLimitExceeded",
    "SchemaRejected",
    "AuthError",
    "ConnectionError",
]