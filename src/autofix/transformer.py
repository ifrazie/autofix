"""AI transformer.

For each match, hand the local model (Ollama, via the strands-agents SDK) the
matched snippet plus surrounding context and the rule's transform prompt, and ask
for a *structured* result: the replacement code, a confidence flag, and a reason.
Our code — not the model — decides what to do with it (splice or skip; see
matcher/verifier). Keeping the model on a short leash (one snippet in, one
structured object out) is what keeps the result trustworthy.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path

import libcst as cst
from libcst.metadata import MetadataWrapper, PositionProvider
from pydantic import BaseModel, Field

from .matcher import Match

DEFAULT_MODEL = "deepseek-coder-v2"
DEFAULT_HOST = "http://localhost:11434"


class TransformResult(BaseModel):
    """The structured output we require from the model for one match."""

    replacement_code: str = Field(
        description="The corrected replacement for ONLY the matched call expression, as valid Python."
    )
    confident: bool = Field(
        description="True only if the fix is correct and safe to apply; False to skip."
    )
    reason: str = Field(
        description="Why the fix is safe, or — when not confident — why the match should be left alone."
    )


class OllamaError(RuntimeError):
    """Raised when Ollama is unreachable or the model is unavailable."""


def build_agent(model_id: str = DEFAULT_MODEL, host: str = DEFAULT_HOST):
    """Create a strands Agent backed by a local Ollama model.

    Imports are local so the deterministic parts of the tool (matcher, rules)
    stay importable even if the AI stack isn't installed.
    """

    from strands import Agent
    from strands.models.ollama import OllamaModel

    model = OllamaModel(host=host, model_id=model_id, temperature=0)
    return Agent(model=model)


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
        if name.split(":")[0] == model_id:
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


def transform_match(agent, transform_prompt: str, snippet: str, context: str = "") -> TransformResult:
    """Ask the model for a structured transformation of one matched snippet."""

    prompt = _build_prompt(transform_prompt, snippet, context)
    return agent.structured_output(TransformResult, prompt)


# --------------------------------------------------------------------------- #
# Applying results
# --------------------------------------------------------------------------- #


@dataclass
class FixResult:
    """The outcome for a single match: applied, or left alone with a reason."""

    match: Match
    status: str  # "fixed" | "skipped"
    reason: str  # why safe (fixed) or why left alone (skipped)
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


def plan_fixes(agent, matches: list[Match], root: Path) -> list[FixResult]:
    """Ask the model about every match and decide fixed vs. skipped.

    Confident replacements that parse as a Python expression are recorded as
    ``fixed`` (with an isolated per-fix diff); everything else — low confidence,
    ambiguous, or non-parsing model output — is recorded as ``skipped`` with a
    reason. Nothing is written to disk here; that is the verifier's job.
    """

    results: list[FixResult] = []
    source_cache: dict[Path, str] = {}

    for m in matches:
        source = source_cache.setdefault(m.file_path, m.file_path.read_text(encoding="utf-8"))
        context = _context_for(source, m.line)
        tr = transform_match(agent, m.rule.transform_prompt, m.snippet, context)

        if not tr.confident:
            results.append(FixResult(m, "skipped", tr.reason or "model not confident"))
            continue

        try:
            expr = cst.parse_expression(tr.replacement_code)
        except cst.ParserSyntaxError as exc:
            results.append(FixResult(m, "skipped", f"model output invalid Python: {exc}"))
            continue

        expr = _strip_redundant_trailing(expr)
        normalized = cst.Module(body=[]).code_for_node(expr)

        rel = m.file_path.relative_to(root).as_posix()
        new_source = apply_to_source(source, {m.span: expr})
        results.append(
            FixResult(
                m,
                "fixed",
                tr.reason,
                replacement_code=normalized,
                diff=_unified_diff(source, new_source, rel),
            )
        )

    return results


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
        replacements = {
            r.match.span: cst.parse_expression(r.replacement_code) for r in frs
        }
        out[path] = apply_to_source(source, replacements)
    return out
