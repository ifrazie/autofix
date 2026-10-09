"""AI transformer.

For each match, hand a model the matched snippet plus surrounding context and the
rule's transform prompt, and ask for a *structured* result: the replacement code,
a confidence flag, and a reason. Our code — not the model — decides what to do with
it (splice or skip; see matcher/verifier). Keeping the model on a short leash (one
snippet in, one structured object out) is what keeps the result trustworthy.

Two interchangeable backends sit behind the same `Agent.structured_output` call:
a cloud provider (AWS Bedrock, the default) and a local one (Ollama). The provider
choice never touches the transform logic below — only how the agent is built.
"""

from __future__ import annotations

import difflib
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

import libcst as cst
from libcst.metadata import MetadataWrapper, PositionProvider
from pydantic import BaseModel, Field

from .matcher import Match

# Providers ----------------------------------------------------------------- #
PROVIDERS = ("bedrock", "ollama")
DEFAULT_PROVIDER = "bedrock"

# Bedrock (cloud, default): Claude Sonnet 4.5 via its us-east-1 inference profile.
DEFAULT_BEDROCK_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
DEFAULT_REGION = "us-east-1"

# Ollama (local fallback): kept so the tool still runs with no cloud/key/cost.
DEFAULT_MODEL = "deepseek-coder-v2"
DEFAULT_HOST = "http://localhost:11434"

# Resilience defaults -------------------------------------------------------- #
# How many matches to transform in parallel. Bounded so a large repo doesn't fan
# out into hundreds of simultaneous model calls and trip provider rate limits.
DEFAULT_CONCURRENCY = 4
# Overall per-match budget (seconds), including any retries; a match that exceeds
# it is recorded as an error rather than hanging the whole run.
DEFAULT_TIMEOUT = 120.0
# Extra attempts after the first for transient (e.g. throttling/network) failures.
DEFAULT_MAX_RETRIES = 2
# Base seconds for exponential backoff between retries (base * 2**attempt).
_RETRY_BACKOFF = 0.5

# Substrings that mark an exception as transient (worth retrying). Matched case-
# insensitively against the exception type name and message. Anything that does
# not match is treated as permanent and NOT retried, so a deterministic failure
# (e.g. a validation or auth error) fails fast instead of burning the retry budget.
_TRANSIENT_MARKERS = (
    "throttl",
    "rate limit",
    "rateexceeded",
    "too many requests",
    "timeout",
    "timed out",
    "temporarily",
    "unavailable",
    "service unavailable",
    "connection",
    "reset by peer",
    "econnreset",
    "429",
    "500",
    "502",
    "503",
    "504",
)


def _is_transient(exc: BaseException) -> bool:
    """Classify an exception as transient (retryable) vs. permanent.

    Transient failures (throttling, 5xx, connection resets, timeouts) are worth
    retrying with backoff; a permanent failure (bad request, auth, validation)
    will fail the same way every time, so retrying only wastes the deadline.
    """

    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


class TransformTimeout(RuntimeError):
    """Raised when a single match's transform exceeds its deadline."""


def default_model_for(provider: str) -> str:
    """The provider's default model id."""
    return DEFAULT_BEDROCK_MODEL if provider == "bedrock" else DEFAULT_MODEL


class TransformResult(BaseModel):
    """The structured output we require from the model for one match."""

    replacement_code: str = Field(
        description=(
            "The corrected replacement for ONLY the matched call expression, "
            "as valid Python."
        )
    )
    confident: bool = Field(
        description="True only if the fix is correct and safe to apply; False to skip."
    )
    reason: str = Field(
        description=(
            "Why the fix is safe, or - when not confident - why the match "
            "should be left alone."
        )
    )


class ProviderError(RuntimeError):
    """Raised when the selected model backend can't be reached or used."""


class OllamaError(ProviderError):
    """Raised when Ollama is unreachable or the model is unavailable."""


