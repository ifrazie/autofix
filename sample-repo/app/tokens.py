"""Session token generation."""

import hashlib


def make_session_token(user_id: str, secret: str) -> str:
    """Derive a session token. SECURITY-CRITICAL — must resist forgery."""
    return hashlib.md5(f"{user_id}:{secret}".encode("utf-8")).hexdigest()
