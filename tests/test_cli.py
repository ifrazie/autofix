"""CLI-level tests that don't touch a model backend (rules inspection + wiring).

The ``fix`` pipeline is exercised offline by monkeypatching ``preflight`` and
``make_agent`` so a scripted FakeAgent stands in for the model — no billable call.
"""

import json
import textwrap

from typer.testing import CliRunner

from autofix.cli import app
from autofix.transformer import TransformResult

runner = CliRunner()


class FakeAgent:
    def __init__(self, results):
        self._results = list(results)

    def structured_output(self, model, prompt):
        return self._results.pop(0)


def _patch_agent(monkeypatch, results):
    monkeypatch.setattr("autofix.cli.preflight", lambda *a, **k: "fake-model")
    monkeypatch.setattr("autofix.cli.make_agent", lambda *a, **k: FakeAgent(results))


def _sha256(arg="b'a'"):
    return TransformResult(
        replacement_code=f"hashlib.sha256({arg})", confident=True, reason="password hash"
    )


def _target_repo(tmp_path):
    """A tiny target dir with one md5 call and a matching rule file."""
    target = tmp_path / "repo"
    (target / "app").mkdir(parents=True)
    (target / "app" / "auth.py").write_text(
        "import hashlib\ntoken = hashlib.md5(b'a').hexdigest()\n", encoding="utf-8"
    )
    rule = tmp_path / "md5.yaml"
    _write_rule(
        rule,
        """
        id: md5-to-sha256
        rationale: "md5 is broken"
        match:
          call: "md5"
        transform:
          prompt: "use sha256"
        """,
    )
    return target, rule


def _write_rule(path, text):
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def test_version_flag():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.startswith("autofix ")


def test_rules_validate_reports_valid_and_invalid(tmp_path):
    _write_rule(
        tmp_path / "good.yaml",
        """
        id: good
        rationale: "fine"
        match:
          call: "md5"
        transform:
          prompt: "fix"
        """,
    )
    _write_rule(
        tmp_path / "bad.yaml",
        """
        id: bad
        match:
          call: "md5"
        transform:
          prompt: "fix"
        """,
    )

    result = runner.invoke(app, ["rules", "validate", "--rules-dir", str(tmp_path)])
    # Any invalid rule makes the command exit non-zero.
    assert result.exit_code == 1
    assert "[OK]" in result.output
    assert "[BAD]" in result.output
    assert "1 valid, 1 invalid" in result.output


def test_rules_validate_all_valid_exits_zero(tmp_path):
    _write_rule(
        tmp_path / "good.yaml",
        """
        id: good
        rationale: "fine"
        match:
          call: "md5"
        transform:
          prompt: "fix"
        """,
    )
    result = runner.invoke(app, ["rules", "validate", "--rule", str(tmp_path / "good.yaml")])
    assert result.exit_code == 0
    assert "1 valid, 0 invalid" in result.output


def test_rules_validate_needs_a_source():
    result = runner.invoke(app, ["rules", "validate"])
    assert result.exit_code == 1
    assert "no rules given" in result.output


def test_rules_list_shows_ids_and_rationale(tmp_path):
    _target_repo(tmp_path)  # writes tmp_path/md5.yaml
    result = runner.invoke(app, ["rules", "list", "--rule", str(tmp_path / "md5.yaml")])
    assert result.exit_code == 0
    assert "[md5-to-sha256]" in result.output
    assert "md5 is broken" in result.output
    assert "1 rule(s)" in result.output


def test_fix_unknown_provider_fails_fast(tmp_path):
    target, rule = _target_repo(tmp_path)
    result = runner.invoke(
        app, ["fix", str(target), "--rule", str(rule), "--provider", "nope"]
    )
    assert result.exit_code == 1
    assert "unknown provider" in result.output


