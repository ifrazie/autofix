"""Transformer apply-logic tests using a fake agent (no model / Ollama needed)."""

import libcst as cst

from autofix.matcher import find_matches
from autofix.rules import MatchSpec, Rule
from autofix.transformer import (
    TransformResult,
    apply_to_source,
    changed_sources,
    plan_fixes,
)


class FakeAgent:
    """Returns a scripted TransformResult per call."""

    def __init__(self, results):
        self._results = list(results)

    def structured_output(self, model, prompt):
        return self._results.pop(0)


def _rule(call):
    return Rule(id="md5", rationale="", match=MatchSpec(call=call), transform_prompt="fix it")


def _write(tmp_path, text):
    p = tmp_path / "m.py"
    p.write_text(text, encoding="utf-8")
    return p


def test_confident_fix_is_recorded_and_applies(tmp_path):
    p = _write(tmp_path, "import hashlib\nx = hashlib.md5(b'a').hexdigest()\n")
    matches = find_matches(_rule("md5"), [p])
    agent = FakeAgent([TransformResult(replacement_code="hashlib.sha256(b'a')", confident=True, reason="safe")])

    results = plan_fixes(agent, matches, tmp_path)
    assert len(results) == 1 and results[0].status == "fixed"
    assert results[0].diff and "sha256" in results[0].diff

    new = changed_sources(results)[p]
    assert "sha256" in new and "md5" not in new
    cst.parse_module(new)  # still parses


def test_redundant_trailing_hexdigest_is_stripped(tmp_path):
    """Model appends .hexdigest() to the inner-call snippet -> must not double up."""
    p = _write(tmp_path, "import hashlib\nx = hashlib.md5(b'a').hexdigest()\n")
    matches = find_matches(_rule("md5"), [p])
    agent = FakeAgent(
        [TransformResult(replacement_code="hashlib.sha256(b'a').hexdigest()", confident=True, reason="ok")]
    )
    results = plan_fixes(agent, matches, tmp_path)
    new = changed_sources(results)[p]
    assert new.count(".hexdigest()") == 1
    assert "hashlib.sha256(b'a').hexdigest()" in new


def test_low_confidence_is_skipped(tmp_path):
    p = _write(tmp_path, "import hashlib\nx = hashlib.md5(b'a')\n")
    matches = find_matches(_rule("md5"), [p])
    agent = FakeAgent([TransformResult(replacement_code="whatever", confident=False, reason="ambiguous checksum")])

    results = plan_fixes(agent, matches, tmp_path)
    assert results[0].status == "skipped"
    assert "ambiguous" in results[0].reason
    assert changed_sources(results) == {}


def test_invalid_model_output_is_skipped_not_applied(tmp_path):
    p = _write(tmp_path, "import hashlib\nx = hashlib.md5(b'a')\n")
    matches = find_matches(_rule("md5"), [p])
    agent = FakeAgent([TransformResult(replacement_code="def (((", confident=True, reason="oops")])

    results = plan_fixes(agent, matches, tmp_path)
    assert results[0].status == "skipped"
    assert "invalid" in results[0].reason.lower()


def test_apply_preserves_formatting(tmp_path):
    src = "import hashlib\n\n\nx  =  hashlib.md5( b'a' )   # keep me\n"
    p = _write(tmp_path, src)
    (m,) = find_matches(_rule("md5"), [p])
    expr = cst.parse_expression("hashlib.sha256(b'a')")
    new = apply_to_source(src, {m.span: expr})
    assert "# keep me" in new
    assert new.startswith("import hashlib\n\n\n")


def test_nested_hexdigest_call_is_preserved(tmp_path):
    """Regression: replacing the inner md5 call must keep the outer .hexdigest()."""
    p = _write(tmp_path, "import hashlib\nx = hashlib.md5(b'a').hexdigest()\n")
    (m,) = find_matches(_rule("md5"), [p])
    expr = cst.parse_expression("hashlib.sha256(b'a')")
    new = apply_to_source("import hashlib\nx = hashlib.md5(b'a').hexdigest()\n", {m.span: expr})
    assert new.strip().endswith("hashlib.sha256(b'a').hexdigest()")
