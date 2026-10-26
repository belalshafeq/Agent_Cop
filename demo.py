"""AgentTrafficCop demo: two agents, one loop, two outcomes.

Run with the server already up:

    Terminal 1:  python run.py
    Terminal 2:  python demo.py

The demo runs the same Coder ↔ Reviewer conversation twice:

  * Run 1: messages are NOT sent to AgentTrafficCop. The loop is
           allowed to continue until a hard-coded ceiling. We print
           a fake cost to show the "before" pain.
  * Run 2: every message goes through AgentTrafficCop. The breaker
           trips within a few turns, and the session halts cleanly.

No LLM API key required. Deterministic. Safe to run repeatedly.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict

import httpx

from demo_agents import Coder, Reviewer, estimate_cost

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _load_env() -> None:
    """Load .env from the repo root, if present."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.is_file():
        load_dotenv(dotenv_path=env_path, override=False)


_load_env()

ATC_URL = os.getenv("ATC_URL", "http://localhost:8000")

def _first_api_key() -> str:
    """Pull the first key out of ATC_API_KEYS in the environment."""
    raw = os.getenv("ATC_API_KEYS", "")
    if not raw:
        print(
            "ERROR: no ATC_API_KEYS found in environment or .env. "
            "Set one, e.g. by copying .env.example to .env.",
            file=sys.stderr,
        )
        sys.exit(2)
    return raw.split(",")[0].strip()

ATC_KEY = os.getenv("ATC_KEY") or _first_api_key()

SESSION_ID = "demo-loop"

# The "without protection" run is capped at this many round-trips so the
# demo doesn't actually hang forever. Real life has no cap.
BEFORE_MAX_ROUNDS = 12

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


def hr(char: str = "─", width: int = 72) -> None:
    print(DIM + char * width + RESET)


def header(text: str, color: str = CYAN) -> None:
    hr("═")
    print(f"{color}{BOLD}{text}{RESET}")
    hr("═")


def short(payload: Dict[str, Any], max_len: int = 60) -> str:
    s = str(payload)
    return s if len(s) <= max_len else s[: max_len - 3] + "..."


# ---------------------------------------------------------------------------
# Run 1 — WITHOUT AgentTrafficCop
# ---------------------------------------------------------------------------


async def run_without_protection() -> None:
    header("RUN 1: WITHOUT AgentTrafficCop", YELLOW)
    print("Two agents loop. Nothing stops them. Watch the cost.\n")

    coder = Coder()
    reviewer = Reviewer()

    round_no = 0
    for round_no in range(1, BEFORE_MAX_ROUNDS + 1):
        c = coder.next_message()
        print(
            f"  {CYAN}Coder{RESET}     → {DIM}Reviewer{RESET} : "
            f"{short(c.payload)}"
        )

        r = reviewer.next_message()
        print(
            f"  {CYAN}Reviewer{RESET}   → {DIM}Coder{RESET}    : "
            f"{short(r.payload)}"
        )

    total_messages = round_no * 2
    cost = estimate_cost(total_messages)
    print()
    print(
        f"  {RED}{BOLD}No breaker. Loop ran {round_no} rounds "
        f"({total_messages} messages).{RESET}"
    )
    print(
        f"  {RED}{BOLD}Estimated cost: ${cost:.2f}{RESET}  "
        f"{DIM}(fake model, for illustration){RESET}\n"
    )


# ---------------------------------------------------------------------------
# Run 2 — WITH AgentTrafficCop
# ---------------------------------------------------------------------------


async def send_through_atc(
    client: httpx.AsyncClient,
    session_id: str,
    sender: str,
    recipient: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """POST one message to /v1/route and return the parsed response."""
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
        timeout=5.0,
    )
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text}
    return {"status_code": r.status_code, "body": body}