class BedrockError(ProviderError):
    """Raised when AWS Bedrock can't be used (no creds / boto3 missing)."""


def build_agent(model_id: str = DEFAULT_MODEL, host: str = DEFAULT_HOST):
    """Create a strands Agent backed by a local Ollama model.

    Imports are local so the deterministic parts of the tool (matcher, rules)
    stay importable even if the AI stack isn't installed.
    """

    from strands import Agent
    from strands.models.ollama import OllamaModel

    model = OllamaModel(host=host, model_id=model_id, temperature=0)
    return Agent(model=model)


def build_bedrock_agent(model_id: str = DEFAULT_BEDROCK_MODEL, region: str = DEFAULT_REGION):
    """Create a strands Agent backed by an AWS Bedrock model.

    Same structured-output path as the Ollama agent — only the backend differs.
    The boto client is configured to retry throttling/5xx responses adaptively and
    to bound each network call, so transient Bedrock hiccups are absorbed here
    before the app-level retry in :func:`plan_fixes` ever sees them.
    """

    from botocore.config import Config
    from strands import Agent
    from strands.models import BedrockModel

    client_config = Config(
        retries={"max_attempts": 4, "mode": "adaptive"},
        connect_timeout=10,
        read_timeout=60,
    )
    model = BedrockModel(
        model_id=model_id,
        region_name=region,
        temperature=0,
        boto_client_config=client_config,
    )
    return Agent(model=model)


def check_bedrock(model_id: str = DEFAULT_BEDROCK_MODEL, region: str = DEFAULT_REGION) -> str:
    """Fail fast if AWS credentials or boto3 are missing. Returns the model id."""

    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise BedrockError("the 'boto3' package is not installed") from exc

    if boto3.Session(region_name=region).get_credentials() is None:
        raise BedrockError(
            "no AWS credentials found. Configure them (aws configure, env vars, or a role)."
        )
    return model_id


def make_agent(provider: str, *, model: str | None = None, host: str = DEFAULT_HOST,
               region: str = DEFAULT_REGION):
    """Build an agent for the chosen provider (model defaults per provider)."""

    if provider == "bedrock":
        return build_bedrock_agent(model or DEFAULT_BEDROCK_MODEL, region)
    if provider == "ollama":
        return build_agent(model or DEFAULT_MODEL, host)
    raise ProviderError(f"unknown provider '{provider}' (choose one of: {', '.join(PROVIDERS)})")


def preflight(provider: str, *, model: str | None = None, host: str = DEFAULT_HOST,
              region: str = DEFAULT_REGION) -> str:
    """Verify the provider is usable; return the concrete model id to build with."""

    if provider == "bedrock":
        return check_bedrock(model or DEFAULT_BEDROCK_MODEL, region)
    if provider == "ollama":
        return check_ollama(model or DEFAULT_MODEL, host)
    raise ProviderError(f"unknown provider '{provider}' (choose one of: {', '.join(PROVIDERS)})")


def check_ollama(model_id: str = DEFAULT_MODEL, host: str = DEFAULT_HOST) -> str:
    """Fail fast if Ollama isn't running or the model is missing.

    Returns the concrete available tag to use. Ollama does not auto-resolve a bare
    name (e.g. ``deepseek-coder-v2``) to an installed tag (e.g. ``:16b``) on the
    chat call, so we resolve it here and hand back the exact name.
    """

    try:
        import ollama
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise OllamaError("the 'ollama' package is not installed") from exc

    client = ollama.Client(host=host)
    try:
        available = [m.model for m in client.list().models]
    except Exception as exc:  # connection refused, etc.
        raise OllamaError(
            f"cannot reach Ollama at {host}. Start it with 'ollama serve'."
        ) from exc

    # Exact match wins; otherwise resolve a bare name to its first installed tag.
    if model_id in available:
        return model_id
    for name in available:
        if name and name.split(":")[0] == model_id:
            return name
    raise OllamaError(f"model '{model_id}' is not pulled. Run: ollama pull {model_id}")


