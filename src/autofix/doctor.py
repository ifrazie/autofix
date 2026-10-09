"""Environment diagnostics for ``autofix doctor``.

A quick pre-flight that answers "will a run work here?" without doing one: it
checks the Python version, that ``git`` is on PATH, that ``pytest`` can be
imported (the verifier shells out to it), and that the selected model provider is
reachable (AWS credentials for Bedrock, a running Ollama with the model pulled).

Each check returns a :class:`Check` with an ``ok`` flag and a human message, so
the same results drive both the terminal report and the JSON output, and so the
logic is unit-testable without spawning the CLI.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass

from .transformer import (
    DEFAULT_BEDROCK_MODEL,
    DEFAULT_HOST,
    DEFAULT_MODEL,
    DEFAULT_REGION,
    ProviderError,
    check_bedrock,
    check_ollama,
)

# OK / warning / failure. A warning doesn't fail the overall run (e.g. the
# non-selected provider being unavailable is fine); a failure does.
OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class Check:
    name: str
    status: str   # OK | WARN | FAIL
    detail: str


def check_python() -> Check:
    v = sys.version_info
    version = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) >= (3, 11):
        return Check("python", OK, f"Python {version}")
    return Check("python", FAIL, f"Python {version} (3.11+ required)")


def check_git() -> Check:
    if shutil.which("git") is None:
        return Check("git", FAIL, "git not found on PATH (required to branch/commit fixes)")
    try:
        out = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("git", FAIL, f"git failed to run: {exc}")
    return Check("git", OK, out.stdout.strip() or "git available")


def check_pytest() -> Check:
    try:
        import pytest  # noqa: F401
    except ImportError:
        return Check(
            "pytest", WARN,
            "pytest not importable; verification will report no tests. "
            "Install it in the target's environment to verify fixes.",
        )
    return Check("pytest", OK, f"pytest {getattr(pytest, '__version__', '?')}")


def check_provider(
    provider: str,
    *,
    model: str | None = None,
    region: str = DEFAULT_REGION,
    host: str = DEFAULT_HOST,
) -> Check:
    """Check that the *selected* provider is usable (credentials / reachability)."""

    try:
        if provider == "bedrock":
            resolved = check_bedrock(model or DEFAULT_BEDROCK_MODEL, region)
            return Check("provider:bedrock", OK, f"AWS credentials found; model {resolved}")
        if provider == "ollama":
            resolved = check_ollama(model or DEFAULT_MODEL, host)
            return Check("provider:ollama", OK, f"Ollama reachable; model {resolved}")
        return Check(f"provider:{provider}", FAIL, f"unknown provider '{provider}'")
    except ProviderError as exc:
        return Check(f"provider:{provider}", FAIL, str(exc))


def run_checks(
    provider: str,
    *,
    model: str | None = None,
    region: str = DEFAULT_REGION,
    host: str = DEFAULT_HOST,
) -> list[Check]:
    """Run every diagnostic for the selected provider, in report order."""

    return [
        check_python(),
        check_git(),
        check_pytest(),
        check_provider(provider, model=model, region=region, host=host),
    ]


def overall_ok(checks: list[Check]) -> bool:
    """True when no check hard-failed (warnings are tolerated)."""
    return all(c.status != FAIL for c in checks)
