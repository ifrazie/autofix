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
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
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

    * ``"fixed"``   — a confident, parseable replacement was produced;
    * ``"skipped"`` — the model declined or its output was unusable (left alone);
    * ``"error"``   — the transform itself failed (timeout / provider exception),
      so this match is recorded and skipped without sinking the rest of the run.
    """

    match: Match
    status: str  # "fixed" | "skipped" | "error"
    reason: str  # why safe (fixed), why left alone (skipped), or the failure (error)
    replacement_code: str | None = None
    diff: str | None = None  # unified diff of just this fix (fixed only)


def _context_for(source: str, line: int, radius: int = 4) -> str:
    lines = source.splitlines()
    start = max(0, line - 1 - radius)
    end = min(len(lines), line - 1 + radius + 1)
    return "\n".join(lines[start:end])


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
    agent, m: Match, context: str, max_retries: int
) -> TransformResult:
    """Call the model for one match, retrying transient failures with backoff.

    Runs inside a worker thread. Any exception (throttling, network, provider
    error) is retried up to ``max_retries`` times with exponential backoff; the
    last exception is re-raised if every attempt fails, and the caller turns that
    into an ``error`` result rather than letting it abort the batch.
    """

    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return transform_match(agent, m.rule.transform_prompt, m.snippet, context)
        except Exception as exc:  # provider-agnostic; classified only by retry budget
            last_exc = exc
            if attempt < max_retries:
                time.sleep(_RETRY_BACKOFF * (2**attempt))
    assert last_exc is not None  # loop ran at least once
    raise last_exc


def _finalize(tr: TransformResult, m: Match, source: str, root: Path) -> FixResult:
    """Turn a model result into a fixed/skipped :class:`FixResult` (no I/O)."""

    if not tr.confident:
        return FixResult(m, "skipped", tr.reason or "model not confident")

    try:
        expr = cst.parse_expression(tr.replacement_code)
    except cst.ParserSyntaxError as exc:
        return FixResult(m, "skipped", f"model output invalid Python: {exc}")

    expr = _strip_redundant_trailing(expr)
    normalized = cst.Module(body=[]).code_for_node(expr)

    rel = m.file_path.relative_to(root).as_posix()
    new_source = apply_to_source(source, {m.span: expr})
    return FixResult(
        m,
        "fixed",
        tr.reason,
        replacement_code=normalized,
        diff=_unified_diff(source, new_source, rel),
    )


def plan_fixes(
    agent,
    matches: list[Match],
    root: Path,
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> list[FixResult]:
    """Ask the model about every match and decide fixed / skipped / error.

    Confident replacements that parse as a Python expression are recorded as
    ``fixed`` (with an isolated per-fix diff); low-confidence, ambiguous, or
    non-parsing output is ``skipped`` with a reason; and a transform that fails
    outright (times out or raises even after retries) is recorded as ``error`` so
    one bad match never sinks the rest of the batch. Nothing is written to disk
    here; that is the verifier's job.

    Matches are transformed concurrently (bounded by ``concurrency``); each match
    has an overall ``timeout`` budget covering its retries. Results are returned in
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
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_transform_with_retry, agent, m, ctx, max_retries): i
            for i, (m, _src, ctx) in enumerate(prepared)
        }
        for fut, i in futures.items():
            m, source, _ctx = prepared[i]
            try:
                tr = fut.result(timeout=timeout)
            except FutureTimeoutError:
                results[i] = FixResult(m, "error", f"transform timed out after {timeout:g}s")
                continue
            except Exception as exc:  # isolate a provider failure to just this match
                results[i] = FixResult(m, "error", f"transform failed: {exc}")
                continue
            results[i] = _finalize(tr, m, source, root)

    return [r for r in results if r is not None]


def changed_sources(results: list[FixResult]) -> dict[Path, str]:
    """Combine all confident fixes per file into the final rewritten source.

    Applies every ``fixed`` replacement for a file at once, so the on-disk result
    (written later by the verifier) reflects all fixes together.
    """

    fixed_by_file: dict[Path, list[FixResult]] = {}
    for r in results:
        if r.status == "fixed":
            fixed_by_file.setdefault(r.match.file_path, []).append(r)

    out: dict[Path, str] = {}
    for path, frs in fixed_by_file.items():
        source = path.read_text(encoding="utf-8")
        replacements: dict[Span, cst.BaseExpression] = {}
        for r in frs:
            # A "fixed" result always carries its replacement code.
            assert r.replacement_code is not None
            replacements[r.match.span] = cst.parse_expression(r.replacement_code)
        out[path] = apply_to_source(source, replacements)
    return out
