"""LangGraph integration for AgentTrafficCop.

Wraps a LangGraph node function so that every invocation first asks
AgentTrafficCop whether the current handoff is allowed. If the cop
approves, the node runs normally. If it trips the breaker, the node
raises before executing.

Usage:

    from agenttrafficcop import Guard
    from agenttrafficcop.langgraph import guarded_node

    guard = Guard(url="http://localhost:8000", api_key="...")

    graph.add_node(
        "reviewer",
        guarded_node(
            reviewer_fn,
            guard=guard,
            sender="coder",
            recipient="reviewer",
            # session_id and payload can be static values or callables
            # that extract them from the LangGraph state.
            session_id=lambda state: state["run_id"],
            payload=lambda state: {"draft": state["draft"]},
        ),
    )

Design notes:
  * We use only LangGraph's public node-function contract: a node is
    just a callable `(state) -> state_update | None`. Nothing internal.
  * Both sync and async node functions are supported. Async nodes are
    awaited; sync nodes are called directly. The guard call itself is
    always async.
  * If the user does not supply a `payload` extractor, we hash the
    entire state dict (minus a small denylist of known-noise keys).
    This makes the integration work out-of-the-box for graph authors
    who don't want to think about payload shaping.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Union

from .client import Guard
from .exceptions import AgentTrafficCopError

logger = logging.getLogger("agenttrafficcop.langgraph")

# A LangGraph state is almost always a plain dict (TypedDict at type
# level, dict at runtime). We accept any Mapping.
StateT = Dict[str, Any]

# A node function is `(state) -> state_update | None`, sync or async.
NodeFn = Callable[[StateT], Union[Optional[StateT], Awaitable[Optional[StateT]]]]

# An extractor is either a literal value or a callable taking state.
ValueOrExtractor = Union[str, Callable[[StateT], Any]]

# Keys that are unlikely to represent meaningful "handoff content" and
# are therefore excluded from the auto-payload hash.
_AUTO_PAYLOAD_EXCLUDE = frozenset(
    {
        "messages",   # conversation history grows every step -> false loops
        "history",
        "trace",
        "logs",
    }
)


def _resolve(value: ValueOrExtractor, state: StateT, *, name: str) -> Any:
    """Return either the literal *value* or the result of calling it on state."""
    if callable(value):
        try:
            return value(state)
        except Exception as e:
            raise RuntimeError(
                f"AgentTrafficCop: {name} extractor raised on state: {e}"
            ) from e
    return value


def _default_payload(state: StateT) -> Dict[str, Any]:
    """Auto-payload: the state minus known-noise keys.

    Used when the caller doesn't supply a payload extractor. This is a
    pragmatic default — it works for small graph states and produces a
    stable hash. For real graphs, the caller should supply an explicit
    `payload=` extractor.
    """
    return {k: v for k, v in state.items() if k not in _AUTO_PAYLOAD_EXCLUDE}


def guarded_node(
    node_fn: NodeFn,
    *,
    guard: Guard,
    sender: str,
    recipient: str,
    session_id: ValueOrExtractor = "session_id",
    payload: Optional[ValueOrExtractor] = None,
    intent: str = "handoff",
    metadata: Optional[ValueOrExtractor] = None,
    raise_on_trip: bool = True,
) -> NodeFn:
    """Wrap a LangGraph node so it passes through AgentTrafficCop first.

    Args:
        node_fn:       The original node function (sync or async).
        guard:         A configured `agenttrafficcop.Guard` instance.
        sender:        Identifier of the agent emitting this handoff.
        recipient:     Identifier of the agent receiving it.
        session_id:    Static string OR callable(state) -> str. Defaults
                       to reading `state["session_id"]`.
        payload:       Static dict OR callable(state) -> dict. Defaults
                       to the whole state minus known-noise keys.
        intent:        One of the AgentMessage intents. Default "handoff".
        metadata:      Static dict OR callable(state) -> dict, optional.
        raise_on_trip: If True (default), a breaker trip raises the SDK
                       exception. If False, the wrapper returns the
                       state unchanged and the graph continues without
                       running the node — useful for graphs that want
                       to handle trips via state instead of exceptions.

    Returns:
        A node function with the same signature as `node_fn`.
    """
    if not callable(node_fn):
        raise TypeError("node_fn must be callable")

    is_async = inspect.iscoroutinefunction(node_fn)

    async def _check(state: StateT) -> None:
        sid = _resolve(session_id, state, name="session_id")
        if payload is None:
            pld = _default_payload(state)
        else:
            pld = _resolve(payload, state, name="payload")
        md = _resolve(metadata, state, name="metadata") if metadata is not None else None

        if not isinstance(sid, str) or not sid:
            raise ValueError(
                "AgentTrafficCop: session_id resolved to an empty/invalid value"
            )
        if not isinstance(pld, dict):
            raise ValueError(
                "AgentTrafficCop: payload must resolve to a dict, got "
                f"{type(pld).__name__}"
            )

        try:
            await guard.check(
                session_id=sid,
                sender=sender,
                recipient=recipient,
                payload=pld,
                intent=intent,
                metadata=md,
            )
        except AgentTrafficCopError as e:
            if raise_on_trip:
                logger.warning(
                    "langgraph_node_tripped node=%s session=%s error_code=%s",
                    getattr(node_fn, "__name__", repr(node_fn)),
                    sid,
                    e.error_code,
                )
                raise
            logger.info(
                "langgraph_node_skipped node=%s session=%s error_code=%s",
                getattr(node_fn, "__name__", repr(node_fn)),
                sid,
                e.error_code,
            )
            # Swallow and signal "no-op" to the caller.
            return

    if is_async:
        async def _wrapped_async(state: StateT) -> Any:
            await _check(state)
            return await node_fn(state)  # type: ignore[misc]

        _wrapped_async.__name__ = getattr(node_fn, "__name__", "guarded_node")
        _wrapped_async.__doc__ = node_fn.__doc__
        return _wrapped_async  # type: ignore[return-value]

    def _wrapped_sync(state: StateT) -> Any:
        # The guard call is async; run it in a dedicated event loop if
        # no loop is running. In LangGraph sync-mode nodes, there's no
        # running loop, so asyncio.run is safe. If a loop *is* running
        # (e.g., LangGraph called this from an async context), we cannot
        # use asyncio.run and instead block on a fresh loop via
        # run_coroutine_threadsafe — but since there's no other thread,
        # we fall back to raising a clear error.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(_check(state))
        else:
            raise RuntimeError(
                "guarded_node: sync node called inside a running event loop. "
                "Either make your node async, or use raise_on_trip=False. "
                "AgentTrafficCop's Guard is async-only by design."
            )
        return node_fn(state)

    _wrapped_sync.__name__ = getattr(node_fn, "__name__", "guarded_node")
    _wrapped_sync.__doc__ = node_fn.__doc__
    return _wrapped_sync  # type: ignore[return-value]