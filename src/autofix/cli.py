"""Command-line entry point.

    autofix fix <target> --rule md5.yaml --rule shell.yaml
    autofix fix <target> --rules-dir rules/
    autofix rules validate --rules-dir rules/

The ``fix`` command orchestrates the pipeline: resolve target -> load rules ->
find matches -> ask the model to transform each (fixing confident matches,
skipping ambiguous ones) -> verify on a fresh branch with pytest -> report. The
``rules`` command group inspects rule files without touching any code.
"""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import typer

from . import reporter
from .matcher import find_matches
from .rules import RuleError, gather_rule_paths, load_rule, load_rules
from .target import TargetError, python_files, resolve_target
from .transformer import (
    DEFAULT_CONCURRENCY,
    DEFAULT_HOST,
    DEFAULT_MAX_RETRIES,
    DEFAULT_PROVIDER,
    DEFAULT_REGION,
    DEFAULT_TIMEOUT,
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
rules_app = typer.Typer(add_completion=False, help="Inspect and validate rule files.")
app.add_typer(rules_app, name="rules")


def _fail(msg: str) -> NoReturn:
    typer.secho(f"error: {msg}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _version_callback(value: bool) -> None:
    if value:
        from importlib.metadata import version

        typer.echo(f"autofix {version('autofix')}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the autofix version and exit.",
    ),
) -> None:
    """Rule-driven vulnerability remediation for Python codebases."""


@app.command()
def fix(
    target: str = typer.Argument(..., help="Local path or GitHub URL of the repository."),
    rule: list[Path] = typer.Option([], "--rule", "-r", help="Rule YAML file (repeatable)."),
    rules_dir: str = typer.Option(
        None, "--rules-dir", help="Directory of *.yaml rule files to load (in addition to --rule)."
    ),
    provider: str = typer.Option(
        DEFAULT_PROVIDER, "--provider", help=f"Model backend: {' | '.join(PROVIDERS)}."
    ),
    model: str = typer.Option(
        None, "--model", help="Model id (defaults to the provider's default model)."
    ),
    region: str = typer.Option(DEFAULT_REGION, "--region", help="AWS region (bedrock only)."),
    host: str = typer.Option(DEFAULT_HOST, "--host", help="Ollama host URL (ollama only)."),
    concurrency: int = typer.Option(
        DEFAULT_CONCURRENCY, "--concurrency", min=1,
        help="Number of matches to transform in parallel.",
    ),
    timeout: float = typer.Option(
        DEFAULT_TIMEOUT, "--timeout", min=1,
        help="Overall per-match budget in seconds (includes retries).",
    ),
    max_retries: int = typer.Option(
        DEFAULT_MAX_RETRIES, "--max-retries", min=0,
        help="Extra attempts after the first for transient transform failures.",
    ),
) -> None:
    """Find and fix every instance of each rule's pattern in the target."""

    if provider not in PROVIDERS:
        _fail(f"unknown provider '{provider}' (choose one of: {', '.join(PROVIDERS)})")

    try:
        resolved = resolve_target(target)
        rule_paths = gather_rule_paths([str(p) for p in rule], rules_dir)
        rules = load_rules(rule_paths)
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

    results = plan_fixes(
        agent,
        matches,
        resolved.path,
        concurrency=concurrency,
        timeout=timeout,
        max_retries=max_retries,
    )

    fixed = [r for r in results if r.status == "fixed"]
    skipped = [r for r in results if r.status == "skipped"]
    errored = [r for r in results if r.status == "error"]

    reporter.fixes(results, resolved.path)
    reporter.summary(len(matches), len(fixed), len(skipped), len(errored))

    if not fixed:
        reporter.no_fixes()
        return

    try:
        vr = verify(resolved.path, changed_sources(results), resolved.path)
    except VerifierError as exc:
        _fail(str(exc))

    reporter.verification(vr)


@rules_app.command("validate")
def rules_validate(
    rule: list[Path] = typer.Option([], "--rule", "-r", help="Rule YAML file (repeatable)."),
    rules_dir: str = typer.Option(
        None, "--rules-dir", help="Directory of *.yaml rule files to validate."
    ),
) -> None:
    """Load and schema-check rule files without running a fix."""

    try:
        rule_paths = gather_rule_paths([str(p) for p in rule], rules_dir)
    except RuleError as exc:
        _fail(str(exc))

    ok = []
    bad = []
    for path in rule_paths:
        try:
            ok.append((path, load_rule(path)))
        except RuleError as exc:
            bad.append((path, str(exc)))

    reporter.rules_validation(ok, bad)
    if bad:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
