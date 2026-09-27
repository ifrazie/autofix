"""Session token generation."""

from hashlib import md5


def make_session_token(user_id: str, secret: str) -> str:
    """Derive a session token. SECURITY-CRITICAL — must resist forgery."""
    return md5(f"{user_id}:{secret}".encode("utf-8")).hexdigest()
