"""Verifier: land the confident fixes on a fresh branch and run the tests.

This is the "verified" half of the promise, and it is deliberately careful never
to disturb the user's working tree:

* If the target is its own git repo, the fixes are applied in a dedicated **git
  worktree** checked out onto a new ``autofix/<timestamp>`` branch. The user's
  checkout — current branch, staged index, uncommitted edits, untracked files —
  is never touched, because a worktree is a separate directory sharing the same
  repository. The branch is left behind for inspection; the throwaway worktree
  directory is pruned.
* Otherwise (our ``sample-repo`` lives *inside* this project, so it has no repo of
  its own) it copies the target into a temp dir, ``git init``s a baseline commit,
  and branches there. The user's real tree is never touched.

Only the files Autofix actually rewrote are staged and committed, so unrelated
changes in the repository can never ride along in an autofix commit.

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

# Explicit verification states. "no_tests" means pytest ran but collected nothing,
# which must not be reported as a pass.
PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"      # --no-test: tests intentionally not run
NO_TESTS = "no_tests"    # pytest ran but collected zero tests
# pytest's "no tests collected" exit code.
_PYTEST_NO_TESTS = 5


class VerifierError(RuntimeError):
    """Raised when a git operation the verifier depends on fails."""


@dataclass
class VerifyResult:
    """Outcome of the branch/commit/test cycle."""

    workspace: Path            # where the branch/worktree lives
    branch: str                # autofix/<timestamp>
    isolated: bool             # True if we worked in a temp copy (non-repo target)
    committed: list[str]       # relative paths of files written and committed
    state: str                 # one of PASSED / FAILED / SKIPPED / NO_TESTS
    pytest_output: str         # captured pytest stdout+stderr
    culprits: list[str] = field(default_factory=list)  # likely-culprit paths on red

    @property
    def passed(self) -> bool:
        """True only on a green run with tests actually collected and passing.

        SKIPPED (``--no-test``) and NO_TESTS are explicitly *not* passes: nothing
        was proven, so callers must not treat them as success.
        """
        return self.state == PASSED

    @property
    def tests_ran(self) -> bool:
        return self.state not in (SKIPPED,)


def _git(workspace: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=workspace, capture_output=True, text=True)


def _is_git_repo(path: Path) -> bool:
    return _git(path, "rev-parse", "--is-inside-work-tree").returncode == 0


def _baseline_temp_repo(target: Path) -> Path:
    """Copy a non-repo target into a temp dir and ``git init`` a baseline commit."""

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
    return workspace


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


def _run_pytest(workspace: Path, test_path: str) -> tuple[str, str]:
    """Run pytest in ``workspace`` and classify the outcome into a state."""

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rfE", test_path],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 0:
        return PASSED, output
    if proc.returncode == _PYTEST_NO_TESTS:
        # No tests were collected, so nothing was actually verified.
        return NO_TESTS, output
    return FAILED, output


def baseline_tests_pass(target: Path, test_path: str = ".") -> tuple[bool, str]:
    """Run the target's suite BEFORE any fix, to catch a pre-existing red.

    Returns ``(ok, output)`` where ``ok`` is True when the suite is green or has
    no tests to collect (nothing pre-existing to blame). This lets the caller tell
    a regression introduced by a fix apart from a repo that was already failing,
    so a pre-existing failure is never mis-attributed to Autofix.

    The suite is run against a throwaway copy/worktree so the baseline run itself
    never mutates the user's checkout.
    """

    if _is_git_repo(target):
        wt_root = Path(tempfile.mkdtemp(prefix="autofix-base-"))
        worktree = wt_root / "tree"
        added = _git(target, "worktree", "add", "--detach", str(worktree), "HEAD")
        if added.returncode != 0:
            shutil.rmtree(wt_root, ignore_errors=True)
            raise VerifierError(f"could not create baseline worktree: {added.stderr.strip()}")
        try:
            state, output = _run_pytest(worktree, test_path)
        finally:
            _git(target, "worktree", "remove", "--force", str(worktree))
            shutil.rmtree(wt_root, ignore_errors=True)
    else:
        workspace = _baseline_temp_repo(target)
        state, output = _run_pytest(workspace, test_path)

    return state in (PASSED, NO_TESTS), output


def _commit_changes(
    workspace: Path, changed: dict[Path, str], root: Path
) -> list[str]:
    """Write each changed source into ``workspace`` and commit ONLY those files.

    Returns the committed paths (relative, posix). Staging is scoped to exactly the
    files Autofix rewrote (``git add -- <path>``), so unrelated edits already in the
    repository can never be swept into the autofix commit.
    """

    committed: list[str] = []
    for orig_path, new_source in changed.items():
        rel = orig_path.relative_to(root)
        dest = workspace / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(new_source, encoding="utf-8")
        committed.append(rel.as_posix())

    if committed:
        add = _git(workspace, "add", "--", *committed)
        if add.returncode != 0:
            raise VerifierError(f"git add failed: {add.stderr.strip()}")
    commit = _git(
        workspace, *_GIT_IDENTITY, "commit", "-m", f"autofix: apply {len(committed)} fix(es)"
    )
    if commit.returncode != 0:
        raise VerifierError(f"commit failed: {commit.stderr.strip()}")
    return committed


def verify(
    target: Path,
    changed: dict[Path, str],
    root: Path,
    test_path: str = ".",
    *,
    run_tests: bool = True,
) -> VerifyResult:
    """Apply ``changed`` sources on a fresh branch, commit, and run ``pytest``.

    ``changed`` maps original file paths to their rewritten source; ``root`` is the
    target root those paths are relative to.

    For a target that is its own git repo, the work happens in a dedicated git
    **worktree** on a new ``autofix/<timestamp>`` branch — the user's checkout
    (branch, index, uncommitted and untracked files) is never read or mutated. For
    a non-repo target, an isolated temp copy is used instead. Either way the branch
    is left intact for inspection and only Autofix's own edits are committed.

    With ``run_tests=False`` (``--no-test``) the fixes are still branched and
    committed, but pytest is skipped and the result's state is ``SKIPPED`` — which
    is *not* a pass.
    """

    branch = "autofix/" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")

    if _is_git_repo(target):
        return _verify_in_worktree(
            target, changed, root, branch, test_path, run_tests=run_tests
        )
    return _verify_in_copy(target, changed, root, branch, test_path, run_tests=run_tests)


def _verify_in_worktree(
    target: Path,
    changed: dict[Path, str],
    root: Path,
    branch: str,
    test_path: str,
    *,
    run_tests: bool,
) -> VerifyResult:
    """Isolated path for a real repo: a git worktree on a fresh branch.

    The worktree is a separate directory, so the user's main checkout is untouched
    no matter what happens here. The ``autofix/<branch>`` is kept; the throwaway
    worktree directory is always pruned, even on error.
    """

    wt_root = Path(tempfile.mkdtemp(prefix="autofix-wt-"))
    worktree = wt_root / "tree"
    added = _git(target, "worktree", "add", "-b", branch, str(worktree), "HEAD")
    if added.returncode != 0:
        shutil.rmtree(wt_root, ignore_errors=True)
        raise VerifierError(f"could not create worktree branch {branch}: {added.stderr.strip()}")

    try:
        committed = _commit_changes(worktree, changed, root)
        if run_tests:
            state, output = _run_pytest(worktree, test_path)
            culprits = _guess_culprits(output, committed) if state == FAILED else []
        else:
            state, output, culprits = SKIPPED, "", []
    finally:
        # Detach the worktree directory but KEEP the branch for inspection.
        _git(target, "worktree", "remove", "--force", str(worktree))
        shutil.rmtree(wt_root, ignore_errors=True)

    return VerifyResult(
        workspace=target,
        branch=branch,
        isolated=False,
        committed=committed,
        state=state,
        pytest_output=output,
        culprits=culprits,
    )


def _verify_in_copy(
    target: Path,
    changed: dict[Path, str],
    root: Path,
    branch: str,
    test_path: str,
    *,
    run_tests: bool,
) -> VerifyResult:
    """Isolated path for a non-repo target: temp copy + git init + branch."""

    workspace = _baseline_temp_repo(target)
    # The committed sources were written relative to ``root``; rebase onto the copy.
    rebased = {workspace / p.relative_to(root): src for p, src in changed.items()}

    made = _git(workspace, "checkout", "-b", branch)
    if made.returncode != 0:
        raise VerifierError(f"could not create branch {branch}: {made.stderr.strip()}")

    committed = _commit_changes(workspace, rebased, workspace)
    if run_tests:
        state, output = _run_pytest(workspace, test_path)
        culprits = _guess_culprits(output, committed) if state == FAILED else []
    else:
        state, output, culprits = SKIPPED, "", []

    return VerifyResult(
        workspace=workspace,
        branch=branch,
        isolated=True,
        committed=committed,
        state=state,
        pytest_output=output,
        culprits=culprits,
    )
