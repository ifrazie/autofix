"""In-memory response cache.

NOTE: the md5 here is NOT a security control. It is only a fast, compact cache
key derived from request parameters — collisions would at worst serve a slightly
stale entry, never a security failure. This is the ambiguous case Autofix should
recognize and leave alone.
"""

import hashlib


def cache_key(*parts: str) -> str:
    """Build a cache key from request parameters (non-security fingerprint)."""
    joined = "|".join(parts)
    return hashlib.md5(joined.encode("utf-8")).hexdigest()


_STORE: dict[str, object] = {}


def get_or_set(key_parts, factory):
    key = cache_key(*key_parts)
    if key not in _STORE:
        _STORE[key] = factory()
    return _STORE[key]