def _build_prompt(transform_prompt: str, snippet: str, context: str) -> str:
    return (
        f"{transform_prompt}\n\n"
        "The matched code is a single Python call expression:\n"
        f"```python\n{snippet}\n```\n\n"
        "Here is the surrounding code for context only (do NOT rewrite it):\n"
        f"```python\n{context}\n```\n\n"
        "Respond with the corrected replacement for ONLY the matched call "
        "expression in `replacement_code` (valid Python, no surrounding "
        "statements, no code fences). Set `confident` true only if the fix is "
        "clearly correct and safe; otherwise false. Always give a short `reason`."
    )


def transform_match(
    agent, transform_prompt: str, snippet: str, context: str = ""
) -> TransformResult:
    """Ask the model for a structured transformation of one matched snippet."""

    prompt = _build_prompt(transform_prompt, snippet, context)
    return agent.structured_output(TransformResult, prompt)


# --------------------------------------------------------------------------- #
# Applying results
# --------------------------------------------------------------------------- #


@dataclass
class FixResult:
    """The outcome for a single match.

    ``status`` is one of:

    * ``"fixed"``   — a confident, parseable, non-no-op replacement was produced;
    * ``"skipped"`` — the model declined or its output was unusable (left alone);
    * ``"error"``   — the transform itself failed (timeout / provider exception),
      so this match is recorded and skipped without sinking the rest of the run.

    A ``"fixed"`` result is *proposed* until it survives the final-combination step
    in :func:`changed_sources`: if two rules target the same or nested spans, only
    one edit can land, and the loser is marked ``applied = False`` with a conflict
    note. ``applied`` is therefore the ground truth for "this edit is in the patch",
    while ``status == "fixed"`` means "the model produced a usable fix for it".
    """

    match: Match
    status: str  # "fixed" | "skipped" | "error"
    reason: str  # why safe (fixed), why left alone (skipped), or the failure (error)
    replacement_code: str | None = None
    diff: str | None = None  # unified diff of this fix in the final combined file
    applied: bool = True  # False when a conflict kept this fix out of the patch
    conflict: str | None = None  # explanation when applied is False


# Character budget for the per-match context handed to the model. Big enough for
# imports plus an enclosing function, small enough to keep prompts (and cost) bounded.
_CONTEXT_BUDGET = 2000


def _context_for(source: str, line: int, radius: int = 4) -> str:
    """Build the context shown to the model for a match at 1-based ``line``.

    Includes, within :data:`_CONTEXT_BUDGET` characters:

    * the file's ``import`` / ``from ... import`` lines (so the model knows which
      names are already available and under what alias), and
    * the enclosing ``def``/``class`` block, found by scanning upward for the
      nearest header indented less than the matched line, through to where that
      block's indentation ends — falling back to a ±``radius`` window when no
      enclosing block is found.

    This gives the model the signature and local names it needs to adapt a fix,
    instead of a blind ±4-line slice.
    """

    lines = source.splitlines()
    n = len(lines)
    idx = min(max(line - 1, 0), max(n - 1, 0))

    import_lines = [
        ln for ln in lines
        if ln.lstrip().startswith(("import ", "from "))
    ]

    def _indent(s: str) -> int:
        return len(s) - len(s.lstrip())

    # Find the enclosing def/class: nearest preceding header with smaller indent.
    target_indent = _indent(lines[idx]) if idx < n else 0
    start = max(0, idx - radius)
    header: int | None = None
    for i in range(idx, -1, -1):
        stripped = lines[i].lstrip()
        is_header = stripped.startswith(("def ", "class ", "async def "))
        if is_header and _indent(lines[i]) < target_indent:
            header = i
            break
    if header is not None:
        start = header
        header_indent = _indent(lines[header])
        end = idx + 1
        # Extend forward to the end of the enclosing block.
        for j in range(idx + 1, n):
            if lines[j].strip() and _indent(lines[j]) <= header_indent:
                break
            end = j + 1
    else:
        end = min(n, idx + radius + 1)

    block = lines[start:end]
    pieces: list[str] = []
    if import_lines:
        pieces.append("\n".join(import_lines))
        pieces.append("")  # blank separator
    pieces.append("\n".join(block))
    context = "\n".join(pieces)

    if len(context) > _CONTEXT_BUDGET:
        # Keep the window around the match; trim from the block, not the imports.
        header_part = ("\n".join(import_lines) + "\n\n") if import_lines else ""
        room = max(0, _CONTEXT_BUDGET - len(header_part))
        context = header_part + "\n".join(block)[:room]
    return context


