"""Command-line entry point.

Parses arguments and orchestrates the pipeline:

    autofix <target> --rule md5.yaml --rule shell.yaml

Slice 1 wires the deterministic front of the pipeline: resolve the target, load
and validate the rules, and find + list every matching instance. Later slices add
the transformer, verifier, and full report.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import typer

from .matcher import find_matches
from .rules import RuleError, load_rules
from .target import TargetError, resolve_target, python_files

app = typer.Typer(
    add_completion=False,
    help="Rule-driven vulnerability remediation: find and fix a pattern across a Python codebase.",
)


@app.command()
def main(
    target: str = typer.Argument(
        ..., help="Local path or GitHub URL of the repository to scan."
    ),
    rule: List[Path] = typer.Option(
        ..., "--rule", "-r", help="Path to a rule YAML file (repeatable)."
    ),
) -> None:
    """Find every instance of each rule's pattern in the target repository."""

    try:
        resolved = resolve_target(target)
    except TargetError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    try:
        rules = load_rules([str(p) for p in rule])
    except RuleError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    files = python_files(resolved.path)
    typer.echo(
        f"Scanning {resolved.path} - {len(files)} Python file(s), {len(rules)} rule(s).\n"
    )

    total = 0
    for r in rules:
        matches = find_matches(r, files)
        total += len(matches)
        typer.secho(f"Rule '{r.id}': {len(matches)} match(es)", bold=True)
        typer.echo(f"  why: {r.rationale}")
        for m in matches:
            rel = m.file_path.relative_to(resolved.path)
            typer.echo(f"  {rel}:{m.line}:{m.column}  {m.snippet}")
        typer.echo("")

    if total == 0:
        typer.secho("Nothing matched.", fg=typer.colors.YELLOW)


if __name__ == "__main__":
    app()
