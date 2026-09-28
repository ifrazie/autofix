"""Command-line entry point.

    autofix <target> --rule md5.yaml --rule shell.yaml

Orchestrates the pipeline: resolve target -> load rules -> find matches -> ask the
model to transform each (fixing confident matches, skipping ambiguous ones) ->
verify on a fresh branch with pytest -> report.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import typer

from . import reporter
from .matcher import find_matches
from .rules import RuleError, load_rules
from .target import TargetError, resolve_target, python_files
from .transformer import (
    DEFAULT_HOST,
    DEFAULT_PROVIDER,
    DEFAULT_REGION,
    PROVIDERS,
    ProviderError,
    changed_sources,
    default_model_for,
    make_agent,
    plan_fixes,
    preflight,
)
from .verifier import VerifierError, verify

app = typer.Typer(
    add_completion=False,
    help="Rule-driven vulnerability remediation: find and fix a pattern across a Python codebase.",
)


def _fail(msg: str) -> None:
    typer.secho(f"error: {msg}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


@app.command()
def main(
    target: str = typer.Argument(..., help="Local path or GitHub URL of the repository."),
    rule: List[Path] = typer.Option(..., "--rule", "-r", help="Rule YAML file (repeatable)."),
    provider: str = typer.Option(
        DEFAULT_PROVIDER, "--provider", help=f"Model backend: {' | '.join(PROVIDERS)}."
    ),
    model: str = typer.Option(
        None, "--model", help="Model id (defaults to the provider's default model)."
    ),
    region: str = typer.Option(DEFAULT_REGION, "--region", help="AWS region (bedrock only)."),
    host: str = typer.Option(DEFAULT_HOST, "--host", help="Ollama host URL (ollama only)."),
) -> None:
    """Find and fix every instance of each rule's pattern in the target."""

    if provider not in PROVIDERS:
        _fail(f"unknown provider '{provider}' (choose one of: {', '.join(PROVIDERS)})")

    try:
        resolved = resolve_target(target)
        rules = load_rules([str(p) for p in rule])
    except (TargetError, RuleError) as exc:
        _fail(str(exc))

    files = python_files(resolved.path)
    reporter.scanning(resolved.path, len(files), len(rules))

    matches = []
    for r in rules:
        matches.extend(find_matches(r, files))

    if not matches:
        reporter.no_matches()
        raise typer.Exit(code=0)

    model_id = model or default_model_for(provider)
    reporter.transforming(provider, model_id, len(matches), len(rules))

    try:
        resolved_model = preflight(provider, model=model_id, host=host, region=region)
    except ProviderError as exc:
        _fail(str(exc))
    agent = make_agent(provider, model=resolved_model, host=host, region=region)

    results = plan_fixes(agent, matches, resolved.path)

    fixed = [r for r in results if r.status == "fixed"]
    skipped = [r for r in results if r.status == "skipped"]

    reporter.fixes(results, resolved.path)
    reporter.summary(len(matches), len(fixed), len(skipped))

    if not fixed:
        reporter.no_fixes()
        return

    try:
        vr = verify(resolved.path, changed_sources(results), resolved.path)
    except VerifierError as exc:
        _fail(str(exc))

    reporter.verification(vr)


if __name__ == "__main__":
    app()
