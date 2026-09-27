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
    DEFAULT_MODEL,
    OllamaError,
    build_agent,
    check_ollama,
    plan_fixes,
)

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
    model: str = typer.Option(DEFAULT_MODEL, "--model", help="Ollama model id."),
    host: str = typer.Option(DEFAULT_HOST, "--host", help="Ollama host URL."),
) -> None:
    """Find and fix every instance of each rule's pattern in the target."""

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

    typer.echo(f"Found {len(matches)} match(es). Asking the model to transform each...\n")

    try:
        resolved_model = check_ollama(model, host)
    except OllamaError as exc:
        _fail(str(exc))
    agent = build_agent(resolved_model, host)

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


if __name__ == "__main__":
    app()
