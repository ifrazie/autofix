"""Config file + settings precedence.

A real CLI shouldn't force every flag on every run. Defaults can come from a
project config file so ``autofix fix .`` just works, while an explicit flag still
wins for a one-off. Precedence, highest first:

    CLI flag  >  environment variable  >  config file  >  built-in default

Config is read from ``.autofix.toml`` (a top-level table) or, failing that, the
``[tool.autofix]`` table in ``pyproject.toml``, searched from the current
directory upward. Only a known set of keys is honored; unknown keys are ignored
so a typo never silently changes behavior in a surprising way.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")

# Settings that may be supplied by env/config (not just the CLI). Kept explicit so
# an unknown key in a config file is ignored rather than trusted.
CONFIG_KEYS = frozenset(
    {"provider", "model", "region", "host", "rules_dir", "concurrency", "timeout", "max_retries"}
)

_ENV_PREFIX = "AUTOFIX_"


def load_config(start: Path | None = None) -> dict[str, Any]:
    """Find and read the nearest config, returning only the recognized keys.

    Searches ``start`` (default: cwd) and its parents for ``.autofix.toml`` first,
    then a ``pyproject.toml`` with a ``[tool.autofix]`` table. Returns ``{}`` when
    no config is found. Malformed TOML raises ``tomllib.TOMLDecodeError``.
    """

    start = (start or Path.cwd()).resolve()
    for directory in (start, *start.parents):
        dedicated = directory / ".autofix.toml"
        if dedicated.is_file():
            return _filter(_read_toml(dedicated))
        pyproject = directory / "pyproject.toml"
        if pyproject.is_file():
            table = _read_toml(pyproject).get("tool", {}).get("autofix")
            if isinstance(table, dict):
                return _filter(table)
    return {}


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _filter(table: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in table.items() if k in CONFIG_KEYS}


def resolve(
    key: str,
    cli_value: T | None,
    config: dict[str, Any],
    default: T,
    cast: Callable[[Any], T] = lambda x: x,
) -> T:
    """Resolve one setting by precedence: CLI > env > config > default.

    A CLI option left at ``None`` counts as "not given"; that is what lets an
    unset flag fall through to the environment, then the config file, then the
    built-in default.
    """

    if cli_value is not None:
        return cli_value
    env = os.environ.get(_ENV_PREFIX + key.upper())
    if env is not None:
        return cast(env)
    if key in config:
        return cast(config[key])
    return default
