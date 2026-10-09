"""Pipeline task test — stays green whether the call is left as-is or rewritten.

The shell pipeline here is deliberately un-rewritable to shell=False, so Autofix
should skip it; this test just confirms the function still works when the real
process call is stubbed out.
"""

import subprocess
from types import SimpleNamespace

import pytest

from app import pipeline


@pytest.fixture
def fake_run(monkeypatch):
    def _run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="3\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _run)


def test_count_errors(fake_run):
    assert pipeline.count_errors("/var/log/app.log") == 3
