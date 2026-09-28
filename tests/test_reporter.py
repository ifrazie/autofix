"""Reporter tests: content, and graceful color degradation when not a TTY.

capsys captures a non-terminal stream, so click strips ANSI styles — asserting no
escape codes appear is exactly the "readable when piped to a file" check.
"""

import json
from pathlib import Path

from autofix import reporter
from autofix.matcher import find_matches
from autofix.rules import MatchSpec, Rule
from autofix.transformer import FixResult, UsageEstimate
from autofix.verifier import VerifyResult

ESC = "\x1b"  # start of an ANSI escape sequence


def _fixed_result(tmp_path):
    rule = Rule(
        id="md5-to-sha256",
        rationale="md5 is cryptographically broken; use sha256.",
        match=MatchSpec(call="md5"),
        transform_prompt="fix it",
    )
    p = tmp_path / "m.py"
    p.write_text("import hashlib\nx = hashlib.md5(b'a')\n", encoding="utf-8")
    (m,) = find_matches(rule, [p])
    diff = "--- a/m.py\n+++ b/m.py\n@@ -2 +2 @@\n-x = hashlib.md5(b'a')\n+x = hashlib.sha256(b'a')\n"
    return FixResult(m, "fixed", "used for a password hash", replacement_code="hashlib.sha256(b'a')", diff=diff)


def test_fix_report_shows_rule_rationale_and_diff_without_ansi(tmp_path, capsys):
    reporter.fixes([_fixed_result(tmp_path)], tmp_path)
    out = capsys.readouterr().out
    assert "FIXED" in out and "m.py:2" in out and "[md5-to-sha256]" in out
    assert "md5 is cryptographically broken" in out  # rule rationale
    assert "used for a password hash" in out          # per-fix reason
    assert "+x = hashlib.sha256(b'a')" in out          # diff body
    assert ESC not in out                              # color degraded (piped)


def test_no_matches_message(capsys):
    reporter.no_matches()
    assert "Nothing matched" in capsys.readouterr().out


def test_summary_counts(capsys):
    reporter.summary(8, 7, 1)
    out = capsys.readouterr().out
    assert "8 matched" in out and "7 fixed" in out and "1 skipped" in out


def test_verification_skipped_when_tests_not_run(capsys):
    vr = VerifyResult(
        workspace=Path("/tmp/ws"),
        branch="autofix/x",
        isolated=True,
        committed=["app/auth.py"],
        passed=True,
        pytest_output="",
        culprits=[],
        tests_ran=False,
    )
    reporter.verification(vr)
    out = capsys.readouterr().out
    assert "SKIPPED" in out and "--no-test" in out


def test_cost_line_shows_estimate_for_bedrock(capsys):
    reporter.cost(UsageEstimate(calls=3, input_tokens=1000, output_tokens=200), "bedrock")
    out = capsys.readouterr().out
    assert "approximate" in out and "$" in out and "3 call(s)" in out


def test_cost_line_notes_local_for_ollama(capsys):
    reporter.cost(UsageEstimate(calls=1, input_tokens=10, output_tokens=5), "ollama")
    out = capsys.readouterr().out
    assert "no API cost" in out and "$" not in out


def test_json_report_shape(tmp_path):
    result = _fixed_result(tmp_path)
    usage = UsageEstimate(calls=1, input_tokens=100, output_tokens=20)
    vr = VerifyResult(
        workspace=tmp_path,
        branch="autofix/x",
        isolated=True,
        committed=["m.py"],
        passed=True,
        pytest_output="",
        culprits=[],
    )
    out = reporter.json_report(
        [result], tmp_path, "bedrock", verify_result=vr, usage=usage, dry_run=False
    )
    data = json.loads(out)
    assert data["summary"] == {"matched": 1, "fixed": 1, "skipped": 0, "errored": 0}
    assert data["results"][0]["rule"] == "md5-to-sha256"
    assert data["results"][0]["status"] == "fixed"
    assert data["verification"]["passed"] is True
    assert data["usage"]["approximate"] is True
    assert data["usage"]["estimated_cost_usd"] is not None
    assert data["dry_run"] is False


def test_json_report_omits_verification_when_none(tmp_path):
    data = json.loads(reporter.json_report([], tmp_path, "ollama", dry_run=True))
    assert "verification" not in data
    assert data["summary"]["matched"] == 0
    assert data["dry_run"] is True


def test_verification_red_shows_culprit_callout(capsys):
    vr = VerifyResult(
        workspace=Path("/tmp/ws"),
        branch="autofix/x",
        isolated=True,
        committed=["app/auth.py"],
        passed=False,
        pytest_output="",
        culprits=["app/auth.py"],
    )
    reporter.verification(vr)
    out = capsys.readouterr().out
    assert "FAIL" in out and "LIKELY CULPRIT" in out and "app/auth.py" in out
    assert ESC not in out
