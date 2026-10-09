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

from . import reporter, verifier
from .config import load_config, resolve
from .doctor import overall_ok, run_checks
from .matcher import Match, scan
from .plan import (
    PlanError,
    build_plan,
    load_plan,
    resolve_plan_patch,
    write_plan,
)
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
from .verifier import VerifierError, VerifyResult, baseline_tests_pass, verify

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
    include: list[str] = typer.Option(
        [], "--include", help="Only scan files matching this glob (relative to target; repeatable)."
    ),
    exclude: list[str] = typer.Option(
        [], "--exclude", help="Skip files matching this glob (relative to target; repeatable)."
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
    plan_out: Path = typer.Option(
        None, "--plan-out",
        help="Write a reusable remediation plan (JSON) to this path for later `apply`.",
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

    files = python_files(resolved.path, include=include or None, exclude=exclude or None)
    log.info("scanning %s: %d file(s), %d rule(s)", resolved.path, len(files), len(rules))
    if narrate:
        reporter.scanning(resolved.path, len(files), len(rules))

    scan_result = scan(rules, files)
    matches = scan_result.matches
    if scan_result.unreadable and narrate:
        reporter.unreadable_files(scan_result.unreadable, resolved.path)

    if not matches:
        if json_out:
            typer.echo(reporter.json_report(
                [], resolved.path, provider, dry_run=dry_run,
                unreadable=scan_result.unreadable,
            ))
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

    # Combine the fixes into the final per-file source NOW (before reporting), so
    # span conflicts are resolved and each fix's diff is regenerated from the
    # final combined file. This mutates results: a fix dropped by a conflict is
    # marked applied=False. Done once; reused for the report and the verifier.
    patch = changed_sources(results)

    skipped = [r for r in results if r.status == "skipped"]
    errored = [r for r in results if r.status == "error"]
    applied = [r for r in results if r.status == "fixed" and r.applied]
    conflicted = [r for r in results if r.status == "fixed" and not r.applied]
    usage = estimate_usage(matches, results)

    # Persist a reusable plan of the confident, conflict-resolved patch, so the
    # (metered, non-deterministic) model step can be reviewed once and replayed
    # later with `apply` — no model call needed the second time.
    if plan_out is not None and patch:
        plan = build_plan(results, patch, resolved.path, provider=provider, model=model_id)
        write_plan(plan, plan_out)
        if narrate:
            reporter.plan_written(plan_out, len(patch))

    if narrate:
        reporter.fixes(results, resolved.path)
        reporter.summary(
            len(matches), len(applied), len(skipped), len(errored), len(conflicted)
        )
        reporter.cost(usage, provider)

    verify_result = None
    baseline_ok = True
    if dry_run:
        if narrate:
            reporter.dry_run_note()
    elif applied:
        # Run the suite BEFORE applying fixes so a pre-existing red is never
        # mis-attributed to Autofix. Skipped under --no-test (no verification at all).
        if not no_test:
            try:
                baseline_ok, _ = baseline_tests_pass(resolved.path)
            except VerifierError as exc:
                _fail(str(exc))
            if not baseline_ok and narrate:
                reporter.baseline_red()
        try:
            verify_result = verify(
                resolved.path, patch, resolved.path, run_tests=not no_test
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
                baseline_ok=baseline_ok, unreadable=scan_result.unreadable,
            )
        )

    raise typer.Exit(code=_exit_code(errored, verify_result, baseline_ok, dry_run=dry_run))


def _exit_code(
    errored: list[FixResult],
    verify_result: VerifyResult | None,
    baseline_ok: bool,
    *,
    dry_run: bool,
) -> int:
    """Translate the run outcome into a process exit code.

    Nonzero means "do not trust this run as a clean success", so CI fails loudly:

    * any transform that errored out (a match we could neither fix nor cleanly skip);
    * verification that ran tests and did not come back green (FAIL or NO_TESTS);
    * a pre-existing baseline failure, so a repo that was already red can't look
      green just because our fixes happened not to break anything further.

    A dry run reports but proves nothing, so it never fails on verification state;
    it still fails if a transform errored.
    """

    if errored:
        return 1
    if dry_run:
        return 0
    if not baseline_ok:
        return 1
    # SKIPPED (--no-test) proved nothing but is an explicit user choice, so it is
    # not a failure; FAILED and NO_TESTS are.
    if verify_result is not None and verify_result.state in (verifier.FAILED, verifier.NO_TESTS):
        return 1
    return 0


@app.command("scan")
def scan_cmd(
    target: str = typer.Argument(..., help="Local path or GitHub URL of the repository."),
    rule: list[Path] = typer.Option([], "--rule", "-r", help="Rule YAML file (repeatable)."),
    rules_dir: str = typer.Option(
        None, "--rules-dir", help="Directory of *.yaml rule files to load (in addition to --rule)."
    ),
    include: list[str] = typer.Option(
        [], "--include", help="Only scan files matching this glob (relative to target; repeatable)."
    ),
    exclude: list[str] = typer.Option(
        [], "--exclude", help="Skip files matching this glob (relative to target; repeatable)."
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit a machine-readable JSON report instead of the human report."
    ),
    verbose: int = typer.Option(
        0, "--verbose", "-v", count=True, help="Increase log verbosity (-v info, -vv debug)."
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress the narrative report."),
) -> None:
    """Find every match of each rule WITHOUT calling a model (free, no writes).

    This is the deterministic half of the pipeline on its own: it shows which
    sites each rule would look at, with the security rationale, so you can preview
    (and cost nothing) before running ``fix``. Exits non-zero if a file could not
    be read or parsed, so incomplete coverage is visible in CI.
    """

    _configure_logging(verbose, quiet)
    cfg = load_config()
    rules_dir_resolved = resolve("rules_dir", rules_dir, cfg, "") or None
    narrate = not json_out and not quiet

    try:
        resolved = resolve_target(target)
        rule_paths = gather_rule_paths([str(p) for p in rule], rules_dir_resolved)
        rules = load_rules(rule_paths)
    except (TargetError, RuleError) as exc:
        _fail(str(exc))

    files = python_files(resolved.path, include=include or None, exclude=exclude or None)
    if narrate:
        reporter.scanning(resolved.path, len(files), len(rules))

    result = scan(rules, files)

    if json_out:
        typer.echo(reporter.scan_json(result.matches, resolved.path, result.unreadable))
    elif narrate:
        if result.unreadable:
            reporter.unreadable_files(result.unreadable, resolved.path)
        if result.matches:
            reporter.scan_matches(result.matches, resolved.path)
            reporter.summary_scan(len(result.matches), len(rules))
        else:
            reporter.no_matches()

    # Unreadable files mean coverage is incomplete; surface that as a failure for CI.
    raise typer.Exit(code=1 if result.unreadable else 0)


@app.command("apply")
def apply_cmd(
    plan_file: Path = typer.Argument(..., help="Plan JSON produced by `fix --plan-out`."),
    target: str = typer.Option(
        None, "--target",
        help="Repository to apply the plan to (default: the root recorded in the plan).",
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
    """Apply a saved remediation plan WITHOUT calling a model.

    Replays the exact rewritten sources recorded by a prior ``fix --plan-out`` run,
    on a fresh branch, then runs the tests — the same verification ``fix`` does.
    Each target file is checked against the plan's recorded original hash first;
    any file that has drifted is refused (never clobbered), and the apply fails.
    """

    _configure_logging(verbose, quiet)
    narrate = not json_out and not quiet

    try:
        plan = load_plan(plan_file)
    except PlanError as exc:
        _fail(str(exc))

    root = Path(target).expanduser().resolve() if target else Path(plan["root"])
    if not root.is_dir():
        _fail(f"target is not a directory: {root}")

    patch, stale = resolve_plan_patch(plan, root)

    if narrate and stale:
        reporter.stale_plan(stale)

    # A plan that no longer fits the tree must not be half-applied.
    if stale:
        if json_out:
            typer.echo(reporter.apply_json(plan, root, patch, stale, verify_result=None))
        else:
            reporter.apply_aborted(len(stale))
        raise typer.Exit(code=1)

    if not patch:
        if json_out:
            typer.echo(reporter.apply_json(plan, root, patch, stale, verify_result=None))
        elif narrate:
            typer.echo("Plan has no files to apply.")
        raise typer.Exit(code=0)

    try:
        verify_result = verify(root, patch, root, run_tests=not no_test)
    except VerifierError as exc:
        _fail(str(exc))

    if narrate:
        reporter.verification(verify_result)
    if json_out:
        typer.echo(reporter.apply_json(plan, root, patch, stale, verify_result=verify_result))

    failed = verify_result.state in (verifier.FAILED, verifier.NO_TESTS)
    raise typer.Exit(code=1 if failed else 0)


@app.command("doctor")
def doctor_cmd(
    provider: str = typer.Option(
        None, "--provider", help=f"Provider to check: {' | '.join(PROVIDERS)}."
    ),
    model: str = typer.Option(
        None, "--model", help="Model id to check (provider default if unset)."
    ),
    region: str = typer.Option(None, "--region", help="AWS region (bedrock only)."),
    host: str = typer.Option(None, "--host", help="Ollama host URL (ollama only)."),
    json_out: bool = typer.Option(
        False, "--json", help="Emit a machine-readable JSON report instead of the human report."
    ),
) -> None:
    """Check this environment is ready to run: Python, git, pytest, and provider.

    Exits non-zero if any required check fails, so it can gate a CI job or a
    first-run setup script. Provider checks honor the same config precedence as
    ``fix`` (CLI flag > env > config file > default).
    """

    cfg = load_config()
    provider = resolve("provider", provider, cfg, DEFAULT_PROVIDER)
    if provider not in PROVIDERS:
        _fail(f"unknown provider '{provider}' (choose one of: {', '.join(PROVIDERS)})")
    model_id = resolve("model", model, cfg, "") or None
    region = resolve("region", region, cfg, DEFAULT_REGION)
    host = resolve("host", host, cfg, DEFAULT_HOST)

    checks = run_checks(provider, model=model_id, region=region, host=host)
    if json_out:
        typer.echo(reporter.doctor_json(checks))
    else:
        reporter.doctor(checks)

    raise typer.Exit(code=0 if overall_ok(checks) else 1)


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
