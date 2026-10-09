"""Reporter: the calm, reviewable terminal report.

Turns the pipeline's results into sectioned output — a per-fix breakdown with the
rule's rationale printed alongside its changes, a colorized inline unified diff,
summary counts, the explicit zero-match message, and an unmissable culprit callout
on red. Tone is precise and quiet: a security tool earning trust.

Color is applied with ``typer.style`` and degrades automatically when output is
piped — click strips styles from a non-terminal stream — so a redirected log stays
readable. Output is kept ASCII-only so it renders on every console encoding
(notably Windows cp1252), where a stray Unicode glyph would raise on write.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from .doctor import FAIL, OK, WARN, Check
from .plan import StalePlanFile
from .rules import Rule
from .transformer import FixResult, UsageEstimate, estimated_cost_usd
from .verifier import NO_TESTS, PASSED, SKIPPED, VerifyResult


def scanning(target: Path, n_files: int, n_rules: int) -> None:
    typer.echo(f"Scanning {target} - {n_files} Python file(s), {n_rules} rule(s).")


def _rel(path: Path, root: Path) -> str:
    """Path relative to ``root`` in posix form, or the absolute path if unrelated."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def unreadable_files(unreadable: list[tuple[Path, str]], root: Path) -> None:
    """Warn that some files could not be read/parsed, so coverage is incomplete."""
    typer.secho(
        f"\nWarning: {len(unreadable)} file(s) could not be scanned "
        "(not matched; coverage is incomplete):",
        fg=typer.colors.YELLOW,
        bold=True,
    )
    for path, reason in unreadable:
        typer.echo(f"    {_rel(path, root)}: {reason}")


def no_matches() -> None:
    """The explicit zero-match path — never silent success."""
    typer.secho("\nNothing matched. No changes to make.", fg=typer.colors.YELLOW, bold=True)


def summary_scan(matched: int, n_rules: int) -> None:
    typer.secho(f"Summary: {matched} match(es) across {n_rules} rule(s).", bold=True)


def scan_matches(matches: list, root: Path) -> None:
    """Model-free scan report: every match with its rule id and rationale.

    No fix is proposed and no model is called; this is the "what would be looked
    at" preview, grouped by rule so the security rationale travels with the sites.
    """

    from .matcher import Match  # local import to avoid a cycle at module load

    by_rule: dict[str, list[Match]] = {}
    for m in matches:
        by_rule.setdefault(m.rule.id, []).append(m)

    for rule_id in sorted(by_rule):
        group = by_rule[rule_id]
        rule = group[0].rule
        typer.secho(f"[{rule_id}]  {len(group)} match(es)", fg=typer.colors.CYAN, bold=True)
        typer.echo(f"    rule: {rule.rationale}")
        for m in group:
            loc = f"{_rel(m.file_path, root)}:{m.line}"
            typer.echo(f"    - {loc}  {m.snippet}")
        typer.echo("")


def scan_json(matches: list, root: Path, unreadable: list[tuple[Path, str]] | None = None) -> str:
    """Machine-readable scan report (no fixes, no model)."""

    unreadable = unreadable or []
    entries = [
        {
            "file": _rel(m.file_path, root),
            "line": m.line,
            "column": m.column,
            "rule": m.rule.id,
            "rationale": m.rule.rationale,
            "snippet": m.snippet,
        }
        for m in matches
    ]
    counts: dict[str, int] = {}
    for m in matches:
        counts[m.rule.id] = counts.get(m.rule.id, 0) + 1
    payload = {
        "matched": len(matches),
        "by_rule": counts,
        "matches": entries,
        "unreadable": [{"file": _rel(p, root), "reason": r} for p, r in unreadable],
    }
    return json.dumps(payload, indent=2)


def transforming(provider: str, model_id: str, n_matches: int, n_rules: int) -> None:
    typer.echo(
        f"Found {n_matches} match(es) across {n_rules} rule(s). "
        f"Asking {provider}:{model_id} to transform each...\n"
    )


