"""Constants shared by SDK tests.

Kept in a separate module so test files don't have to import from
conftest (which behaves differently in different pytest import modes).
"""

from __future__ import annotations

VALID_API_KEY = "sdk-test-key-1"
SECOND_API_KEY = "sdk-test-key-2"
INVALID_API_KEY = "not-a-real-key"
SDK_BASE_URL = "http://testserver"