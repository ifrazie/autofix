"""Command-line entry point.

    autofix fix <target> --rule md5.yaml --rule shell.yaml
    autofix fix <target> --rules-dir rules/ --dry-run
    autofix rules list --rules-dir rules/
    autofix rules validate --rules-dir rules/

The ``fix`` command orchestrates the pipeline: resolve target -> load rules ->
find matches -> ask the model to transform each (fixing confident matches,
skipping ambiguous ones) -> verify on a fresh branch with pytest -> report. The
``rules`` command group inspects rule files without touching any code.

Settings that back the pipeline (provider, model, region, host, rules-dir,
concurrency, timeout, max-retries) follow a precedence chain: an explicit CLI flag
wins, then an ``AUTOFIX_*`` env var, then a config file (``.autofix.toml`` or
``[tool.autofix]``), then the built-in default. Overridable options default to
``None`` here precisely so an unset flag can fall through to those lower layers.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import NoReturn

import typer

from . import reporter
from .config import load_config, resolve
from .matcher import Match, find_matches
from .rules import Rule, RuleError, gather_rule_paths, load_rule, load_rules
from .target import TargetError, python_files, resolve_target
from .transformer import (
    DEFAULT_CONCURRENCY,
    DEFAULT_HOST,
    DEFAULT_MAX_RETRIES,
    DEFAULT_PROVIDER,
    DEFAULT_REGION,
    DEFAULT_TIMEOUT,
    PROVIDERS,
    FixResult,
    ProviderError,
    changed_sources,
    default_model_for,
    estimate_usage,
    make_agent,
    plan_fixes,
    preflight,
)
from .verifier import VerifierError, verify

log = logging.getLogger("autofix")

app = typer.Typer(
    add_completion=False,
    help="Rule-driven vulnerability remediation: find and fix a pattern across a Python codebase.",
)
rules_app = typer.Typer(add_completion=False, help="Inspect and validate rule files.")
app.add_typer(rules_app, name="rules")


def _fail(msg: str) -> NoReturn:
    typer.secho(f"error: {msg}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _configure_logging(verbose: int, quiet: bool) -> None:
    """Diagnostics go to stderr so stdout (report / --json) stays clean."""

    if quiet:
        level = logging.ERROR
    elif verbose >= 2:
        level = logging.DEBUG
    elif verbose == 1:
        level = logging.INFO
    else:
        level = logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s", stream=sys.stderr)


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


def _run_transforms(
    agent,
    matches: list[Match],
    root: Path,
    *,
    concurrency: int,
    timeout: float,
    max_retries: int,
    show_progress: bool,
) -> list[FixResult]:
    """Run the transforms, driving a stderr progress bar when appropriate."""

    if not show_progress:
        return plan_fixes(
            agent, matches, root,
            concurrency=concurrency, timeout=timeout, max_retries=max_retries,
        )

    from rich.console import Console
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        TextColumn,
        TimeElapsedColumn,
    )

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=Console(stderr=True),
        transient=True,
    ) as progress:
        task = progress.add_task("Transforming", total=len(matches))
        return plan_fixes(
            agent, matches, root,
            concurrency=concurrency, timeout=timeout, max_retries=max_retries,
            on_result=lambda _r: progress.advance(task),
        )


@app.command()
def fix(
    target: str = typer.Argument(..., help="Local path or GitHub URL of the repository."),
    rule: list[Path] = typer.Option([], "--rule", "-r", help="Rule YAML file (repeatable)."),
    rules_dir: str = typer.Option(
        None, "--rules-dir", help="Directory of *.yaml rule files to load (in addition to --rule)."
    ),
    provider: str = typer.Option(
        None, "--provider", help=f"Model backend: {' | '.join(PROVIDERS)}."
    ),
    model: str = typer.Option(
        None, "--model", help="Model id (defaults to the provider's default model)."
    ),
    region: str = typer.Option(None, "--region", help="AWS region (bedrock only)."),
    host: str = typer.Option(None, "--host", help="Ollama host URL (ollama only)."),
    concurrency: int = typer.Option(
        None, "--concurrency", help="Number of matches to transform in parallel."
    ),
    timeout: float = typer.Option(
        None, "--timeout", help="Overall per-match budget in seconds (includes retries)."
    ),
    max_retries: int = typer.Option(
        None, "--max-retries", help="Extra attempts after the first for transient failures."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Match, transform, and report only; write nothing, run no tests."
    ),
    no_test: bool = typer.Option(
        False, "--no-test", help="Branch and commit the fixes but skip the pytest step."
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit a machine-readable JSON report instead of the human report."
    ),
    verbose: int = typer.Option(
        0, "--verbose", "-v", count=True, help="Increase log verbosity (-v info, -vv debug)."
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress the narrative report."),
) -> None:
    """Find and fix every instance of each rule's pattern in the target."""

    _configure_logging(verbose, quiet)
    cfg = load_config()

    provider = resolve("provider", provider, cfg, DEFAULT_PROVIDER)
    if provider not in PROVIDERS:
        _fail(f"unknown provider '{provider}' (choose one of: {', '.join(PROVIDERS)})")
    model = resolve("model", model, cfg, "")  # "" == unset; resolved to a default below
    region = resolve("region", region, cfg, DEFAULT_REGION)
    host = resolve("host", host, cfg, DEFAULT_HOST)
    rules_dir_resolved = resolve("rules_dir", rules_dir, cfg, "") or None
    concurrency = resolve("concurrency", concurrency, cfg, DEFAULT_CONCURRENCY, int)
    timeout = resolve("timeout", timeout, cfg, DEFAULT_TIMEOUT, float)
    max_retries = resolve("max_retries", max_retries, cfg, DEFAULT_MAX_RETRIES, int)

    if concurrency < 1:
        _fail("--concurrency must be >= 1")
    if timeout < 1:
        _fail("--timeout must be >= 1")
    if max_retries < 0:
        _fail("--max-retries must be >= 0")

    # The narrative report is stdout; suppress it under --json or --quiet.
    narrate = not json_out and not quiet

    try:
        resolved = resolve_target(target)
        rule_paths = gather_rule_paths([str(p) for p in rule], rules_dir_resolved)
        rules = load_rules(rule_paths)
    except (TargetError, RuleError) as exc:
        _fail(str(exc))

    files = python_files(resolved.path)
    log.info("scanning %s: %d file(s), %d rule(s)", resolved.path, len(files), len(rules))
    if narrate:
        reporter.scanning(resolved.path, len(files), len(rules))

    matches: list[Match] = []
    for r in rules:
        matches.extend(find_matches(r, files))

    if not matches:
        if json_out:
            typer.echo(reporter.json_report([], resolved.path, provider, dry_run=dry_run))
        elif narrate:
            reporter.no_matches()
        raise typer.Exit(code=0)

    model_id = model or default_model_for(provider)
    if narrate:
        reporter.transforming(provider, model_id, len(matches), len(rules))

    try:
        resolved_model = preflight(provider, model=model_id, host=host, region=region)
    except ProviderError as exc:
        _fail(str(exc))
    agent = make_agent(provider, model=resolved_model, host=host, region=region)

    results = _run_transforms(
        agent, matches, resolved.path,
        concurrency=concurrency, timeout=timeout, max_retries=max_retries,
        show_progress=narrate and sys.stderr.isatty(),
    )

    fixed = [r for r in results if r.status == "fixed"]
    skipped = [r for r in results if r.status == "skipped"]
    errored = [r for r in results if r.status == "error"]
    usage = estimate_usage(matches, results)

    if narrate:
        reporter.fixes(results, resolved.path)
        reporter.summary(len(matches), len(fixed), len(skipped), len(errored))
        reporter.cost(usage, provider)

    verify_result = None
    if dry_run:
        if narrate:
            reporter.dry_run_note()
    elif fixed:
        try:
            verify_result = verify(
                resolved.path, changed_sources(results), resolved.path, run_tests=not no_test
            )
        except VerifierError as exc:
            _fail(str(exc))
        if narrate:
            reporter.verification(verify_result)
    elif narrate:
        reporter.no_fixes()

    if json_out:
        typer.echo(
            reporter.json_report(
                results, resolved.path, provider,
                verify_result=verify_result, usage=usage, dry_run=dry_run,
            )
        )


