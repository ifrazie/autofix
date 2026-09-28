"""Verifier: land the confident fixes on a fresh branch and run the tests.

This is the "verified" half of the promise. It never mutates the user's working
tree in surprising ways:

* If the target is its own git repo, it branches **in place** onto
  ``autofix/<timestamp>``, commits the fixes there, runs ``pytest``, and then
  checks the original branch back out — so the working branch is untouched and
  the autofix branch is left intact for inspection.
* Otherwise (our ``sample-repo`` lives *inside* this project, so it has no repo of
  its own) it copies the target into a temp dir, ``git init``s a baseline commit,
  and does all of the above there. The user's real tree is never touched.

On red, it names the fix most likely responsible rather than leaving the failure
unexplained.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

_GIT_IDENTITY = ("-c", "user.email=autofix@example.com", "-c", "user.name=autofix")
_IGNORE = (".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", "node_modules")


class VerifierError(RuntimeError):
    """Raised when a git operation the verifier depends on fails."""


@dataclass
class VerifyResult:
    """Outcome of the branch/commit/test cycle."""

    workspace: Path            # where the branch lives (temp copy or the target repo)
    branch: str                # autofix/<timestamp>
    isolated: bool             # True if we worked in a temp copy
    committed: list[str]       # relative paths of files written and committed
    passed: bool               # did pytest come back green?
    pytest_output: str         # captured pytest stdout+stderr
    culprits: list[str] = field(default_factory=list)  # likely-culprit paths on red


def _git(workspace: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=workspace, capture_output=True, text=True)


def _prepare_workspace(target: Path) -> tuple[Path, bool]:
    """Return ``(workspace, isolated)``.

    Branch in place if ``target`` is its own git repo; otherwise copy it to a temp
    dir and ``git init`` a baseline commit so we have an isolated repo to work in.
    """

    if (target / ".git").is_dir():
        return target, False

    tmp = Path(tempfile.mkdtemp(prefix="autofix-"))
    workspace = tmp / target.name
    shutil.copytree(target, workspace, ignore=shutil.ignore_patterns(*_IGNORE))

    init = _git(workspace, "init")
    if init.returncode != 0:
        raise VerifierError(f"git init failed: {init.stderr.strip()}")
    _git(workspace, "add", "-A")
    baseline = _git(workspace, *_GIT_IDENTITY, "commit", "-m", "baseline before autofix")
    if baseline.returncode != 0:
        raise VerifierError(f"baseline commit failed: {baseline.stderr.strip()}")
    return workspace, True


def _current_branch(workspace: Path) -> str:
    return _git(workspace, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "HEAD"


def _guess_culprits(pytest_output: str, changed_files: list[str]) -> list[str]:
    """Name the fix(es) most likely to blame for a red run.

    Primary signal: a failing test file whose name maps to a changed source file
    (``tests/test_auth.py`` → ``app/auth.py`` by the ``auth`` stem). Falling back
    to the most-recently-applied fixes when nothing maps cleanly.
    """

    by_stem: dict[str, list[str]] = {}
    for c in changed_files:
        by_stem.setdefault(Path(c).stem, []).append(c)

    suspects: list[str] = []
    for line in pytest_output.splitlines():
        line = line.strip()
        if not line.startswith(("FAILED ", "ERROR ")):
            continue
        node = line.split(None, 1)[1]
        core = Path(node.split("::", 1)[0]).stem
        if core.startswith("test_"):
            core = core[len("test_"):]
        elif core.endswith("_test"):
            core = core[: -len("_test")]
        for c in by_stem.get(core, []):
            if c not in suspects:
                suspects.append(c)

    if not suspects:  # no clean mapping — blame most recently applied first
        suspects = list(reversed(changed_files))
    return suspects


def verify(
    target: Path, changed: dict[Path, str], root: Path, test_path: str = "."
) -> VerifyResult:
    """Apply ``changed`` sources on a fresh branch, commit, and run ``pytest``.

    ``changed`` maps original file paths to their rewritten source; ``root`` is the
    target root those paths are relative to. Returns a :class:`VerifyResult`; the
    branch is always left intact for inspection.
    """

    workspace, isolated = _prepare_workspace(target)
    base_branch = _current_branch(workspace)
    branch = "autofix/" + datetime.now().strftime("%Y%m%d-%H%M%S")

    made = _git(workspace, "checkout", "-b", branch)
    if made.returncode != 0:
        raise VerifierError(f"could not create branch {branch}: {made.stderr.strip()}")

    committed: list[str] = []
    for orig_path, new_source in changed.items():
        rel = orig_path.relative_to(root)
        dest = workspace / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(new_source, encoding="utf-8")
        committed.append(rel.as_posix())

    _git(workspace, "add", "-A")
    commit = _git(
        workspace, *_GIT_IDENTITY, "commit", "-m", f"autofix: apply {len(committed)} fix(es)"
    )
    if commit.returncode != 0:
        raise VerifierError(f"commit failed: {commit.stderr.strip()}")

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rfE", test_path],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    passed = proc.returncode == 0
    output = proc.stdout + proc.stderr
    culprits = [] if passed else _guess_culprits(output, committed)

    if not isolated:  # leave the user on their original branch; autofix branch stays
        _git(workspace, "checkout", base_branch)

    return VerifyResult(workspace, branch, isolated, committed, passed, output, culprits)
