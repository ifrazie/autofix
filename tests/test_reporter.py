"""Reporter tests: content, and graceful color degradation when not a TTY.

capsys captures a non-terminal stream, so click strips ANSI styles — asserting no
escape codes appear is exactly the "readable when piped to a file" check.
"""

from pathlib import Path

from autofix import reporter
from autofix.matcher import find_matches
from autofix.rules import MatchSpec, Rule
from autofix.transformer import FixResult
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