def _color_diff(diff: str) -> str:
    lines = []
    for line in diff.splitlines():
        if line.startswith(("+++", "---")):
            lines.append(typer.style(line, bold=True))
        elif line.startswith("@@"):
            lines.append(typer.style(line, fg=typer.colors.CYAN))
        elif line.startswith("+"):
            lines.append(typer.style(line, fg=typer.colors.GREEN))
        elif line.startswith("-"):
            lines.append(typer.style(line, fg=typer.colors.RED))
        else:
            lines.append(line)
    return "\n".join(lines)


def fixes(results: list[FixResult], root: Path) -> None:
    """Per-match breakdown: location, rule id + rationale, reason, and (fixed) diff."""

    for r in results:
        rel = r.match.file_path.relative_to(root).as_posix()
        loc = f"{rel}:{r.match.line}"
        rule = r.match.rule

        if r.status == "fixed" and r.applied:
            typer.secho(f"[FIXED] {loc}  [{rule.id}]", fg=typer.colors.GREEN, bold=True)
        elif r.status == "fixed":  # produced a fix, but a conflict kept it out
            typer.secho(f"[DROPPED] {loc}  [{rule.id}]", fg=typer.colors.YELLOW, bold=True)
        elif r.status == "error":
            typer.secho(f"[ERROR] {loc}  [{rule.id}]", fg=typer.colors.RED, bold=True)
        else:
            typer.secho(f"[SKIP]  {loc}  [{rule.id}]", fg=typer.colors.YELLOW, bold=True)

        typer.echo(f"    rule: {rule.rationale}")
        if r.status == "fixed" and r.applied:
            typer.echo(f"    why:  {r.reason}")
            if r.diff:
                typer.echo(_color_diff(r.diff.rstrip("\n")))
        elif r.status == "fixed":
            typer.echo(f"    why:  {r.reason}")
            typer.echo(f"    dropped: {r.conflict}")
        elif r.status == "error":
            typer.echo(f"    error: {r.reason}")
        else:
            typer.echo(f"    left alone: {r.reason}")
        typer.echo("")


def summary(
    matched: int, fixed: int, skipped: int, errored: int = 0, conflicted: int = 0
) -> None:
    line = f"Summary: {matched} matched, {fixed} fixed, {skipped} skipped"
    if conflicted:
        line += f", {conflicted} dropped (conflict)"
    if errored:
        line += f", {errored} errored"
    typer.secho(line, bold=True)


def cost(usage: UsageEstimate, provider: str) -> None:
    """A single, clearly-approximate usage/cost line for the run."""

    line = (
        f"Usage (approx): {usage.input_tokens} in + {usage.output_tokens} out tokens "
        f"over {usage.calls} call(s)"
    )
    est = estimated_cost_usd(usage, provider)
    if est is None:
        line += f" - {provider} runs locally, no API cost."
    else:
        line += f" - est. ${est:.4f} ({provider}, approximate)."
    typer.secho(line)


def dry_run_note() -> None:
    typer.secho(
        "\nDry run: no files written, no branch created, no tests run.",
        fg=typer.colors.YELLOW,
        bold=True,
    )


def no_fixes() -> None:
    typer.echo("\nNo confident fixes to verify.")


def baseline_red() -> None:
    """Warn that the target's suite was already failing before any fix."""
    typer.secho(
        "\nWarning: the target's test suite was already failing BEFORE any fix. "
        "A red run below may reflect pre-existing failures, not Autofix's changes.",
        fg=typer.colors.YELLOW,
        bold=True,
    )


def rules_list(rules: list[Rule]) -> None:
    """List loaded rules with id, match target, and rationale."""

    for rule in rules:
        typer.secho(f"[{rule.id}]  match: {rule.match.call}", fg=typer.colors.CYAN, bold=True)
        typer.echo(f"    {rule.rationale}")
    typer.secho(f"\n{len(rules)} rule(s)", bold=True)


