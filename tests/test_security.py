"""Tests for app.security — Bearer auth, fail-closed behavior."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.security import _extract_bearer, _fingerprint, verify_api_key
from tests.conftest import INVALID_API_KEY, SECOND_API_KEY, VALID_API_KEY


class TestExtractBearer:
    """Verify the Authorization header parser."""

    def test_valid_header(self):
        assert _extract_bearer("Bearer abc123") == "abc123"

    def test_case_insensitive_scheme(self):
        assert _extract_bearer("bearer abc123") == "abc123"
        assert _extract_bearer("BEARER abc123") == "abc123"

    def test_whitespace_stripped(self):
        assert _extract_bearer("Bearer   abc123  ") == "abc123"

    def test_none_returns_none(self):
        assert _extract_bearer(None) is None

    def test_empty_string_returns_none(self):
        assert _extract_bearer("") is None

    def test_missing_scheme_returns_none(self):
        assert _extract_bearer("abc123") is None

    def test_wrong_scheme_returns_none(self):
        assert _extract_bearer("Basic abc123") is None

    def test_empty_token_returns_none(self):
        assert _extract_bearer("Bearer ") is None


class TestFingerprint:
    """Verify the log-correlation fingerprint."""

    def test_returns_8_hex_chars(self):
        f = _fingerprint("some-secret")
        assert isinstance(f, str)
        assert len(f) == 8
        assert all(c in "0123456789abcdef" for c in f)

    def test_deterministic(self):
        assert _fingerprint("x") == _fingerprint("x")

    def test_different_inputs_different_fingerprints(self):
        assert _fingerprint("x") != _fingerprint("y")

    def test_does_not_leak_secret(self):
        """Fingerprint is not the secret itself."""
        secret = "super-secret-value"
        f = _fingerprint(secret)
        assert secret not in f
        assert f != secret


class TestVerifyApiKey:
    """Verify the FastAPI auth dependency."""

    def test_valid_key_returns_fingerprint(self):
        fp = verify_api_key(f"Bearer {VALID_API_KEY}")
        assert isinstance(fp, str)
        assert len(fp) == 8

    def test_second_valid_key_also_works(self):
        fp = verify_api_key(f"Bearer {SECOND_API_KEY}")
        assert isinstance(fp, str)
        assert len(fp) == 8

    def test_different_keys_produce_different_fingerprints(self):
        fp1 = verify_api_key(f"Bearer {VALID_API_KEY}")
        fp2 = verify_api_key(f"Bearer {SECOND_API_KEY}")
        assert fp1 != fp2

    def test_missing_header_raises_401(self):
        with pytest.raises(HTTPException) as exc_info:
            verify_api_key(None)
        assert exc_info.value.status_code == 401

    def test_malformed_header_raises_401(self):
        with pytest.raises(HTTPException) as exc_info:
            verify_api_key("NotBearer abc123")
        assert exc_info.value.status_code == 401

    def test_invalid_key_raises_401(self):
        with pytest.raises(HTTPException) as exc_info:
            verify_api_key(f"Bearer {INVALID_API_KEY}")
        assert exc_info.value.status_code == 401

    def test_401_includes_www_authenticate_header(self):
        with pytest.raises(HTTPException) as exc_info:
            verify_api_key(None)
        headers = exc_info.value.headers or {}
        assert "WWW-Authenticate" in headers
        assert "Bearer" in headers["WWW-Authenticate"]