"""Deterministic SHA-256 payload hashing for loop detection.

The hash must be stable across processes, runs, and machines. We
therefore canonicalize the payload into a JSON string with sorted
keys and no insignificant whitespace, then compute SHA-256 over the
UTF-8 bytes of that string.

Security:
  * We use `json.dumps`, never `pickle` — no code execution risk.
  * We never `eval` or `exec` any user input.
  * The fallback serializer uses `repr()`, not `__reduce__` or similar.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _fallback_serializer(obj: Any) -> Any:
    """Best-effort serialization of non-JSON-native values.

    This runs only when `json.dumps` encounters a value it cannot
    serialize natively (e.g. sets, bytes, datetime, custom classes).
    We convert to deterministic, string-based representations so the
    overall hash remains stable.

    IMPORTANT: We deliberately avoid anything that could execute
    arbitrary code (no pickle, no __reduce__, no eval).
    """
    # Sets and frozensets: sort by repr for determinism.
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=repr)
    # Bytes: decode as UTF-8 with replacement for invalid sequences.
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj).decode("utf-8", errors="replace")
    # Everything else: stable repr.
    return repr(obj)


def canonicalize(payload: Any) -> str:
    """Return a deterministic JSON string representation of *payload*.

    Determinism guarantees:
      * Keys are sorted alphabetically.
      * No insignificant whitespace (compact separators).
      * Unicode is preserved as UTF-8 (ensure_ascii=False).
      * Non-serializable values get a stable fallback.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_fallback_serializer,
    )


def payload_hash(payload: Any) -> str:
    """Compute the SHA-256 hex digest of a canonicalized payload.

    Returns a 64-character lowercase hex string.
    """
    canonical = canonicalize(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()