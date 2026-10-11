"""The Guard client — the primary user-facing API of the SDK.

Usage:

    from agenttrafficcop import Guard, LoopDetected

    async with Guard(url="http://localhost:8000", api_key="...") as guard:
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

The client is async-first. A sync wrapper is intentionally not provided
in v0.1.0 — agent orchestrators are almost always async, and adding a
sync path doubles the surface area for bugs.

Multi-loop safety:
    Some frameworks (notably LangGraph) create child event loops. If a
    Guard instance is reused across loops, the cached httpx.AsyncClient
    would be bound to the wrong loop. We detect this and transparently
    rebuild the client for the currently running loop.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import httpx

from .exceptions import (
    AgentTrafficCopError,
    AuthError,
    ConnectionError,
    exception_from_response,
)
from .models import RouteResult


class Guard:
    """Async client for the AgentTrafficCop governance middleware.

    Args:
        url:      Base URL of the AgentTrafficCop server, e.g.
                  "http://localhost:8000". No trailing slash required.
        api_key:  The Bearer token. Must match one of the keys the
                  server has configured in ATC_API_KEYS.
        timeout:  Per-request timeout in seconds. Default 10.
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        timeout: float = 10.0,
    ) -> None:
        if not url:
            raise ValueError("url must not be empty")
        if not api_key:
            raise ValueError("api_key must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be positive")

        self._base_url = url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
        self._client_loop: Optional[asyncio.AbstractEventLoop] = None

    # --- Lifecycle ----------------------------------------------------------

    async def __aenter__(self) -> "Guard":
        await self._ensure_client()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Return an AsyncClient bound to the *current* event loop.

        We detect a loop mismatch (LangGraph and some frameworks create
        child event loops) and transparently create a fresh client. This
        keeps the "one client per Guard" optimization for the common case
        while remaining correct under nested/multiple event loops.
        """
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — we're being called from sync code. Create
            # a client anyway; the caller will hit an error on first use
            # with a clearer message than ours would be.
            current_loop = None

        if (
            self._client is not None
            and not self._client.is_closed
            and self._client_loop is current_loop
        ):
            return self._client

        # Either no client yet, or it's bound to a different loop.
        # Close the old one if it's still open and its loop is alive.
        old = self._client
        if old is not None and not old.is_closed:
            try:
                await old.aclose()
            except RuntimeError:
                # Old client was bound to a dead loop; drop it.
                pass

        self._client = httpx.AsyncClient(timeout=self._timeout)
        self._client_loop = current_loop
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client. Safe to call multiple times
        and from any event loop context."""
        client = self._client
        self._client = None
        self._client_loop = None
        if client is None or client.is_closed:
            return
        try:
            await client.aclose()
        except RuntimeError:
            # Client was bound to a loop that has since closed.
            pass

    # --- Public API ---------------------------------------------------------

    async def check(
        self,
        *,
        session_id: str,
        sender: str,
        recipient: str,
        payload: Dict[str, Any],
        intent: str = "handoff",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> RouteResult:
        """Send one agent-to-agent message through the cop."""
        body: Dict[str, Any] = {
            "session_id": session_id,
            "sender": sender,
            "recipient": recipient,
            "intent": intent,
            "payload": payload,
        }
        if metadata is not None:
            body["metadata"] = metadata

        client = await self._ensure_client()

        try:
            response = await client.post(
                f"{self._base_url}/v1/route",
                headers={"authorization": f"Bearer {self._api_key}"},
                json=body,
            )
        except httpx.RequestError as e:
            raise ConnectionError(
                f"Could not reach AgentTrafficCop at {self._base_url}: {e}",
                error_code="CONNECTION_FAILED",
                session_id=session_id,
                diagnostics={"underlying": e.__class__.__name__},
            ) from e

        if response.status_code == 200:
            try:
                parsed = response.json()
            except ValueError as e:
                raise AgentTrafficCopError(
                    "Server returned 200 with a non-JSON body.",
                    error_code="MALFORMED_RESPONSE",
                    session_id=session_id,
                    diagnostics={"body_preview": response.text[:200]},
                ) from e
            try:
                return RouteResult.from_response(parsed)
            except ValueError as e:
                raise AgentTrafficCopError(
                    str(e),
                    error_code="MALFORMED_RESPONSE",
                    session_id=session_id,
                    diagnostics={"body_preview": response.text[:200]},
                ) from e

        if response.status_code == 401:
            raise AuthError(
                "AgentTrafficCop rejected the API key.",
                error_code="AUTH_INVALID",
                session_id=session_id,
                diagnostics={"body_preview": response.text[:200]},
            )

        if response.status_code == 422:
            try:
                parsed = response.json()
            except ValueError:
                parsed = {
                    "error": "UNPARSEABLE_422",
                    "message": "Server returned 422 with a non-JSON body.",
                    "session_id": session_id,
                    "diagnostics": {"body_preview": response.text[:200]},
                }
            raise exception_from_response(parsed)

        raise AgentTrafficCopError(
            f"AgentTrafficCop returned unexpected HTTP "
            f"{response.status_code}.",
            error_code="UNEXPECTED_STATUS",
            session_id=session_id,
            diagnostics={
                "status_code": response.status_code,
                "body_preview": response.text[:200],
            },
        )

    # --- Admin utilities ----------------------------------------------------

    async def health(self) -> Dict[str, Any]:
        """Call GET /health and return the parsed body. No auth required."""
        client = await self._ensure_client()
        try:
            response = await client.get(f"{self._base_url}/health")
        except httpx.RequestError as e:
            raise ConnectionError(
                f"Could not reach AgentTrafficCop at {self._base_url}: {e}",
                error_code="CONNECTION_FAILED",
            ) from e
        response.raise_for_status()
        return response.json()

    async def reset_session(self, session_id: str) -> bool:
        """Call DELETE /v1/sessions/{id}. Returns True if the session existed."""
        client = await self._ensure_client()
        try:
            response = await client.delete(
                f"{self._base_url}/v1/sessions/{session_id}",
                headers={"authorization": f"Bearer {self._api_key}"},
            )
        except httpx.RequestError as e:
            raise ConnectionError(
                f"Could not reach AgentTrafficCop at {self._base_url}: {e}",
                error_code="CONNECTION_FAILED",
                session_id=session_id,
            ) from e
        if response.status_code == 401:
            raise AuthError(
                "AgentTrafficCop rejected the API key.",
                error_code="AUTH_INVALID",
                session_id=session_id,
            )
        response.raise_for_status()
        return bool(response.json().get("removed", False))