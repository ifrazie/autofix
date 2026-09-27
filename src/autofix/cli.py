"""Command-line entry point.

    autofix <target> --rule md5.yaml --rule shell.yaml

Orchestrates the pipeline: resolve target -> load rules -> find matches ->
ask the model to transform each (fixing confident matches, skipping ambiguous
ones) -> report. Slice 4 adds the branch/commit/pytest verifier; Slice 5 adds
the polished report.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import typer

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
    typer.echo(f"Scanning {resolved.path} - {len(files)} Python file(s), {len(rules)} rule(s).")

    matches = []
    for r in rules:
        matches.extend(find_matches(r, files))

    if not matches:
        typer.secho("\nNothing matched.", fg=typer.colors.YELLOW)
        raise typer.Exit(code=0)

    model_id = model or default_model_for(provider)
    typer.echo(
        f"Found {len(matches)} match(es). Asking {provider}:{model_id} to transform each...\n"
    )

    try:
        resolved_model = preflight(provider, model=model_id, host=host, region=region)
    except ProviderError as exc:
        _fail(str(exc))
    agent = make_agent(provider, model=resolved_model, host=host, region=region)

    results = plan_fixes(agent, matches, resolved.path)

    fixed = [r for r in results if r.status == "fixed"]
    skipped = [r for r in results if r.status == "skipped"]

    for r in results:
        rel = r.match.file_path.relative_to(resolved.path).as_posix()
        loc = f"{rel}:{r.match.line}"
        if r.status == "fixed":
            typer.secho(f"[FIXED] {loc}  ({r.match.rule.id})", fg=typer.colors.GREEN, bold=True)
            typer.echo(f"    why: {r.reason}")
            if r.diff:
                typer.echo(r.diff.rstrip("\n"))
        else:
            typer.secho(f"[SKIP]  {loc}  ({r.match.rule.id})", fg=typer.colors.YELLOW, bold=True)
            typer.echo(f"    left alone: {r.reason}")
        typer.echo("")

    typer.secho(
        f"Summary: {len(matches)} matched, {len(fixed)} fixed, {len(skipped)} skipped.",
        bold=True,
    )

    if not fixed:
        typer.echo("\nNo confident fixes to verify.")
        return

    typer.echo("\nVerifying on a fresh branch...")
    try:
        vr = verify(resolved.path, changed_sources(results), resolved.path)
    except VerifierError as exc:
        _fail(str(exc))

    where = f"{vr.workspace} (isolated copy)" if vr.isolated else str(vr.workspace)
    typer.echo(f"Branch {vr.branch} - {len(vr.committed)} file(s) committed in {where}")

    if vr.passed:
        typer.secho("Tests: PASS", fg=typer.colors.GREEN, bold=True)
    else:
        typer.secho("Tests: FAIL", fg=typer.colors.RED, bold=True)
        if vr.culprits:
            typer.secho(
                f"Likely culprit(s): {', '.join(vr.culprits)}",
                fg=typer.colors.RED,
                bold=True,
            )
        typer.echo("Branch left intact for inspection.")


if __name__ == "__main__":
    app()
