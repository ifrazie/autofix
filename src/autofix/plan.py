"""Remediation plans: save a reviewed fix set, apply it later without a model.

A *plan* is the deterministic record of one ``fix`` run's confident, conflict-
resolved output. It captures, per changed file, the exact rewritten source plus a
hash of the *original* source it was computed against, alongside the per-fix
explanations and the provider/model/version metadata. That lets the expensive,
non-deterministic step (asking a model) happen once, be reviewed, and then be
replayed byte-for-byte by :func:`apply` — which refuses to touch a file whose
current contents no longer match the recorded original hash, so a plan can never
be applied blindly to code that has drifted underneath it.

The plan is plain JSON (schema ``PLAN_SCHEMA``) so it diffs cleanly in review and
can be produced/consumed by other tooling.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .transformer import FixResult

PLAN_SCHEMA = "autofix/plan/v1"


class PlanError(ValueError):
    """Raised when a plan file is missing, malformed, or does not match the target."""


def source_hash(text: str) -> str:
    """Stable content hash of a source file, used to detect drift before applying."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class PlannedFile:
    """One file's planned rewrite plus the original it was derived from."""

    path: str              # posix path relative to the target root
    original_sha: str      # hash of the original source the fix was computed against
    new_source: str        # the full rewritten source to write on apply
    fixes: list[dict]      # per-fix explanations (rule id, line, reason)


def build_plan(
    results: list[FixResult],
    patch: dict[Path, str],
    root: Path,
    *,
    provider: str,
    model: str,
) -> dict:
    """Assemble a JSON-serializable plan from a completed ``fix`` run.

    ``patch`` is the output of :func:`autofix.transformer.changed_sources` (already
    conflict-resolved, imports applied); ``results`` carries the per-match
    explanations. Only files present in ``patch`` are recorded, and only the
    ``applied`` fixes for each are listed.
    """

    fixes_by_path: dict[Path, list[FixResult]] = {}
    for r in results:
        if r.status == "fixed" and r.applied and r.match.file_path in patch:
            fixes_by_path.setdefault(r.match.file_path, []).append(r)

    files = []
    for path, new_source in sorted(patch.items()):
        original = path.read_text(encoding="utf-8")
        rel = path.relative_to(root).as_posix()
        fixes = [
            {
                "rule": r.match.rule.id,
                "line": r.match.line,
                "reason": r.reason,
            }
            for r in fixes_by_path.get(path, [])
        ]
        files.append(
            {
                "path": rel,
                "original_sha": source_hash(original),
                "new_source": new_source,
                "fixes": fixes,
            }
        )

    return {
        "schema": PLAN_SCHEMA,
        "created_at": datetime.now(UTC).isoformat(),
        "provider": provider,
        "model": model,
        "root": str(root),
        "files": files,
    }


def write_plan(plan: dict, path: Path) -> None:
    """Serialize a plan to ``path`` as pretty JSON."""
    path.write_text(json.dumps(plan, indent=2), encoding="utf-8")


def load_plan(path: Path) -> dict:
    """Read and structurally validate a plan file."""

    if not path.is_file():
        raise PlanError(f"plan file not found: {path}")
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PlanError(f"{path}: could not parse plan JSON: {exc}") from exc

    if not isinstance(plan, dict) or plan.get("schema") != PLAN_SCHEMA:
        raise PlanError(f"{path}: not an {PLAN_SCHEMA} plan file")
    files = plan.get("files")
    if not isinstance(files, list):
        raise PlanError(f"{path}: plan 'files' must be a list")
    for entry in files:
        missing = {"path", "original_sha", "new_source"} - set(entry or {})
        if not isinstance(entry, dict) or missing:
            raise PlanError(f"{path}: each file entry needs path, original_sha, new_source")
    return plan


@dataclass
class StalePlanFile:
    """A plan file whose target drifted from the recorded original."""

    path: str
    reason: str


def resolve_plan_patch(plan: dict, root: Path) -> tuple[dict[Path, str], list[StalePlanFile]]:
    """Turn a loaded plan into a ``{path: new_source}`` patch, checking for drift.

    Each recorded file's current contents are hashed and compared to the plan's
    ``original_sha``. A file that is missing, unreadable, or whose hash no longer
    matches is reported as *stale* and left out of the returned patch — so
    :func:`apply` can refuse a plan that no longer fits the tree instead of
    clobbering changed code.
    """

    patch: dict[Path, str] = {}
    stale: list[StalePlanFile] = []
    for entry in plan["files"]:
        rel = entry["path"]
        abs_path = root / rel
        if not abs_path.is_file():
            stale.append(StalePlanFile(rel, "file no longer exists"))
            continue
        try:
            current = abs_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            stale.append(StalePlanFile(rel, f"could not read: {exc}"))
            continue
        if source_hash(current) != entry["original_sha"]:
            stale.append(
                StalePlanFile(rel, "source has changed since the plan was created")
            )
            continue
        patch[abs_path] = entry["new_source"]
    return patch, stale
