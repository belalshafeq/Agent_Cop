"""Tests for app.hashing — canonical JSON + SHA-256."""

from __future__ import annotations

from app.hashing import canonicalize, payload_hash


class TestCanonicalize:
    """Verify that canonicalize() produces deterministic JSON."""

    def test_sorted_keys(self):
        """Key order in the input must not affect output."""
        a = canonicalize({"b": 2, "a": 1})
        b = canonicalize({"a": 1, "b": 2})
        assert a == b
        assert a == '{"a":1,"b":2}'

    def test_compact_separators(self):
        """No insignificant whitespace in the output."""
        out = canonicalize({"a": 1, "b": [1, 2]})
        assert " " not in out
        assert out == '{"a":1,"b":[1,2]}'

    def test_unicode_preserved(self):
        """Unicode is preserved as UTF-8, not escaped."""
        out = canonicalize({"greeting": "héllo"})
        assert "héllo" in out

    def test_nested_deterministic(self):
        """Deeply nested structures canonicalize identically."""
        a = canonicalize({"x": {"b": 1, "a": 2}, "y": [3, {"c": 4, "b": 5}]})
        b = canonicalize({"y": [3, {"b": 5, "c": 4}], "x": {"a": 2, "b": 1}})
        assert a == b

    def test_sets_serialized_deterministically(self):
        """Sets fall back to sorted repr — deterministic."""
        a = canonicalize({"tags": {"x", "y", "z"}})
        b = canonicalize({"tags": {"z", "y", "x"}})
        assert a == b

    def test_bytes_decoded(self):
        """Bytes decode as UTF-8 with replacement, not crash."""
        out = canonicalize({"data": b"hello"})
        assert '"hello"' in out


class TestPayloadHash:
    """Verify SHA-256 hashing of canonicalized payloads."""

    def test_returns_64_hex_chars(self):
        h = payload_hash({"a": 1})
        assert isinstance(h, str)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_key_order_independent(self):
        """Same content, different key order → same hash."""
        h1 = payload_hash({"b": 2, "a": 1})
        h2 = payload_hash({"a": 1, "b": 2})
        assert h1 == h2

    def test_different_content_different_hash(self):
        """Different content → different hash."""
        h1 = payload_hash({"a": 1})
        h2 = payload_hash({"a": 2})
        assert h1 != h2

    def test_stable_across_calls(self):
        """Same input, called twice → same hash."""
        h1 = payload_hash({"x": "y", "n": 42})
        h2 = payload_hash({"x": "y", "n": 42})
        assert h1 == h2

    def test_empty_dict_hashes(self):
        """Empty payload produces a valid hash (not a crash)."""
        h = payload_hash({})
        assert len(h) == 64

    def test_non_json_serializable_does_not_crash(self):
        """Non-JSON-native values fall back safely."""
        # Sets, bytes, and custom objects should hash without error.
        h = payload_hash({"set": {1, 2, 3}, "bytes": b"raw"})
        assert len(h) == 64