async def run_with_protection() -> bool:
    """Returns True if the breaker tripped, False otherwise."""
    header("RUN 2: WITH AgentTrafficCop", GREEN)
    print(
        f"Same agents. Every message goes through the cop at {ATC_URL}.\n"
    )

    # Clean slate so the demo is idempotent.
    async with httpx.AsyncClient() as client:
        await client.delete(
            f"{ATC_URL}/v1/sessions/{SESSION_ID}",
            headers={"authorization": f"Bearer {ATC_KEY}"},
            timeout=5.0,
        )

    coder = Coder()
    reviewer = Reviewer()

    tripped = False
    messages_sent = 0

    async with httpx.AsyncClient() as client:
        for round_no in range(1, BEFORE_MAX_ROUNDS + 1):
            for agent, msg in (
                ("Coder", coder.next_message()),
                ("Reviewer", reviewer.next_message()),
            ):
                messages_sent += 1
                result = await send_through_atc(
                    client,
                    session_id=SESSION_ID,
                    sender=msg.sender,
                    recipient=msg.recipient,
                    payload=msg.payload,
                )
                status = result["status_code"]
                body = result["body"]

                if status == 200:
                    step = body.get("step", "?")
                    print(
                        f"  {CYAN}{agent:<8}{RESET} → "
                        f"{DIM}{msg.recipient:<8}{RESET} : "
                        f"{short(msg.payload)}  "
                        f"{GREEN}[APPROVED step={step}]{RESET}"
                    )
                elif status == 422:
                    err = body.get("error", "UNKNOWN")
                    diag = body.get("diagnostics", {})
                    print(
                        f"  {CYAN}{agent:<8}{RESET} → "
                        f"{DIM}{msg.recipient.capitalize():<8}{RESET} : "
                        f"{short(msg.payload)}  "
                        f"{RED}[{err}]{RESET}"
                    )
                    print()
                    print(f"  {RED}{BOLD}🛑 CIRCUIT BREAKER TRIPPED{RESET}")
                    print(f"  {RED}Error code: {err}{RESET}")
                    print(f"  {RED}Session:    {SESSION_ID}{RESET}")
                    for k, v in diag.items():
                        if k == "recent_hashes":
                            continue
                        print(f"  {RED}  {k}: {v}{RESET}")
                    print()
                    cost = estimate_cost(messages_sent)
                    print(
                        f"  {GREEN}{BOLD}Session halted after "
                        f"{messages_sent} messages. "
                        f"Estimated cost: ${cost:.2f}{RESET}"
                    )
                    print(
                        f"  {GREEN}{BOLD}Saved vs Run 1: "
                        f"${estimate_cost(BEFORE_MAX_ROUNDS * 2) - cost:.2f}"
                        f"{RESET}\n"
                    )
                    tripped = True
                    break
                elif status == 401:
                    print(
                        f"  {RED}AUTH FAILED ({status}) — is ATC_KEY valid?{RESET}"
                    )
                    return False
                else:
                    print(
                        f"  {RED}UNEXPECTED {status}: {body}{RESET}"
                    )
                    return False
            if tripped:
                break

    if not tripped:
        print(f"  {YELLOW}No trip after {BEFORE_MAX_ROUNDS} rounds.{RESET}\n")
    return tripped


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> int:
    print()
    print(f"{BOLD}AgentTrafficCop demo{RESET}")
    print(f"{DIM}Two toy agents. One loop. Two outcomes.{RESET}\n")

    # Quick health check so failures are obvious.
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{ATC_URL}/health", timeout=3.0)
            r.raise_for_status()
            h = r.json()
    except Exception as e:
        print(
            f"{RED}Cannot reach AgentTrafficCop at {ATC_URL}.{RESET}\n"
            f"{DIM}Start it in another terminal:  python run.py{RESET}\n"
            f"{DIM}Underlying error: {e}{RESET}",
            file=sys.stderr,
        )
        return 2

    print(
        f"{DIM}Connected to {ATC_URL} "
        f"(version {h.get('version', '?')}, "
        f"sessions={h.get('active_sessions', 0)}){RESET}\n"
    )

    await run_without_protection()
    await run_with_protection()

    print(f"{BOLD}Demo complete.{RESET}\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))