def rules_validation(
    ok: list[tuple[Path, Rule]], bad: list[tuple[Path, str]]
) -> None:
    """Report which rule files loaded cleanly and which failed schema checks."""

    for path, rule in ok:
        typer.secho(f"[OK]   {path}  [{rule.id}]", fg=typer.colors.GREEN)
    for path, msg in bad:
        typer.secho(f"[BAD]  {path}", fg=typer.colors.RED, bold=True)
        typer.echo(f"    {msg}")
    typer.secho(f"\n{len(ok)} valid, {len(bad)} invalid", bold=True)


def plan_written(path: Path, n_files: int) -> None:
    typer.secho(
        f"\nPlan written to {path} ({n_files} file(s)). "
        f"Replay later with: autofix apply {path}",
        fg=typer.colors.CYAN,
        bold=True,
    )


def stale_plan(stale: list[StalePlanFile]) -> None:
    """Loudly report files that drifted from the plan, so nothing is clobbered."""
    typer.secho(
        f"\n{len(stale)} file(s) have changed since the plan was created:",
        fg=typer.colors.RED,
        bold=True,
    )
    for s in stale:
        typer.secho(f"    {s.path}: {s.reason}", fg=typer.colors.RED)


def apply_aborted(n_stale: int) -> None:
    typer.secho(
        f"Aborted: {n_stale} stale file(s). Re-run `fix` to regenerate a current plan.",
        fg=typer.colors.RED,
        bold=True,
    )


def apply_json(
    plan: dict,
    root: Path,
    patch: dict[Path, str],
    stale: list[StalePlanFile],
    *,
    verify_result: VerifyResult | None,
) -> str:
    """Machine-readable result of an `apply` run."""

    payload: dict = {
        "schema": plan.get("schema"),
        "root": str(root),
        "applied_files": sorted(_rel(p, root) for p in patch),
        "stale": [{"file": s.path, "reason": s.reason} for s in stale],
        "aborted": bool(stale),
    }
    if verify_result is not None:
        payload["verification"] = {
            "branch": verify_result.branch,
            "workspace": str(verify_result.workspace),
            "isolated": verify_result.isolated,
            "state": verify_result.state,
            "tests_ran": verify_result.tests_ran,
            "passed": verify_result.passed,
            "committed": verify_result.committed,
            "culprits": verify_result.culprits,
        }
    return json.dumps(payload, indent=2)


def verification(vr: VerifyResult) -> None:
    """Branch/commit/test outcome, with the loud culprit callout on red."""

    where = f"{vr.workspace} (isolated copy)" if vr.isolated else str(vr.workspace)
    typer.echo("\nVerifying on a fresh branch...")
    typer.echo(f"Branch {vr.branch} - {len(vr.committed)} file(s) committed in {where}")

    if vr.state == SKIPPED:
        typer.secho("Tests: SKIPPED (--no-test)", fg=typer.colors.YELLOW, bold=True)
        return

    if vr.state == NO_TESTS:
        typer.secho(
            "Tests: NO TESTS COLLECTED - nothing was verified.",
            fg=typer.colors.YELLOW,
            bold=True,
        )
        typer.echo(f"Branch left intact for inspection: {vr.workspace}")
        return

    if vr.state == PASSED:
        typer.secho("Tests: PASS", fg=typer.colors.GREEN, bold=True)
        return

    typer.secho("Tests: FAIL", fg=typer.colors.RED, bold=True)
    if vr.culprits:
        typer.secho("======== LIKELY CULPRIT ========", fg=typer.colors.RED, bold=True)
        for c in vr.culprits:
            typer.secho(f"    {c}", fg=typer.colors.RED, bold=True)
    if vr.pytest_output.strip():
        typer.echo("---- pytest output ----")
        typer.echo(vr.pytest_output.rstrip())
    typer.echo(f"Branch left intact for inspection: {vr.workspace}")


