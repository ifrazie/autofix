"""AI transformer.

For each match, hand the local model (Ollama, via the strands-agents SDK) the
matched snippet plus surrounding context and the rule's transform prompt, and ask
for a *structured* result: the replacement code, a confidence flag, and a reason.
Our code — not the model — decides what to do with it (splice or skip; see
matcher/verifier). Keeping the model on a short leash (one snippet in, one
structured object out) is what keeps the result trustworthy.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

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


def check_ollama(model_id: str = DEFAULT_MODEL, host: str = DEFAULT_HOST) -> None:
    """Fail fast with a plain message if Ollama isn't running or the model is missing."""

    try:
        import ollama
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise OllamaError("the 'ollama' package is not installed") from exc

    client = ollama.Client(host=host)
    try:
        available = {m.model for m in client.list().models}
    except Exception as exc:  # connection refused, etc.
        raise OllamaError(
            f"cannot reach Ollama at {host}. Start it with 'ollama serve'."
        ) from exc

    # Ollama reports names like 'deepseek-coder-v2:latest'; match on the base name.
    if not any(name == model_id or name.split(":")[0] == model_id for name in available):
        raise OllamaError(
            f"model '{model_id}' is not pulled. Run: ollama pull {model_id}"
        )


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
