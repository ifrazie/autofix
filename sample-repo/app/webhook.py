"""Outbound webhook delivery."""

import requests


def send_event(url: str, payload: dict) -> int:
    """POST an event to a subscriber. VULNERABLE: TLS verification disabled."""
    resp = requests.post(url, json=payload, verify=False, timeout=5)
    return resp.status_code
