"""Runnable LangGraph demo of AgentTrafficCop.

Builds a real LangGraph StateGraph with two nodes (coder, reviewer),
both wrapped by `guarded_node()`. The graph loops. On the first few
turns, everything is approved. Around step 6-8, the breaker trips and
the graph halts.

Run with the AgentTrafficCop server already up:

    Terminal 1:  python run.py
    Terminal 2:  python examples/langgraph_demo.py

No LLM API key required. Deterministic. Safe to run repeatedly.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, TypedDict

# --- Make the SDK importable when running from the repo ------------------
# (If the SDK is pip-installed, this is a no-op.)
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SDK_PATH = _REPO_ROOT / "sdk"
if _SDK_PATH.is_dir() and str(_SDK_PATH) not in sys.path:
    sys.path.insert(0, str(_SDK_PATH))

# --- Load .env if present -----------------------------------------------
try:
    from dotenv import load_dotenv
    _ENV_PATH = _REPO_ROOT / ".env"
    if _ENV_PATH.is_file():
        load_dotenv(dotenv_path=_ENV_PATH, override=False)
except ImportError:
    pass

from langgraph.graph import StateGraph, START, END  # noqa: E402

from agenttrafficcop import Guard, LoopDetected, StepLimitExceeded  # noqa: E402
from agenttrafficcop.langgraph import guarded_node  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ATC_URL = os.getenv("ATC_URL", "http://localhost:8000")
ATC_KEY = os.getenv("ATC_KEY") or _first_api_key()

SESSION_ID = "langgraph-demo"

# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"

MAX_TURNS = 15  # hard ceiling so the demo can't hang if something's off


def hr(char: str = "─", width: int = 72) -> None:
    print(DIM + char * width + RESET)


def header(text: str, color: str = CYAN) -> None:
    hr("═")
    print(f"{color}{BOLD}{text}{RESET}")
    hr("═")


def _first_api_key() -> str:
    raw = os.getenv("ATC_API_KEYS", "")
    if not raw:
        print(
            "ERROR: no ATC_API_KEYS found in environment or .env. "
            "Set one, e.g. by copying .env.example to .env.",
            file=sys.stderr,
        )
        sys.exit(2)
    return raw.split(",")[0].strip()


# ---------------------------------------------------------------------------
# Graph state
# ---------------------------------------------------------------------------


class DemoState(TypedDict, total=False):
    session_id: str
    draft: str
    feedback: str
    steps: int
    done: bool


# ---------------------------------------------------------------------------
# Deterministic agent logic (no LLM calls)
# ---------------------------------------------------------------------------


# After 2 distinct turns, both agents repeat their last output -> loop.
# This is what the circuit breaker is designed to catch.
_CODER_DRAFTS = [
    "def foo(): return 42",
    "def foo(): return 42  # renamed",
]
_REVIEWER_FEEDBACK = [
    "Please add a docstring.",
    "Please handle the empty case.",
]


def coder_logic(state: DemoState) -> Dict[str, Any]:
    """The real work the coder node does after AgentTrafficCop approves."""
    turn = state.get("steps", 0) // 2  # both agents tick `steps`, so divide
    idx = min(turn, len(_CODER_DRAFTS) - 1)
    draft = _CODER_DRAFTS[idx]
    print(
        f"  {CYAN}{BOLD}Coder{RESET}     {DIM}(ran){RESET}    "
        f"→ draft: {DIM}{draft!r}{RESET}"
    )
    return {
        "draft": draft,
        "steps": state.get("steps", 0) + 1,
    }


def reviewer_logic(state: DemoState) -> Dict[str, Any]:
    """The real work the reviewer node does after AgentTrafficCop approves."""
    turn = state.get("steps", 0) // 2
    idx = min(turn, len(_REVIEWER_FEEDBACK) - 1)
    fb = _REVIEWER_FEEDBACK[idx]
    print(
        f"  {CYAN}{BOLD}Reviewer{RESET}   {DIM}(ran){RESET}    "
        f"→ feedback: {DIM}{fb!r}{RESET}"
    )
    return {
        "feedback": fb,
        "steps": state.get("steps", 0) + 1,
    }


# ---------------------------------------------------------------------------
# Build the graph
# ---------------------------------------------------------------------------


def build_graph(guard: Guard) -> Any:
    """Build a LangGraph with two guarded nodes and a ping-pong loop."""
    graph = StateGraph(DemoState)

    graph.add_node(
        "coder",
        guarded_node(
            coder_logic,
            guard=guard,
            sender="reviewer",       # who is handing off *to* the coder
            recipient="coder",
            session_id=lambda s: s.get("session_id", SESSION_ID),
            payload=lambda s: {
                "draft": s.get("draft", ""),
                "feedback": s.get("feedback", ""),
            },
        ),
    )

    graph.add_node(
        "reviewer",
        guarded_node(
            reviewer_logic,
            guard=guard,
            sender="coder",          # who is handing off *to* the reviewer
            recipient="reviewer",
            session_id=lambda s: s.get("session_id", SESSION_ID),
            payload=lambda s: {
                "draft": s.get("draft", ""),
                "feedback": s.get("feedback", ""),
            },
        ),
    )

    # Edges: start -> coder -> reviewer -> coder -> ...
    graph.add_edge(START, "coder")
    graph.add_edge("coder", "reviewer")
    graph.add_edge("reviewer", "coder")

    # Compile without a checkpointer — we don't need persistence for the demo.
    return graph.compile()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> int:
    print()
    print(f"{BOLD}AgentTrafficCop × LangGraph demo{RESET}")
    print(f"{DIM}Two guarded nodes. One loop. The breaker trips.{RESET}\n")

    async with Guard(url=ATC_URL, api_key=ATC_KEY) as guard:
        # Health check so failures are obvious.
        try:
            h = await guard.health()
            print(
                f"{DIM}Connected to {ATC_URL} "
                f"(version {h.get('version', '?')}){RESET}\n"
            )
        except Exception as e:
            print(
                f"{RED}Cannot reach AgentTrafficCop at {ATC_URL}.{RESET}\n"
                f"{DIM}Start it in another terminal:  python run.py{RESET}\n"
                f"{DIM}Underlying error: {e}{RESET}",
                file=sys.stderr,
            )
            return 2

        # Clean slate.
        await guard.reset_session(SESSION_ID)

        header("LangGraph: coder ↔ reviewer with guarded nodes", GREEN)
        print(
            f"{DIM}Every node invocation passes through AgentTrafficCop. "
            f"Watch for the trip.{RESET}\n"
        )

        app = build_graph(guard)

        initial_state: DemoState = {
            "session_id": SESSION_ID,
            "draft": "",
            "feedback": "",
            "steps": 0,
            "done": False,
        }

        trip_caught = False
        reason: str = ""
        try:
            # Cap execution with a recursion limit so a runaway graph
            # cannot hang the demo if the breaker somehow doesn't fire.
            await app.ainvoke(
                initial_state,
                config={"recursion_limit": MAX_TURNS},
            )
        except LoopDetected as e:
            trip_caught = True
            reason = e.error_code or "LOOP_DETECTED"
            print()
            hr("═")
            print(f"{RED}{BOLD}🛑 CIRCUIT BREAKER TRIPPED{RESET}")
            print(f"{RED}Error code: {reason}{RESET}")
            print(f"{RED}Session:    {e.session_id}{RESET}")
            for k, v in (e.diagnostics or {}).items():
                if k == "recent_hashes":
                    continue
                print(f"{RED}  {k}: {v}{RESET}")
            hr("═")
        except StepLimitExceeded as e:
            trip_caught = True
            reason = e.error_code or "STEP_LIMIT_EXCEEDED"
            print()
            hr("═")
            print(f"{RED}{BOLD}🛑 STEP LIMIT TRIPPED{RESET}")
            print(f"{RED}Error code: {reason}{RESET}")
            print(f"{RED}Session:    {e.session_id}{RESET}")
            hr("═")
        except Exception as e:
            # RecursionError from LangGraph if MAX_TURNS is hit; anything
            # else is a real bug we want to see.
            print()
            print(f"{YELLOW}Graph stopped: {type(e).__name__}: {e}{RESET}")
            return 3

        if not trip_caught:
            print()
            print(
                f"{YELLOW}No trip after {MAX_TURNS} turns — "
                f"check the server's max_repeated / max_cycle settings.{RESET}"
            )
            return 4

        print()
        print(f"{GREEN}{BOLD}Demo complete.{RESET}")
        print(
            f"{DIM}The LangGraph was halted by AgentTrafficCop's "
            f"circuit breaker at runtime.{RESET}\n"
        )
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))