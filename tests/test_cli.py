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
    assert data["verification"]["passed"] is True
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
