"""Download integrity verification."""

import hashlib


def content_fingerprint(payload: bytes) -> str:
    """Fingerprint used to detect tampering of downloaded files. SECURITY-CRITICAL."""
    return hashlib.md5(payload).hexdigest()