@rules_app.command("list")
def rules_list(
    rule: list[Path] = typer.Option([], "--rule", "-r", help="Rule YAML file (repeatable)."),
    rules_dir: str = typer.Option(
        None, "--rules-dir", help="Directory of *.yaml rule files to list."
    ),
) -> None:
    """List the rules that would be loaded, with their rationale."""

    cfg = load_config()
    rules_dir_resolved = resolve("rules_dir", rules_dir, cfg, "") or None
    try:
        rule_paths = gather_rule_paths([str(p) for p in rule], rules_dir_resolved)
        rules = load_rules(rule_paths)
    except RuleError as exc:
        _fail(str(exc))
    reporter.rules_list(rules)


@rules_app.command("validate")
def rules_validate(
    rule: list[Path] = typer.Option([], "--rule", "-r", help="Rule YAML file (repeatable)."),
    rules_dir: str = typer.Option(
        None, "--rules-dir", help="Directory of *.yaml rule files to validate."
    ),
) -> None:
    """Load and schema-check rule files without running a fix."""

    cfg = load_config()
    rules_dir_resolved = resolve("rules_dir", rules_dir, cfg, "") or None
    try:
        rule_paths = gather_rule_paths([str(p) for p in rule], rules_dir_resolved)
    except RuleError as exc:
        _fail(str(exc))

    ok: list[tuple[Path, Rule]] = []
    bad: list[tuple[Path, str]] = []
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
