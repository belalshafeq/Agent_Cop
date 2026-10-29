"""LangGraph integration for AgentTrafficCop.

Wraps a LangGraph node function so that every invocation first asks
AgentTrafficCop whether the current handoff is allowed.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Union

from .client import Guard
from .exceptions import AgentTrafficCopError

logger = logging.getLogger("agenttrafficcop.langgraph")

StateT = Dict[str, Any]
NodeFn = Callable[[StateT], Union[Optional[StateT], Awaitable[Optional[StateT]]]]
ValueOrExtractor = Union[str, Callable[[StateT], Any]]

_AUTO_PAYLOAD_EXCLUDE = frozenset(
    {"messages", "history", "trace", "logs"}
)


def _resolve(value: ValueOrExtractor, state: StateT, *, name: str) -> Any:
    if callable(value):
        try:
            return value(state)
        except Exception as e:
            raise RuntimeError(
                f"AgentTrafficCop: {name} extractor raised on state: {e}"
            ) from e
    return value


def _default_payload(state: StateT) -> Dict[str, Any]:
    return {k: v for k, v in state.items() if k not in _AUTO_PAYLOAD_EXCLUDE}


def guarded_node(
    node_fn: NodeFn,
    *,
    guard: Guard,
    sender: str,
    recipient: str,
    # FIX (C2): `session_id` is now REQUIRED. The old default was the
    # literal string "session_id", which meant every node that forgot to
    # pass one shared a global session called "session_id". Two unrelated
    # users' loops would collide in the same state. Now it fails loudly
    # with a clear error if omitted.
    session_id: Optional[ValueOrExtractor] = None,
    payload: Optional[ValueOrExtractor] = None,
    intent: str = "handoff",
    metadata: Optional[ValueOrExtractor] = None,
    raise_on_trip: bool = True,
) -> NodeFn:
    if not callable(node_fn):
        raise TypeError("node_fn must be callable")
    if session_id is None:
        raise ValueError(
            "guarded_node: session_id is required. Pass a string or an "
            "extractor callable, e.g. session_id=lambda s: s['run_id']. "
            "The old default ('session_id') shared one global session "
            "across all callers, so it's now a hard error."
        )

    is_async = inspect.iscoroutinefunction(node_fn)

    async def _check(state: StateT) -> bool:
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
            return True
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
            return False

    if is_async:
        async def _wrapped_async(state: StateT) -> Any:
            should_run = await _check(state)
            if not should_run:
                return None
            return await node_fn(state)

        _wrapped_async.__name__ = getattr(node_fn, "__name__", "guarded_node")
        # FIX (L8): `or ""` avoids setting __doc__ to None, which some
        # tooling and introspection treats as missing.
        _wrapped_async.__doc__ = node_fn.__doc__ or ""
        return _wrapped_async

    def _wrapped_sync(state: StateT) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            should_run = asyncio.run(_check(state))
        else:
            # FIX (#6): old message recommended `raise_on_trip=False`,
            # which CANNOT help — we're raising before the guard check
            # even runs. Give the only two real fixes.
            raise RuntimeError(
                "guarded_node: a sync node was called from inside a "
                "running event loop. `raise_on_trip=False` does NOT fix "
                "this — the guard check never runs. To resolve:\n"
                "  (a) make your node async (recommended), or\n"
                "  (b) run the graph from a sync context (no active "
                "event loop on the caller stack).\n"
                "AgentTrafficCop's Guard is async-only by design."
            )
        if not should_run:
            return None
        return node_fn(state)

    _wrapped_sync.__name__ = getattr(node_fn, "__name__", "guarded_node")
    _wrapped_sync.__doc__ = node_fn.__doc__ or ""
    return _wrapped_sync