Span = tuple[int, int, int, int]  # (start_line, start_col, end_line, end_col)


class _FixApplier(cst.CSTTransformer):
    """Replace call nodes at specific full spans with parsed replacements.

    Keying on the full span (not just the start) is what keeps a nested inner call
    like ``hashlib.md5(x)`` distinct from the outer ``.hexdigest()`` call that
    shares its start position — so we replace exactly the matched node.
    """

    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(self, replacements: dict[Span, cst.BaseExpression]) -> None:
        self.replacements = replacements

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call):
        pos = self.get_metadata(PositionProvider, original_node)
        key = (pos.start.line, pos.start.column, pos.end.line, pos.end.column)
        if key in self.replacements:
            return self.replacements[key]
        return updated_node


def apply_to_source(source: str, replacements: dict[Span, cst.BaseExpression]) -> str:
    """Return ``source`` with the given call spans replaced. Formatting preserved."""

    wrapper = MetadataWrapper(cst.parse_module(source))
    return wrapper.visit(_FixApplier(replacements)).code


_REDUNDANT_TRAILING = {"hexdigest", "digest"}


def _strip_redundant_trailing(expr: cst.BaseExpression) -> cst.BaseExpression:
    """Drop a trailing ``.hexdigest()``/``.digest()`` the model may have added.

    The matched span is always the bare call (e.g. ``hashlib.md5(x)``) — any
    ``.hexdigest()`` lives *outside* it and is preserved by the splice. If the
    model appends its own, we'd get ``sha256(x).hexdigest().hexdigest()``; unwrap
    it so the replacement mirrors the exact node it replaces.
    """

    while (
        isinstance(expr, cst.Call)
        and not expr.args
        and isinstance(expr.func, cst.Attribute)
        and expr.func.attr.value in _REDUNDANT_TRAILING
    ):
        expr = expr.func.value
    return expr


def _unified_diff(old: str, new: str, filename: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
        )
    )


def _transform_with_retry(
    agent, m: Match, context: str, max_retries: int, deadline: float
) -> TransformResult:
    """Call the model for one match, retrying transient failures until a deadline.

    Runs inside a worker thread. ``deadline`` is an absolute ``time.monotonic()``
    instant; the whole budget (every attempt plus the backoff between them) must
    fit inside it. Before each attempt we check the clock and raise
    :class:`TransformTimeout` if the budget is spent, so a stalled or repeatedly-
    throttled match stops burning time instead of hanging the run. Only *transient*
    exceptions are retried; a permanent error is re-raised immediately so it fails
    fast. The last exception is re-raised if every attempt fails.
    """

    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        if time.monotonic() >= deadline:
            raise TransformTimeout("deadline exceeded before model call")
        try:
            return transform_match(agent, m.rule.transform_prompt, m.snippet, context)
        except Exception as exc:  # provider-agnostic; classified by transience + budget
            last_exc = exc
            if attempt >= max_retries or not _is_transient(exc):
                break
            backoff = _RETRY_BACKOFF * (2**attempt)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(backoff, remaining))
    assert last_exc is not None  # loop ran at least once
    raise last_exc


