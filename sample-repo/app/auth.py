"""User authentication helpers."""

import hashlib


def hash_password(password: str, salt: str) -> str:
    """Hash a user password for storage. SECURITY-CRITICAL."""
    digest = hashlib.md5((salt + password).encode("utf-8"))
    return digest.hexdigest()


def verify_password(password: str, salt: str, stored: str) -> bool:
    return hash_password(password, salt) == stored
