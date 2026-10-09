"""Doctor diagnostic tests — provider checks are stubbed, no network/creds."""

from autofix import doctor
from autofix.doctor import FAIL, OK, WARN, Check, check_git, check_python, overall_ok, run_checks


def test_python_check_passes_on_supported_version():
    c = check_python()
    assert c.name == "python"
    assert c.status == OK  # the test runner is 3.11+


def test_git_check_detects_presence():
    c = check_git()
    # git is required for the project's own tests, so it is present in CI.
    assert c.name == "git"
    assert c.status in (OK, FAIL)


def test_overall_ok_tolerates_warnings_but_not_failures():
    assert overall_ok([Check("a", OK, ""), Check("b", WARN, "")]) is True
    assert overall_ok([Check("a", OK, ""), Check("b", FAIL, "")]) is False


def test_run_checks_reports_provider_failure(monkeypatch):
    from autofix.transformer import ProviderError

    def boom(*a, **k):
        raise ProviderError("no creds")

    monkeypatch.setattr(doctor, "check_bedrock", boom)
    checks = run_checks("bedrock")
    provider = next(c for c in checks if c.name.startswith("provider:"))
    assert provider.status == FAIL
    assert "no creds" in provider.detail
    assert overall_ok(checks) is False


def test_run_checks_provider_ok(monkeypatch):
    monkeypatch.setattr(doctor, "check_ollama", lambda *a, **k: "deepseek-coder-v2:16b")
    checks = run_checks("ollama")
    provider = next(c for c in checks if c.name == "provider:ollama")
    assert provider.status == OK
    assert "deepseek-coder-v2:16b" in provider.detail


def test_unknown_provider_fails(monkeypatch):
    checks = run_checks("nope")
    provider = next(c for c in checks if c.name.startswith("provider:"))
    assert provider.status == FAIL