def _finalize(tr: TransformResult, m: Match, source: str, root: Path) -> FixResult:
    """Turn a model result into a fixed/skipped :class:`FixResult` (no I/O).

    The per-fix diff is a *provisional* preview of this edit alone; the diff shown
    to the user is regenerated from the final combined file in
    :func:`changed_sources` so it reflects every fix and any added imports.
    """

    if not tr.confident:
        return FixResult(m, "skipped", tr.reason or "model not confident")

    try:
        expr = cst.parse_expression(tr.replacement_code)
    except cst.ParserSyntaxError as exc:
        return FixResult(m, "skipped", f"model output invalid Python: {exc}")

    expr = _strip_redundant_trailing(expr)
    normalized = cst.Module(body=[]).code_for_node(expr)

    # Reject a no-op: if the "fix" is byte-for-byte the matched call, it changes
    # nothing and must not be counted as a fix (and would make an empty diff).
    if _normalize_expr_source(m.snippet) == normalized:
        return FixResult(m, "skipped", "model returned the original code unchanged (no-op)")

    rel = m.file_path.relative_to(root).as_posix()
    new_source = apply_to_source(source, {m.span: expr})
    return FixResult(
        m,
        "fixed",
        tr.reason,
        replacement_code=normalized,
        diff=_unified_diff(source, new_source, rel),
    )


def _normalize_expr_source(code: str) -> str:
    """Normalize an expression's source for no-op comparison, or '' if unparseable."""
    try:
        expr = cst.parse_expression(code)
    except cst.ParserSyntaxError:
        return ""
    return cst.Module(body=[]).code_for_node(_strip_redundant_trailing(expr))


def plan_fixes(
    agent,
    matches: list[Match],
    root: Path,
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    on_result: Callable[[FixResult], None] | None = None,
) -> list[FixResult]:
    """Ask the model about every match and decide fixed / skipped / error.

    Confident replacements that parse as a Python expression are recorded as
    ``fixed`` (with an isolated per-fix diff); low-confidence, ambiguous, or
    non-parsing output is ``skipped`` with a reason; and a transform that fails
    outright (exceeds its deadline or raises even after retries) is recorded as
    ``error`` so one bad match never sinks the rest of the batch. Nothing is
    written to disk here; that is the verifier's job.

    Matches are transformed concurrently (bounded by ``concurrency``). Each match
    has an absolute ``timeout`` deadline covering its retries, enforced inside the
    worker; a match still pending after its deadline is recorded as a timeout
    without blocking the rest. Completed matches are processed (and ``on_result``
    is fired) **as they finish**, so a slow early request never stalls progress
    reporting for ones that already came back. The returned list is always in
    match order regardless of completion order.
    """

    if not matches:
        return []

    source_cache: dict[Path, str] = {}
    prepared: list[tuple[Match, str, str]] = []
    for m in matches:
        source = source_cache.setdefault(m.file_path, m.file_path.read_text(encoding="utf-8"))
        prepared.append((m, source, _context_for(source, m.line)))

    results: list[FixResult | None] = [None] * len(prepared)
    workers = max(1, min(concurrency, len(prepared)))
    deadline = time.monotonic() + timeout

    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        future_to_index = {
            pool.submit(_transform_with_retry, agent, m, ctx, max_retries, deadline): i
            for i, (m, _src, ctx) in enumerate(prepared)
        }
        pending = set(future_to_index)
        while pending:
            now = time.monotonic()
            wait_for = max(0.0, deadline - now)
            done, pending = wait(pending, timeout=wait_for, return_when=FIRST_COMPLETED)

            for fut in done:
                i = future_to_index[fut]
                m, source, _ctx = prepared[i]
                try:
                    tr = fut.result()
                    result = _finalize(tr, m, source, root)
                except TransformTimeout:
                    result = FixResult(m, "error", f"transform timed out after {timeout:g}s")
                except Exception as exc:  # isolate a provider failure to just this match
                    result = FixResult(m, "error", f"transform failed: {exc}")
                results[i] = result
                if on_result is not None:
                    on_result(result)

            # Nothing finished within the budget: the remaining matches are over
            # their deadline. Record them as timeouts and stop waiting on workers
            # that may be wedged in a provider call.
            if not done and pending and time.monotonic() >= deadline:
                for fut in pending:
                    i = future_to_index[fut]
                    m, _source, _ctx = prepared[i]
                    result = FixResult(m, "error", f"transform timed out after {timeout:g}s")
                    results[i] = result
                    if on_result is not None:
                        on_result(result)
                break
    finally:
        # Don't block process teardown on a worker wedged in a stalled model call.
        pool.shutdown(wait=False, cancel_futures=True)

    return [r for r in results if r is not None]


