"""Webhook test.

We stub the network call, so this stays green whether the code sends
``verify=False`` or the fixed ``verify=True`` — the point is that Autofix's edit
doesn't break the build, not to make a real HTTPS request.
"""

from types import SimpleNamespace

import pytest

from app import webhook


@pytest.fixture
def fake_post(monkeypatch):
    calls = []

    def _post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=202)

    monkeypatch.setattr(webhook.requests, "post", _post)
    return calls


def test_send_event(fake_post):
    assert webhook.send_event("https://example.test/hook", {"event": "ping"}) == 202
    assert len(fake_post) == 1
