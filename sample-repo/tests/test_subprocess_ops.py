"""Subprocess tests.

We stub out the real process call, so these stay green whether the code uses
``shell=True`` with a string command or ``shell=False`` with an argument list.
The point of the sample suite is to prove Autofix's edits don't break the build,
not to spawn real processes.
"""

import subprocess
from types import SimpleNamespace

import pytest

from app import backup, deploy, tasks


@pytest.fixture
def fake_process(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    def fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(wait=lambda: 0, returncode=0)

    def fake_call(*args, **kwargs):
        calls.append((args, kwargs))
        return 0

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(subprocess, "call", fake_call)
    return calls


def test_restart_service(fake_process):
    assert deploy.restart_service("nginx") == 0
    assert len(fake_process) == 1


def test_snapshot(fake_process):
    assert backup.snapshot("/var/data") == "backup.tgz"
    assert len(fake_process) == 1


def test_disk_usage(fake_process):
    assert tasks.disk_usage("/") == 0
    assert len(fake_process) == 1
