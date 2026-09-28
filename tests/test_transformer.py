"""Transformer apply-logic tests using a fake agent (no model / Ollama needed)."""

import time

import libcst as cst

from autofix.matcher import find_matches
from autofix.rules import MatchSpec, Rule
from autofix.transformer import (
    TransformResult,
    apply_to_source,
    changed_sources,
    plan_fixes,
)


def _ok(arg="b'a'"):
    return TransformResult(
        replacement_code=f"hashlib.sha256({arg})", confident=True, reason="safe"
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


# --- Reliability & resilience (Milestone 1) -------------------------------- #


class ConditionalAgent:
    """Raises when the matched snippet in the prompt contains ``marker``."""

    def __init__(self, marker):
        self.marker = marker

    def structured_output(self, model, prompt):
        if self.marker in prompt:
            raise RuntimeError("provider exploded")
        return _ok()


class SlowAgent:
    def __init__(self, delay):
        self.delay = delay

    def structured_output(self, model, prompt):
        time.sleep(self.delay)
        return _ok()


class FlakyAgent:
    """Fails the first ``fails`` calls, then succeeds. Not thread-shared in tests."""

    def __init__(self, fails):
        self.fails = fails
        self.calls = 0

    def structured_output(self, model, prompt):
        self.calls += 1
        if self.calls <= self.fails:
            raise RuntimeError("throttled")
        return _ok()


def test_one_failing_match_is_isolated_as_error(tmp_path):
    """A provider failure on one match records an 'error' and never sinks the run."""
    # Keep the two calls far apart so their context windows don't overlap
    # (otherwise the "ok" prompt would also carry the "boom" marker).
    src = "import hashlib\n" + "x = hashlib.md5(b'boom')\n" + "\n" * 12 + "y = hashlib.md5(b'ok')\n"
    p = _write(tmp_path, src)
    matches = find_matches(_rule("md5"), [p])
    assert len(matches) == 2

    results = plan_fixes(ConditionalAgent("boom"), matches, tmp_path, max_retries=0)

    by_status = {r.status for r in results}
    assert by_status == {"error", "fixed"}
    err = next(r for r in results if r.status == "error")
    assert "provider exploded" in err.reason
    # The healthy match still produced a real fix.
    assert any(r.status == "fixed" for r in results)


def test_transform_timeout_is_recorded_as_error(tmp_path):
    p = _write(tmp_path, "import hashlib\nx = hashlib.md5(b'a')\n")
    matches = find_matches(_rule("md5"), [p])

    results = plan_fixes(SlowAgent(delay=1.0), matches, tmp_path, timeout=0.1, max_retries=0)

    assert results[0].status == "error"
    assert "timed out" in results[0].reason


def test_transient_failure_is_retried_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr("autofix.transformer._RETRY_BACKOFF", 0.0)  # keep the test fast
    p = _write(tmp_path, "import hashlib\nx = hashlib.md5(b'a')\n")
    matches = find_matches(_rule("md5"), [p])
    agent = FlakyAgent(fails=2)

    results = plan_fixes(agent, matches, tmp_path, max_retries=2)

    assert results[0].status == "fixed"
    assert agent.calls == 3  # two failures + one success


def test_results_stay_in_match_order_under_concurrency(tmp_path):
    src = "import hashlib\n" + "".join(f"v{i} = hashlib.md5(b'{i}')\n" for i in range(6))
    p = _write(tmp_path, src)
    matches = find_matches(_rule("md5"), [p])
    assert len(matches) == 6

    results = plan_fixes(FakeAgent([_ok() for _ in matches]), matches, tmp_path, concurrency=4)

    # Every result lines up with its own match, in source order, despite parallelism.
    assert [r.match.line for r in results] == [m.line for m in matches]
    assert all(r.status == "fixed" for r in results)