def json_report(
    results: list[FixResult],
    root: Path,
    provider: str,
    *,
    verify_result: VerifyResult | None = None,
    usage: UsageEstimate | None = None,
    dry_run: bool = False,
    baseline_ok: bool = True,
    unreadable: list[tuple[Path, str]] | None = None,
) -> str:
    """Render the run as a machine-readable JSON string (for CI / piping).

    Mirrors the human report's information: per-match outcomes, summary counts,
    the verification result (with its explicit state) when one ran, the
    pre-fix baseline health, files that could not be scanned, and the approximate
    usage/cost.
    """

    counts = {"fixed": 0, "skipped": 0, "error": 0}
    applied_count = 0
    conflicted_count = 0
    entries = []
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
        is_applied = r.status == "fixed" and r.applied
        if is_applied:
            applied_count += 1
        elif r.status == "fixed":
            conflicted_count += 1
        # outcome tracks proposed -> applied -> verified; verification (if any) is
        # whole-patch, so individual entries report up to "applied" here.
        if r.status != "fixed":
            outcome = r.status
        elif r.applied:
            outcome = "applied"
        else:
            outcome = "dropped"
        entry = {
            "file": r.match.file_path.relative_to(root).as_posix(),
            "line": r.match.line,
            "rule": r.match.rule.id,
            "status": r.status,
            "outcome": outcome,
            "applied": is_applied,
            "reason": r.reason,
        }
        if r.status == "fixed" and not r.applied:
            entry["conflict"] = r.conflict
        entries.append(entry)

    verified = bool(verify_result is not None and verify_result.passed)
    unreadable = unreadable or []
    payload: dict = {
        "dry_run": dry_run,
        "baseline_tests_passed": baseline_ok,
        "summary": {
            "matched": len(results),
            "fixed": counts["fixed"],
            "applied": applied_count,
            "dropped": conflicted_count,
            "skipped": counts["skipped"],
            "errored": counts["error"],
            "verified": verified,
            "unreadable": len(unreadable),
        },
        "results": entries,
        "unreadable": [
            {"file": _rel(p, root), "reason": reason} for p, reason in unreadable
        ],
    }

    if verify_result is not None:
        payload["verification"] = {
            "branch": verify_result.branch,
            "workspace": str(verify_result.workspace),
            "isolated": verify_result.isolated,
            "state": verify_result.state,
            "tests_ran": verify_result.tests_ran,
            "passed": verify_result.passed,
            "committed": verify_result.committed,
            "culprits": verify_result.culprits,
        }

    if usage is not None:
        payload["usage"] = {
            "calls": usage.calls,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "estimated_cost_usd": estimated_cost_usd(usage, provider),
            "approximate": True,
        }

    return json.dumps(payload, indent=2)


def _doctor_marker(status: str) -> str:
    return {OK: "[OK]  ", WARN: "[WARN]", FAIL: "[FAIL]"}.get(status, "[??]  ")


def doctor(checks: list[Check]) -> None:
    """Render the environment diagnostics, colored by status."""

    color = {OK: typer.colors.GREEN, WARN: typer.colors.YELLOW, FAIL: typer.colors.RED}
    for c in checks:
        typer.secho(f"{_doctor_marker(c.status)} {c.name}", fg=color.get(c.status), bold=True)
        typer.echo(f"    {c.detail}")
    failed = sum(1 for c in checks if c.status == FAIL)
    warned = sum(1 for c in checks if c.status == WARN)
    tail = f"\n{len(checks)} check(s): {failed} failed, {warned} warning(s)"
    typer.secho(tail, bold=True, fg=typer.colors.RED if failed else typer.colors.GREEN)


def doctor_json(checks: list[Check]) -> str:
    payload = {
        "ok": all(c.status != FAIL for c in checks),
        "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in checks],
    }
    return json.dumps(payload, indent=2)
