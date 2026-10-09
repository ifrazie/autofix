"""Target loader.

Resolves the target argument the user passes to ``autofix``:

* a local path is used in place;
* a GitHub (or any ``http(s)``/``git``) URL is cloned into a temp directory.

Returns the working directory the rest of the pipeline operates on.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path


class TargetError(ValueError):
    """Raised when the target cannot be resolved."""


@dataclass
class Target:
    """A resolved target repository."""

    path: Path
    is_temporary: bool  # True when we cloned it and may clean it up


def _looks_like_url(target: str) -> bool:
    return target.startswith(("http://", "https://", "git@", "ssh://"))


def resolve_target(target: str) -> Target:
    """Resolve ``target`` to a local working directory.

    Local paths are validated and returned as-is. URLs are ``git clone``d into a
    fresh temp directory (public repos only; no auth in the PoC).
    """

    if _looks_like_url(target):
        dest = Path(tempfile.mkdtemp(prefix="autofix-clone-"))
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", target, str(dest)],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise TargetError(
                f"could not clone '{target}':\n{exc.stderr.strip()}"
            ) from exc
        return Target(path=dest, is_temporary=True)

    path = Path(target).expanduser().resolve()
    if not path.is_dir():
        raise TargetError(f"target is not a directory: {path}")
    return Target(path=path, is_temporary=False)


def python_files(
    root: Path,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[Path]:
    """Every ``.py`` file under ``root``, skipping virtualenvs and VCS dirs.

    ``include``/``exclude`` are optional lists of glob patterns, matched against
    each file's path relative to ``root`` (posix form) with :func:`fnmatch.fnmatch`
    semantics — note that ``*`` matches across ``/`` here, so ``app/*`` and
    ``app/**`` both mean "anything under ``app/``". When ``include`` is given, a
    file must match at least one include pattern to be scanned; any file matching
    an ``exclude`` pattern is dropped (exclude wins over include). This lets a run
    target just ``app/*`` or skip ``*/migrations/*`` without the engine hard-coding
    project layout.
    """

    skip = {".git", ".venv", "venv", "__pycache__", ".tox", "node_modules"}
    include = include or []
    exclude = exclude or []
    files: list[Path] = []
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root)
        if any(part in skip for part in rel.parts):
            continue
        rel_posix = rel.as_posix()
        if include and not any(fnmatch(rel_posix, pat) for pat in include):
            continue
        if any(fnmatch(rel_posix, pat) for pat in exclude):
            continue
        files.append(p)
    return files
