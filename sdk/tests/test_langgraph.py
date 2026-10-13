"""Tests for the LangGraph integration (guarded_node)."""

from __future__ import annotations

import asyncio

import pytest

from agenttrafficcop import Guard, LoopDetected
from agenttrafficcop.langgraph import guarded_node


# Constants — defined locally to avoid conftest import issues.
# Constants — must match sdk/tests/conftest.py and the main test suite.
VALID_API_KEY = "test-key-1"
SDK_BASE_URL = "http://testserver"


class TestGuardedNodeConstruction:
    def test_wraps_async_node(self):
        async def my_node(state):
            return {"result": "ok"}

        guard = Guard(url=SDK_BASE_URL, api_key=VALID_API_KEY)
        wrapped = guarded_node(
            my_node,
            guard=guard,
            sender="a",
            recipient="b",
            session_id="static-sid",
            payload={"x": 1},
        )
        assert asyncio.iscoroutinefunction(wrapped)

    def test_wraps_sync_node(self):
        def my_node(state):
            return {"result": "ok"}

        guard = Guard(url=SDK_BASE_URL, api_key=VALID_API_KEY)
        wrapped = guarded_node(
            my_node,
            guard=guard,
            sender="a",
            recipient="b",
            session_id="static-sid",
            payload={"x": 1},
        )
        assert not asyncio.iscoroutinefunction(wrapped)

    def test_rejects_non_callable(self):
        guard = Guard(url=SDK_BASE_URL, api_key=VALID_API_KEY)
        with pytest.raises(TypeError):
            guarded_node(
                "not-a-function",
                guard=guard,
                sender="a",
                recipient="b",
            )


class TestAsyncNode:
    async def test_node_runs_when_approved(self):
        calls = []

        async def my_node(state):
            calls.append(state)
            return {"result": "ran"}

        async with Guard(url=SDK_BASE_URL, api_key=VALID_API_KEY) as guard:
            wrapped = guarded_node(
                my_node,
                guard=guard,
                sender="a",
                recipient="b",
                session_id="lg-async-1",
                payload={"x": 1},
            )
            result = await wrapped({"input": "whatever"})

        assert result == {"result": "ran"}
        assert len(calls) == 1

    async def test_node_raises_when_breaker_trips(self):
        from app.config import settings

        async def my_node(state):
            return {"result": "ran"}

        async with Guard(url=SDK_BASE_URL, api_key=VALID_API_KEY) as guard:
            wrapped = guarded_node(
                my_node,
                guard=guard,
                sender="a",
                recipient="b",
                session_id="lg-async-trip",
                payload={"stuck": True},
            )
            with pytest.raises(LoopDetected):
                for _ in range(settings.max_repeated_hashes):
                    await wrapped({"whatever": 1})

    async def test_node_skipped_when_raise_on_trip_false(self):
        from app.config import settings

        calls = []

        async def my_node(state):
            calls.append(state)
            return {"result": "ran"}

        async with Guard(url=SDK_BASE_URL, api_key=VALID_API_KEY) as guard:
            wrapped = guarded_node(
                my_node,
                guard=guard,
                sender="a",
                recipient="b",
                session_id="lg-async-skip",
                payload={"stuck": True},
                raise_on_trip=False,
            )
            # First (max_repeated - 1) calls run the node.
            for _ in range(settings.max_repeated_hashes - 1):
                await wrapped({"whatever": 1})
            # The next call trips; node is skipped; no exception raised.
            result = await wrapped({"whatever": 1})

        assert result is None
        assert len(calls) == settings.max_repeated_hashes - 1


class TestExtractors:
    async def test_callable_session_id_and_payload(self):
        async def my_node(state):
            return {"result": "ran"}

        async with Guard(url=SDK_BASE_URL, api_key=VALID_API_KEY) as guard:
            wrapped = guarded_node(
                my_node,
                guard=guard,
                sender="a",
                recipient="b",
                session_id=lambda s: s["run_id"],
                payload=lambda s: {"data": s["data"]},
            )
            result = await wrapped({"run_id": "from-state", "data": "hello"})

        assert result == {"result": "ran"}