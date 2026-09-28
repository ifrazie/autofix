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

from .rules import Rule
from .transformer import FixResult, UsageEstimate, estimated_cost_usd
from .verifier import VerifyResult


def scanning(target: Path, n_files: int, n_rules: int) -> None:
    typer.echo(f"Scanning {target} - {n_files} Python file(s), {n_rules} rule(s).")


def no_matches() -> None:
    """The explicit zero-match path — never silent success."""
    typer.secho("\nNothing matched. No changes to make.", fg=typer.colors.YELLOW, bold=True)


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

        if r.status == "fixed":
            typer.secho(f"[FIXED] {loc}  [{rule.id}]", fg=typer.colors.GREEN, bold=True)
        elif r.status == "error":
            typer.secho(f"[ERROR] {loc}  [{rule.id}]", fg=typer.colors.RED, bold=True)
        else:
            typer.secho(f"[SKIP]  {loc}  [{rule.id}]", fg=typer.colors.YELLOW, bold=True)

        typer.echo(f"    rule: {rule.rationale}")
        if r.status == "fixed":
            typer.echo(f"    why:  {r.reason}")
            if r.diff:
                typer.echo(_color_diff(r.diff.rstrip("\n")))
        elif r.status == "error":
            typer.echo(f"    error: {r.reason}")
        else:
            typer.echo(f"    left alone: {r.reason}")
        typer.echo("")


def summary(matched: int, fixed: int, skipped: int, errored: int = 0) -> None:
    line = f"Summary: {matched} matched, {fixed} fixed, {skipped} skipped"
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


def verification(vr: VerifyResult) -> None:
    """Branch/commit/test outcome, with the loud culprit callout on red."""

    where = f"{vr.workspace} (isolated copy)" if vr.isolated else str(vr.workspace)
    typer.echo("\nVerifying on a fresh branch...")
    typer.echo(f"Branch {vr.branch} - {len(vr.committed)} file(s) committed in {where}")

    if not vr.tests_ran:
        typer.secho("Tests: SKIPPED (--no-test)", fg=typer.colors.YELLOW, bold=True)
        return

    if vr.passed:
        typer.secho("Tests: PASS", fg=typer.colors.GREEN, bold=True)
        return

    typer.secho("Tests: FAIL", fg=typer.colors.RED, bold=True)
    if vr.culprits:
        typer.secho("======== LIKELY CULPRIT ========", fg=typer.colors.RED, bold=True)
        for c in vr.culprits:
            typer.secho(f"    {c}", fg=typer.colors.RED, bold=True)
    typer.echo(f"Branch left intact for inspection: {vr.workspace}")


def json_report(
    results: list[FixResult],
    root: Path,
    provider: str,
    *,
    verify_result: VerifyResult | None = None,
    usage: UsageEstimate | None = None,
    dry_run: bool = False,
) -> str:
    """Render the run as a machine-readable JSON string (for CI / piping).

    Mirrors the human report's information: per-match outcomes, summary counts,
    the verification result when one ran, and the approximate usage/cost.
    """

    counts = {"fixed": 0, "skipped": 0, "error": 0}
    entries = []
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
        entries.append(
            {
                "file": r.match.file_path.relative_to(root).as_posix(),
                "line": r.match.line,
                "rule": r.match.rule.id,
                "status": r.status,
                "reason": r.reason,
            }
        )

    payload: dict = {
        "dry_run": dry_run,
        "summary": {
            "matched": len(results),
            "fixed": counts["fixed"],
            "skipped": counts["skipped"],
            "errored": counts["error"],
        },
        "results": entries,
    }

    if verify_result is not None:
        payload["verification"] = {
            "branch": verify_result.branch,
            "workspace": str(verify_result.workspace),
            "isolated": verify_result.isolated,
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