def _parse_import(spec: str) -> tuple[str, str | None]:
    """Split a validated import spec into ``(module, obj_or_None)``.

    ``"import secrets"``            -> ("secrets", None)
    ``"from hashlib import sha256"`` -> ("hashlib", "sha256")
    """

    words = spec.split()
    if words[0] == "import":
        return words[1], None
    return words[1], words[3]  # from X import Y (format guaranteed by the loader)


def _add_imports(source: str, specs: set[str]) -> str:
    """Ensure each import spec is present in ``source``, adding it iff missing."""

    from libcst.codemod import CodemodContext
    from libcst.codemod.visitors import AddImportsVisitor

    context = CodemodContext()
    for spec in sorted(specs):  # sorted for deterministic output
        module, obj = _parse_import(spec)
        AddImportsVisitor.add_needed_import(context, module, obj)
    tree = cst.parse_module(source)
    return AddImportsVisitor(context).transform_module(tree).code


def _span_contains(outer: Span, inner: Span) -> bool:
    """True if ``inner`` lies within ``outer`` (by (line, col) start/end bounds)."""
    o_start, o_end = (outer[0], outer[1]), (outer[2], outer[3])
    i_start, i_end = (inner[0], inner[1]), (inner[2], inner[3])
    return o_start <= i_start and i_end <= o_end


def _spans_conflict(a: Span, b: Span) -> bool:
    """Two edits conflict if they target the same span or one nests in the other.

    Splicing one would change or discard the text the other edit was computed
    against, so at most one of a conflicting pair can be safely applied.
    """
    return a == b or _span_contains(a, b) or _span_contains(b, a)


def _resolve_conflicts(frs: list[FixResult]) -> tuple[list[FixResult], list[FixResult]]:
    """Split a file's fixes into (to-apply, rejected) by span conflict.

    Deterministic order: fixes earliest in the file win, and for an exact-span tie
    the first in list order wins. A rejected fix keeps ``status == 'fixed'`` (the
    model did produce a usable fix) but is marked ``applied = False`` so the report
    can say it was dropped and why, instead of silently losing it.
    """

    ordered = sorted(frs, key=lambda r: r.match.span)
    applied: list[FixResult] = []
    rejected: list[FixResult] = []
    for r in ordered:
        clash = next((a for a in applied if _spans_conflict(a.match.span, r.match.span)), None)
        if clash is None:
            applied.append(r)
        else:
            r.applied = False
            r.conflict = (
                f"overlaps another fix at {clash.match.rule.id} "
                f"(line {clash.match.line}); applied that one instead"
            )
            rejected.append(r)
    return applied, rejected


