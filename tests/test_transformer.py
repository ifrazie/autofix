"""Transformer apply-logic tests using a fake agent (no model / Ollama needed)."""

import time

import libcst as cst

from autofix.matcher import find_matches
from autofix.rules import MatchSpec, Rule
from autofix.transformer import (
    TransformResult,
    _context_for,
    apply_to_source,
    changed_sources,
    estimate_usage,
    estimated_cost_usd,
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


def _rule(call, ensure_imports=()):
    return Rule(
        id="md5",
        rationale="",
        match=MatchSpec(call=call),
        transform_prompt="fix it",
        ensure_imports=tuple(ensure_imports),
    )


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


# --- Rule & fix power: import unlock (Milestone 2) ------------------------- #


def test_ensure_import_added_when_missing(tmp_path):
    """A rule's ensure_import is injected into the file when its fix applies."""
    p = _write(tmp_path, "token = random.randint(0, 999999)\n")
    rule = _rule("random", ensure_imports=("import secrets",))
    matches = find_matches(rule, [p])
    assert len(matches) == 1
    agent = FakeAgent(
        [TransformResult(replacement_code="secrets.randbelow(1000000)", confident=True, reason="token")]
    )

    results = plan_fixes(agent, matches, tmp_path)
    new = changed_sources(results)[p]

    assert "import secrets" in new
    assert "secrets.randbelow(1000000)" in new
    assert "random.randint" not in new
    cst.parse_module(new)  # still parses


def test_ensure_import_not_duplicated_when_present(tmp_path):
    p = _write(tmp_path, "import secrets\ntoken = random.randint(0, 9)\n")
    rule = _rule("random", ensure_imports=("import secrets",))
    matches = find_matches(rule, [p])
    agent = FakeAgent(
        [TransformResult(replacement_code="secrets.randbelow(10)", confident=True, reason="token")]
    )

    new = changed_sources(plan_fixes(agent, matches, tmp_path))[p]
    assert new.count("import secrets") == 1


def test_ensure_import_skipped_when_no_fix_applies(tmp_path):
    """No confident fix -> nothing changes, so the import is not added either."""
    p = _write(tmp_path, "token = random.randint(0, 9)\n")
    rule = _rule("random", ensure_imports=("import secrets",))
    matches = find_matches(rule, [p])
    agent = FakeAgent(
        [TransformResult(replacement_code="x", confident=False, reason="non-security use")]
    )

    assert changed_sources(plan_fixes(agent, matches, tmp_path)) == {}


# --- CLI UX & config: usage estimate (Milestone 3) ------------------------ #


def test_estimate_usage_counts_calls_and_scales_with_output(tmp_path):
    p = _write(tmp_path, "import hashlib\nx = hashlib.md5(b'a')\n")
    matches = find_matches(_rule("md5"), [p])
    agent = FakeAgent([_ok()])

    results = plan_fixes(agent, matches, tmp_path)
    usage = estimate_usage(matches, results)

    assert usage.calls == 1
    assert usage.input_tokens > 0
    assert usage.output_tokens > 0
    assert usage.total_tokens == usage.input_tokens + usage.output_tokens


def test_estimated_cost_none_for_local_provider():
    usage = estimate_usage([], [])
    assert estimated_cost_usd(usage, "ollama") is None


def test_estimated_cost_positive_for_bedrock():
    from autofix.transformer import UsageEstimate

    cost = estimated_cost_usd(
        UsageEstimate(calls=2, input_tokens=1000, output_tokens=500), "bedrock"
    )
    assert cost is not None and cost > 0


# --- Patch validation & reporting (P1 item 5) ----------------------------- #


def test_no_op_replacement_is_skipped_not_fixed(tmp_path):
    """A 'fix' identical to the original changes nothing and must not count."""
    p = _write(tmp_path, "import hashlib\nx = hashlib.md5(b'a')\n")
    matches = find_matches(_rule("md5"), [p])
    agent = FakeAgent(
        [TransformResult(replacement_code="hashlib.md5(b'a')", confident=True, reason="loop")]
    )

    results = plan_fixes(agent, matches, tmp_path)
    assert results[0].status == "skipped"
    assert "no-op" in results[0].reason.lower()
    assert changed_sources(results) == {}


def test_same_span_conflict_keeps_one_and_drops_the_other(tmp_path):
    """Two rules fixing the exact same call: one lands, the other is reported dropped."""
    p = _write(tmp_path, "import hashlib\nx = hashlib.md5(b'a')\n")
    rule_a = Rule(id="rule-a", rationale="", match=MatchSpec(call="md5"), transform_prompt="x")
    rule_b = Rule(id="rule-b", rationale="", match=MatchSpec(call="md5"), transform_prompt="x")
    matches = find_matches(rule_a, [p]) + find_matches(rule_b, [p])
    assert len(matches) == 2

    agent = FakeAgent([
        TransformResult(replacement_code="hashlib.sha256(b'a')", confident=True, reason="a"),
        TransformResult(replacement_code="hashlib.blake2b(b'a')", confident=True, reason="b"),
    ])
    results = plan_fixes(agent, matches, tmp_path)
    new = changed_sources(results)[p]

    applied = [r for r in results if r.status == "fixed" and r.applied]
    dropped = [r for r in results if r.status == "fixed" and not r.applied]
    assert len(applied) == 1 and len(dropped) == 1
    assert dropped[0].conflict and "overlaps" in dropped[0].conflict
    # Exactly one replacement landed; the file still parses and isn't double-edited.
    assert ("sha256" in new) ^ ("blake2b" in new)
    cst.parse_module(new)


def test_nested_span_conflict_is_detected(tmp_path):
    """An inner call and the outer call that contains it cannot both be spliced."""
    p = _write(tmp_path, "import hashlib\nx = str(hashlib.md5(b'a'))\n")
    inner = Rule(id="inner", rationale="", match=MatchSpec(call="md5"), transform_prompt="x")
    outer = Rule(id="outer", rationale="", match=MatchSpec(call="str"), transform_prompt="x")
    matches = find_matches(inner, [p]) + find_matches(outer, [p])
    assert len(matches) == 2

    agent = FakeAgent([
        TransformResult(replacement_code="hashlib.sha256(b'a')", confident=True, reason="in"),
        TransformResult(replacement_code="repr(hashlib.md5(b'a'))", confident=True, reason="out"),
    ])
    results = plan_fixes(agent, matches, tmp_path)
    new = changed_sources(results)[p]

    applied = [r for r in results if r.status == "fixed" and r.applied]
    dropped = [r for r in results if r.status == "fixed" and not r.applied]
    assert len(applied) == 1 and len(dropped) == 1
    cst.parse_module(new)  # whichever won, the result is valid Python


def test_applied_fix_diff_includes_added_import(tmp_path):
    """The reviewed diff is regenerated from the final file, so it shows imports."""
    p = _write(tmp_path, "token = random.randint(0, 9)\n")
    rule = _rule("random", ensure_imports=("import secrets",))
    matches = find_matches(rule, [p])
    agent = FakeAgent(
        [TransformResult(replacement_code="secrets.randbelow(10)", confident=True, reason="t")]
    )

    results = plan_fixes(agent, matches, tmp_path)
    # Before combining, the provisional diff can't know about the import.
    changed_sources(results)
    # After combining, the diff reflects the final file including the new import.
    assert results[0].diff is not None
    assert "import secrets" in results[0].diff
    assert "secrets.randbelow(10)" in results[0].diff


# --- Model context (P1 item 6) -------------------------------------------- #


def test_context_includes_imports_and_enclosing_function():
    source = (
        "import hashlib\n"
        "import os\n"
        "\n"
        "def helper():\n"
        "    return 1\n"
        "\n"
        "def hash_password(pw, salt):\n"
        "    # derive a digest\n"
        "    digest = hashlib.md5((salt + pw).encode())\n"
        "    return digest.hexdigest()\n"
    )
    # The md5 call is on line 9.
    ctx = _context_for(source, 9)
    # Imports are carried in so the model knows what's available.
    assert "import hashlib" in ctx
    # The enclosing function signature + body is present...
    assert "def hash_password(pw, salt):" in ctx
    assert "digest = hashlib.md5" in ctx
    # ...but the unrelated earlier function is not dragged along.
    assert "def helper():" not in ctx


def test_context_is_bounded_by_budget():
    import autofix.transformer as t

    big_body = "\n".join(f"    a{i} = {i}" for i in range(2000))
    source = "import hashlib\n\ndef f():\n" + big_body + "\n    x = hashlib.md5(b'a')\n"
    line = source.count("\n")  # last line, the md5 call
    ctx = _context_for(source, line)
    assert len(ctx) <= t._CONTEXT_BUDGET
    assert "import hashlib" in ctx  # imports survive trimming


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


def test_timeout_actually_bounds_wall_clock(tmp_path):
    """A stalled model call must not keep the run alive far past the deadline."""
    p = _write(tmp_path, "import hashlib\nx = hashlib.md5(b'a')\n")
    matches = find_matches(_rule("md5"), [p])

    start = time.monotonic()
    results = plan_fixes(SlowAgent(delay=30.0), matches, tmp_path, timeout=0.2, max_retries=0)
    elapsed = time.monotonic() - start

    assert results[0].status == "error" and "timed out" in results[0].reason
    # We must return shortly after the 0.2s budget, not wait out the 30s call.
    assert elapsed < 5.0


def test_permanent_error_is_not_retried(tmp_path):
    """A non-transient failure fails fast instead of burning the retry budget."""
    p = _write(tmp_path, "import hashlib\nx = hashlib.md5(b'a')\n")
    matches = find_matches(_rule("md5"), [p])

    class PermanentAgent:
        def __init__(self):
            self.calls = 0

        def structured_output(self, model, prompt):
            self.calls += 1
            raise ValueError("invalid request: schema violation")

    agent = PermanentAgent()
    results = plan_fixes(agent, matches, tmp_path, max_retries=5)

    assert results[0].status == "error"
    assert agent.calls == 1  # tried once, never retried


def test_retries_stop_at_deadline(tmp_path, monkeypatch):
    """Transient failures stop retrying once the deadline passes, not forever."""
    monkeypatch.setattr("autofix.transformer._RETRY_BACKOFF", 0.0)
    p = _write(tmp_path, "import hashlib\nx = hashlib.md5(b'a')\n")
    matches = find_matches(_rule("md5"), [p])

    class AlwaysThrottledAgent:
        def __init__(self):
            self.calls = 0

        def structured_output(self, model, prompt):
            self.calls += 1
            time.sleep(0.05)
            raise RuntimeError("throttled: rate limit exceeded")

    agent = AlwaysThrottledAgent()
    results = plan_fixes(agent, matches, tmp_path, timeout=0.2, max_retries=1000)

    assert results[0].status == "error"
    # A huge retry budget must still be bounded by the 0.2s deadline, so only a
    # handful of attempts fit — never 1000.
    assert agent.calls < 50


def test_completed_matches_fire_before_a_slow_one(tmp_path):
    """on_result fires for fast matches without waiting on a slow sibling."""
    # Space the calls far apart so their 4-line context windows don't overlap,
    # otherwise a neighbor's prompt would also carry the slow 'SLOW' marker.
    blocks = []
    for i in range(4):
        marker = "SLOW" if i == 0 else f"ok{i}"
        blocks.append(f"v{i} = hashlib.md5(b'{marker}')\n" + "\n" * 12)
    src = "import hashlib\n" + "".join(blocks)
    p = _write(tmp_path, src)
    matches = find_matches(_rule("md5"), [p])
    assert len(matches) == 4

    class MixedAgent:
        """One deliberately-slow call (the 'SLOW' snippet); the rest are instant."""

        def structured_output(self, model, prompt):
            if "SLOW" in prompt:
                time.sleep(0.6)
            return _ok()

    seen: list[float] = []
    start = time.monotonic()
    plan_fixes(
        MixedAgent(), matches, tmp_path,
        concurrency=4, timeout=5.0,
        on_result=lambda _r: seen.append(time.monotonic() - start),
    )

    # The three instant matches should report well before the 0.6s slow one.
    assert sum(1 for t in seen if t < 0.4) >= 3


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
