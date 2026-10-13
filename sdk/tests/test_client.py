"""Tests for the SDK's Guard client."""

from __future__ import annotations

import pytest

from agenttrafficcop import (
    AuthError,
    Guard,
    LoopDetected,
    RouteResult,
    SchemaRejected,
    StepLimitExceeded,
)


# Constants — defined locally to avoid conftest import issues.
# These must match sdk/tests/conftest.py.
# Constants — must match sdk/tests/conftest.py and the main test suite.
VALID_API_KEY = "test-key-1"
INVALID_API_KEY = "not-a-real-key"
SDK_BASE_URL = "http://testserver"


class TestGuardConstruction:
    def test_constructs_with_valid_args(self):
        g = Guard(url=SDK_BASE_URL, api_key=VALID_API_KEY)
        assert g is not None

    def test_rejects_empty_url(self):
        with pytest.raises(ValueError):
            Guard(url="", api_key=VALID_API_KEY)

    def test_rejects_empty_api_key(self):
        with pytest.raises(ValueError):
            Guard(url=SDK_BASE_URL, api_key="")

    def test_rejects_zero_timeout(self):
        with pytest.raises(ValueError):
            Guard(url=SDK_BASE_URL, api_key=VALID_API_KEY, timeout=0)


class TestCheckSuccess:
    async def test_approves_valid_message(self):
        async with Guard(url=SDK_BASE_URL, api_key=VALID_API_KEY) as g:
            result = await g.check(
                session_id="sdk-test-1",
                sender="coder",
                recipient="reviewer",
                payload={"diff": "fix", "file": "app.py"},
            )
        assert isinstance(result, RouteResult)
        assert result.approved is True
        assert result.session_id == "sdk-test-1"
        assert result.step == 1
        assert len(result.payload_hash) == 64

    async def test_increments_step_on_second_call(self):
        async with Guard(url=SDK_BASE_URL, api_key=VALID_API_KEY) as g:
            r1 = await g.check(
                session_id="sdk-test-2",
                sender="coder",
                recipient="reviewer",
                payload={"a": 1},
            )
            r2 = await g.check(
                session_id="sdk-test-2",
                sender="coder",
                recipient="reviewer",
                payload={"a": 2},
            )
        assert r1.step == 1
        assert r2.step == 2


class TestCheckLoops:
    async def test_repeated_payload_raises_loop_detected(self):
        from app.config import settings

        async with Guard(url=SDK_BASE_URL, api_key=VALID_API_KEY) as g:
            payload = {"stuck": True}
            with pytest.raises(LoopDetected) as exc_info:
                for _ in range(settings.max_repeated_hashes):
                    await g.check(
                        session_id="sdk-loop",
                        sender="a",
                        recipient="b",
                        payload=payload,
                    )
        assert exc_info.value.error_code == "REPEATED_PAYLOAD_LOOP"
        assert exc_info.value.session_id == "sdk-loop"

    async def test_step_limit_raises_step_limit_exceeded(self):
        from app.config import settings

        async with Guard(url=SDK_BASE_URL, api_key=VALID_API_KEY) as g:
            with pytest.raises(StepLimitExceeded) as exc_info:
                for i in range(settings.max_steps + 5):
                    await g.check(
                        session_id="sdk-steps",
                        sender="a",
                        recipient="b",
                        payload={"i": i},
                    )
        assert exc_info.value.error_code == "STEP_LIMIT_EXCEEDED"


class TestCheckSchema:
    async def test_empty_payload_raises_schema_rejected(self):
        async with Guard(url=SDK_BASE_URL, api_key=VALID_API_KEY) as g:
            with pytest.raises(SchemaRejected):
                await g.check(
                    session_id="sdk-schema",
                    sender="a",
                    recipient="b",
                    payload={},
                )

    async def test_same_sender_recipient_raises_schema_rejected(self):
        async with Guard(url=SDK_BASE_URL, api_key=VALID_API_KEY) as g:
            with pytest.raises(SchemaRejected):
                await g.check(
                    session_id="sdk-schema-2",
                    sender="a",
                    recipient="a",
                    payload={"x": 1},
                )


class TestCheckAuth:
    async def test_invalid_key_raises_auth_error(self):
        async with Guard(url=SDK_BASE_URL, api_key=INVALID_API_KEY) as g:
            with pytest.raises(AuthError):
                await g.check(
                    session_id="sdk-auth",
                    sender="a",
                    recipient="b",
                    payload={"x": 1},
                )


class TestHealth:
    async def test_health_returns_ok(self):
        async with Guard(url=SDK_BASE_URL, api_key=VALID_API_KEY) as g:
            h = await g.health()
        assert h["status"] == "ok"
        assert h["service"] == "AgentTrafficCop"


class TestResetSession:
    async def test_reset_returns_true_for_existing(self):
        async with Guard(url=SDK_BASE_URL, api_key=VALID_API_KEY) as g:
            await g.check(
                session_id="sdk-reset",
                sender="a",
                recipient="b",
                payload={"x": 1},
            )
            removed = await g.reset_session("sdk-reset")
        assert removed is True

    async def test_reset_returns_false_for_unknown(self):
        async with Guard(url=SDK_BASE_URL, api_key=VALID_API_KEY) as g:
            removed = await g.reset_session("never-existed")
        assert removed is False


class TestLifecycle:
    async def test_context_manager_opens_and_closes(self):
        async with Guard(url=SDK_BASE_URL, api_key=VALID_API_KEY) as g:
            h = await g.health()
            assert h["status"] == "ok"
        h2 = await g.health()
        assert h2["status"] == "ok"
        await g.close()