def changed_sources(results: list[FixResult]) -> dict[Path, str]:
    """Combine all confident fixes per file into the final rewritten source.

    Applies every non-conflicting ``fixed`` replacement for a file at once, then
    ensures the imports required by the applied rules exist (added only if
    missing), so the on-disk result (written later by the verifier) reflects all
    fixes together and stays runnable — e.g. a ``random`` -> ``secrets`` fix also
    gets ``import secrets``.

    Two edits that target the same span or nest one inside the other cannot both
    land; :func:`_resolve_conflicts` keeps one and marks the other
    ``applied = False`` (mutating the passed-in :class:`FixResult`), so a conflict
    is reported rather than silently dropped. Each applied fix's ``diff`` is
    regenerated from the final combined file, so the preview the user reviews
    matches exactly what will be committed, imports included.
    """

    fixed_by_file: dict[Path, list[FixResult]] = {}
    for r in results:
        if r.status == "fixed":
            fixed_by_file.setdefault(r.match.file_path, []).append(r)

    out: dict[Path, str] = {}
    for path, frs in fixed_by_file.items():
        applied, _rejected = _resolve_conflicts(frs)
        if not applied:
            continue

        source = path.read_text(encoding="utf-8")
        replacements: dict[Span, cst.BaseExpression] = {}
        for r in applied:
            # A "fixed" result always carries its replacement code.
            assert r.replacement_code is not None
            replacements[r.match.span] = cst.parse_expression(r.replacement_code)
        new_source = apply_to_source(source, replacements)

        imports = {imp for r in applied for imp in r.match.rule.ensure_imports}
        if imports:
            new_source = _add_imports(new_source, imports)
        out[path] = new_source

        # Regenerate each applied fix's diff from the final combined file so the
        # preview reflects every sibling fix and any imports that were added.
        combined_diff = _unified_diff(source, new_source, path.name)
        for r in applied:
            r.diff = combined_diff
    return out


# --------------------------------------------------------------------------- #
# Cost / usage estimate
# --------------------------------------------------------------------------- #

# strands' structured_output path returns only the parsed object, not provider
# token counts, so we can't read exact usage. We estimate it from the text we sent
# and got back with the common ~4-chars-per-token rule of thumb; the estimate is
# always labeled approximate so it is never mistaken for a billed figure.
_CHARS_PER_TOKEN = 4


@dataclass
class UsageEstimate:
    """A rough, clearly-approximate accounting of one run's model traffic."""

    calls: int
    input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def _tokens_from_chars(chars: int) -> int:
    return -(-chars // _CHARS_PER_TOKEN)  # ceil div; any non-empty text costs >0


def estimate_usage(matches: list[Match], results: list[FixResult]) -> UsageEstimate:
    """Estimate the tokens a run sent/received, from the prompt and model text.

    Reconstructs each prompt exactly as :func:`plan_fixes` built it, plus the
    text the model returned (replacement + reason), so the figure tracks real
    traffic without needing provider metrics. Approximate by construction.
    """

    source_cache: dict[Path, str] = {}
    input_chars = 0
    output_chars = 0
    for m, r in zip(matches, results, strict=False):
        source = source_cache.setdefault(m.file_path, m.file_path.read_text(encoding="utf-8"))
        context = _context_for(source, m.line)
        input_chars += len(_build_prompt(m.rule.transform_prompt, m.snippet, context))
        output_chars += len(r.replacement_code or "") + len(r.reason or "")

    return UsageEstimate(
        calls=len(results),
        input_tokens=_tokens_from_chars(input_chars),
        output_tokens=_tokens_from_chars(output_chars),
    )


# Approximate public list prices, USD per 1M tokens, as (input, output). A rough
# ballpark for one run's cost — never a billing source of truth. Providers with no
# entry (e.g. Ollama, which runs locally) have no API cost.
_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "bedrock": (3.0, 15.0),  # Claude Sonnet 4.5 list-price tier
}


def estimated_cost_usd(usage: UsageEstimate, provider: str) -> float | None:
    """Approximate USD cost for a run, or ``None`` when the provider is free/local."""

    price = _PRICES_PER_MTOK.get(provider)
    if price is None:
        return None
    in_rate, out_rate = price
    return (usage.input_tokens * in_rate + usage.output_tokens * out_rate) / 1_000_000
