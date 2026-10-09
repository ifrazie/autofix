"""Verifier tests using a synthetic target repo (no model / provider needed)."""

import subprocess

from autofix.verifier import verify


def _make_target(tmp_path):
    """A minimal target: one module + a passing test that pins its return value."""
    (tmp_path / "conftest.py").write_text(
        "import os, sys\nsys.path.insert(0, os.path.dirname(__file__))\n", encoding="utf-8"
    )
    (tmp_path / "mod.py").write_text("def val():\n    return 1\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("", encoding="utf-8")
    (tests / "test_mod.py").write_text(
        "from mod import val\n\ndef test_val():\n    assert val() == 1\n", encoding="utf-8"
    )
    return tmp_path


def test_green_run_branches_commits_and_passes(tmp_path):
    target = _make_target(tmp_path)
    changed = {target / "mod.py": "def val():\n    return 1  # rewritten, same value\n"}

    vr = verify(target, changed, target)

    assert vr.isolated is True  # target has no .git -> temp copy
    assert vr.branch.startswith("autofix/")
    assert vr.committed == ["mod.py"]
    assert vr.passed is True
    assert vr.culprits == []
    # the fix really landed on the branch in the workspace
    assert "rewritten" in (vr.workspace / "mod.py").read_text(encoding="utf-8")


def test_red_run_names_culprit_and_keeps_branch(tmp_path):
    target = _make_target(tmp_path)
    changed = {target / "mod.py": "def val():\n    return 2\n"}  # breaks test_mod

    vr = verify(target, changed, target)

    assert vr.passed is False
    assert "mod.py" in vr.culprits  # test_mod.py -> mod.py by stem
    # branch is preserved for inspection
    branches = subprocess.run(
        ["git", "branch", "--list", vr.branch], cwd=vr.workspace, capture_output=True, text=True
    ).stdout
    assert vr.branch in branches


def test_in_place_repo_uses_worktree_and_never_touches_checkout(tmp_path):
    target = _make_target(tmp_path)
    subprocess.run(["git", "init"], cwd=target, capture_output=True, text=True)
    subprocess.run(["git", "add", "-A"], cwd=target, capture_output=True, text=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "base"],
        cwd=target, capture_output=True, text=True,
    )
    base = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=target, capture_output=True, text=True
    ).stdout.strip()
    # An unrelated, uncommitted edit the user has in flight.
    (target / "mod.py").write_text("def val():\n    return 1  # local WIP\n", encoding="utf-8")

    vr = verify(target, {target / "mod.py": "def val():\n    return 1  # autofix\n"}, target)

    assert vr.isolated is False and vr.workspace == target
    now = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=target, capture_output=True, text=True
    ).stdout.strip()
    assert now == base  # the user is still on their branch
    # The user's uncommitted edit is untouched (the fix landed only on the branch).
    assert "local WIP" in (target / "mod.py").read_text(encoding="utf-8")
    branches = subprocess.run(
        ["git", "branch"], cwd=target, capture_output=True, text=True
    ).stdout
    assert vr.branch in branches  # autofix branch left intact
    # No throwaway worktree is left registered.
    worktrees = subprocess.run(
        ["git", "worktree", "list"], cwd=target, capture_output=True, text=True
    ).stdout
    assert "autofix-wt-" not in worktrees


def test_in_place_commit_contains_only_autofix_edits(tmp_path):
    """An unrelated staged change must NOT ride along in the autofix commit."""
    target = _make_target(tmp_path)
    subprocess.run(["git", "init"], cwd=target, capture_output=True, text=True)
    subprocess.run(["git", "add", "-A"], cwd=target, capture_output=True, text=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "base"],
        cwd=target, capture_output=True, text=True,
    )
    # A second file the user has staged but that Autofix never touches.
    (target / "other.py").write_text("UNRELATED = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "other.py"], cwd=target, capture_output=True, text=True)

    vr = verify(target, {target / "mod.py": "def val():\n    return 1  # autofix\n"}, target)

    # The autofix commit on its branch touched only mod.py.
    files = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", vr.branch],
        cwd=target, capture_output=True, text=True,
    ).stdout.split()
    assert files == ["mod.py"]
