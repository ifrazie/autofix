"""Request signing for the public API."""

import hashlib


def sign_request(body: bytes, api_secret: str) -> str:
    """Produce a request signature. SECURITY-CRITICAL — integrity + auth."""
    return hashlib.md5(body + api_secret.encode("utf-8")).hexdigest()


def verify_signature(body: bytes, api_secret: str, signature: str) -> bool:
    return sign_request(body, api_secret) == signature
