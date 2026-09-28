"""CLI-level tests that don't touch a model backend (rules inspection + wiring)."""

import textwrap

from typer.testing import CliRunner

from autofix.cli import app

runner = CliRunner()


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


def test_fix_unknown_provider_fails_fast(tmp_path):
    rule = _write_rule(
        tmp_path / "r.yaml",
        """
        id: good
        rationale: "fine"
        match:
          call: "md5"
        transform:
          prompt: "fix"
        """,
    )
    result = runner.invoke(
        app, ["fix", str(tmp_path), "--rule", str(rule), "--provider", "nope"]
    )
    assert result.exit_code == 1
    assert "unknown provider" in result.output