def test_fix_dry_run_writes_nothing_and_reports_dry(tmp_path, monkeypatch):
    target, rule = _target_repo(tmp_path)
    before = (target / "app" / "auth.py").read_text(encoding="utf-8")
    _patch_agent(monkeypatch, [_sha256()])

    result = runner.invoke(
        app, ["fix", str(target), "--rule", str(rule), "--dry-run", "--json"]
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["dry_run"] is True
    assert "verification" not in data          # no branch/commit in dry-run
    assert data["summary"]["fixed"] == 1
    # The target on disk is untouched.
    assert (target / "app" / "auth.py").read_text(encoding="utf-8") == before


def test_fix_json_report_parses_and_has_verification(tmp_path, monkeypatch):
    target, rule = _target_repo(tmp_path)
    _patch_agent(monkeypatch, [_sha256()])

    result = runner.invoke(
        app, ["fix", str(target), "--rule", str(rule), "--no-test", "--json"]
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["summary"]["fixed"] == 1
    assert data["verification"]["tests_ran"] is False
    assert data["verification"]["state"] == "skipped"
    # --no-test proves nothing, so it must NOT be reported as a pass.
    assert data["verification"]["passed"] is False
    assert data["usage"]["approximate"] is True


def test_fix_no_matches_json_is_empty_summary(tmp_path, monkeypatch):
    target, rule = _target_repo(tmp_path)
    # Rewrite the target so nothing matches md5.
    (target / "app" / "auth.py").write_text("x = 1 + 2\n", encoding="utf-8")
    _patch_agent(monkeypatch, [])

    result = runner.invoke(app, ["fix", str(target), "--rule", str(rule), "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["summary"]["matched"] == 0
    assert data["results"] == []


def _target_repo_with_tests(tmp_path, *, test_body: str):
    """A target with a tiny module + a test, so verification really runs pytest."""
    target = tmp_path / "repo"
    (target / "app").mkdir(parents=True)
    (target / "app" / "__init__.py").write_text("", encoding="utf-8")
    (target / "app" / "mod.py").write_text(
        "import hashlib\n\n\ndef digest(b):\n    return hashlib.md5(b).hexdigest()\n",
        encoding="utf-8",
    )
    (target / "conftest.py").write_text(
        "import os, sys\nsys.path.insert(0, os.path.dirname(__file__))\n", encoding="utf-8"
    )
    tests = target / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("", encoding="utf-8")
    (tests / "test_mod.py").write_text(test_body, encoding="utf-8")
    rule = tmp_path / "md5.yaml"
    _write_rule(
        rule,
        """
        id: md5-to-sha256
        rationale: "md5 is broken"
        match:
          call: "md5"
        transform:
          prompt: "use sha256"
        """,
    )
    return target, rule


def test_fix_green_run_exits_zero(tmp_path, monkeypatch):
    # Test only asserts the digest is a non-empty hex string -> stays green after fix.
    body = (
        "from app.mod import digest\n\n"
        "def test_digest():\n"
        "    h = digest(b'a')\n"
        "    assert isinstance(h, str) and len(h) > 0\n"
    )
    target, rule = _target_repo_with_tests(tmp_path, test_body=body)
    _patch_agent(monkeypatch, [_sha256()])

    result = runner.invoke(app, ["fix", str(target), "--rule", str(rule), "--json"])
    data = json.loads(result.output)
    assert data["verification"]["state"] == "passed"
    assert result.exit_code == 0


def test_fix_failing_tests_exit_nonzero(tmp_path, monkeypatch):
    # Test pins the exact md5 digest, so swapping to sha256 breaks it -> red run.
    body = (
        "from app.mod import digest\n\n"
        "def test_digest():\n"
        "    assert digest(b'a') == '0cc175b9c0f1b6a831c399e269772661'\n"
    )
    target, rule = _target_repo_with_tests(tmp_path, test_body=body)
    _patch_agent(monkeypatch, [_sha256()])

    result = runner.invoke(app, ["fix", str(target), "--rule", str(rule), "--json"])
    data = json.loads(result.output)
    assert data["verification"]["state"] == "failed"
    assert data["verification"]["passed"] is False
    # A red verification must fail the process so CI notices.
    assert result.exit_code == 1


def test_fix_no_tests_collected_exits_nonzero(tmp_path, monkeypatch):
    # A target with a match but no tests at all: nothing is verified -> not a pass.
    target, rule = _target_repo(tmp_path)
    _patch_agent(monkeypatch, [_sha256()])

    result = runner.invoke(app, ["fix", str(target), "--rule", str(rule), "--json"])
    data = json.loads(result.output)
    assert data["verification"]["state"] == "no_tests"
    assert data["verification"]["passed"] is False
    assert result.exit_code == 1


def test_fix_errored_transform_exits_nonzero(tmp_path, monkeypatch):
    target, rule = _target_repo(tmp_path)

    class BoomAgent:
        def structured_output(self, model, prompt):
            raise RuntimeError("provider exploded")

    monkeypatch.setattr("autofix.cli.preflight", lambda *a, **k: "fake-model")
    monkeypatch.setattr("autofix.cli.make_agent", lambda *a, **k: BoomAgent())

    result = runner.invoke(
        app, ["fix", str(target), "--rule", str(rule), "--max-retries", "0", "--json"]
    )
    data = json.loads(result.output)
    assert data["summary"]["errored"] == 1
    # An errored transform means the run can't be trusted as clean.
    assert result.exit_code == 1


# --- scan command (P2) ----------------------------------------------------- #


def test_scan_lists_matches_without_model(tmp_path):
    target, rule = _target_repo(tmp_path)
    result = runner.invoke(app, ["scan", str(target), "--rule", str(rule), "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["matched"] == 1
    assert data["by_rule"]["md5-to-sha256"] == 1
    assert data["matches"][0]["file"] == "app/auth.py"
    assert "rationale" in data["matches"][0]


def test_scan_reports_unreadable_and_exits_nonzero(tmp_path):
    target, rule = _target_repo(tmp_path)
    (target / "app" / "broken.py").write_text("def oops(:\n", encoding="utf-8")
    result = runner.invoke(app, ["scan", str(target), "--rule", str(rule), "--json"])
    data = json.loads(result.output)
    assert data["unreadable"] and data["unreadable"][0]["file"] == "app/broken.py"
    assert result.exit_code == 1


# --- plan-out + apply round-trip (P2) -------------------------------------- #


def test_fix_writes_plan_and_apply_replays_it(tmp_path, monkeypatch):
    # Build a target with a test that stays green after the md5->sha256 swap.
    target, rule = _target_repo_with_tests(
        tmp_path,
        test_body=(
            "from app.mod import digest\n\n"
            "def test_digest():\n"
            "    h = digest(b'a')\n"
            "    assert isinstance(h, str) and len(h) > 0\n"
        ),
    )
    plan_path = tmp_path / "plan.json"
    _patch_agent(monkeypatch, [_sha256()])

    # 1) fix emits a plan (and still verifies).
    fix_res = runner.invoke(
        app, ["fix", str(target), "--rule", str(rule), "--no-test",
              "--plan-out", str(plan_path), "--json"],
    )
    assert fix_res.exit_code == 0
    assert plan_path.is_file()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["files"][0]["path"] == "app/mod.py"

    # 2) apply replays the plan with NO model (no agent patched) and verifies green.
    apply_res = runner.invoke(app, ["apply", str(plan_path), "--json"])
    data = json.loads(apply_res.output)
    assert data["applied_files"] == ["app/mod.py"]
    assert data["aborted"] is False
    assert data["verification"]["state"] == "passed"
    assert apply_res.exit_code == 0


def test_apply_refuses_stale_plan(tmp_path, monkeypatch):
    target, rule = _target_repo_with_tests(
        tmp_path,
        test_body="def test_ok():\n    assert True\n",
    )
    plan_path = tmp_path / "plan.json"
    _patch_agent(monkeypatch, [_sha256()])
    runner.invoke(
        app, ["fix", str(target), "--rule", str(rule), "--no-test",
              "--plan-out", str(plan_path), "--json"],
    )

    # Drift the target after planning.
    (target / "app" / "mod.py").write_text("x = 1  # changed\n", encoding="utf-8")

    apply_res = runner.invoke(app, ["apply", str(plan_path), "--json"])
    data = json.loads(apply_res.output)
    assert data["aborted"] is True
    assert data["stale"] and data["stale"][0]["file"] == "app/mod.py"
    assert "verification" not in data
    assert apply_res.exit_code == 1


# --- doctor command (P2) --------------------------------------------------- #


def test_doctor_passes_when_provider_ok(monkeypatch):
    monkeypatch.setattr("autofix.doctor.check_ollama", lambda *a, **k: "deepseek-coder-v2:16b")
    result = runner.invoke(app, ["doctor", "--provider", "ollama", "--json"])
    data = json.loads(result.output)
    assert data["ok"] is True
    assert result.exit_code == 0


def test_doctor_fails_when_provider_unavailable(monkeypatch):
    from autofix.transformer import ProviderError

    def boom(*a, **k):
        raise ProviderError("no creds")

    monkeypatch.setattr("autofix.doctor.check_bedrock", boom)
    result = runner.invoke(app, ["doctor", "--provider", "bedrock", "--json"])
    data = json.loads(result.output)
    assert data["ok"] is False
    assert result.exit_code == 1
