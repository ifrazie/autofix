"""Auth tests — assert shape and stability, not a specific digest.

These stay green whether hashing uses md5 or sha256.
"""

from app.auth import hash_password, verify_password


def test_hash_is_hex_string():
    h = hash_password("hunter2", salt="abc")
    assert isinstance(h, str)
    assert len(h) > 0
    assert all(c in "0123456789abcdef" for c in h)


def test_hash_is_deterministic():
    assert hash_password("hunter2", "abc") == hash_password("hunter2", "abc")


def test_verify_roundtrip():
    stored = hash_password("hunter2", "abc")
    assert verify_password("hunter2", "abc", stored)
    assert not verify_password("wrong", "abc", stored)
