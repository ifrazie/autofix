"""Rule loader + rule format.

A rule is plain data. It never lives in the engine as code. Adding a new
vulnerability class means writing a new YAML file, not touching Python. That is
the whole idea of the project.

Schema (see spec.md > Rule Loader + Rule Format)::

    id: md5-to-sha256
    rationale: "md5 is cryptographically broken; use sha256 for hashing."
    match:
      call: "md5"                # function/attribute call name to match
      keyword: { shell: true }   # optional keyword-argument constraints
    transform:
      prompt: >
        Replace this md5() hashing call with sha256() ...
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class RuleError(ValueError):
    """Raised when a rule file is missing, malformed, or invalid."""


@dataclass(frozen=True)
class MatchSpec:
    """The small pattern DSL: what counts as a match.

    * ``call`` is the function/attribute name of a call. A bare name (``md5``)
      matches both ``md5(...)`` and ``hashlib.md5(...)`` (root or final name); a
      dotted name (``yaml.load``) matches the full attribute path exactly, so it
      won't over-match every ``.load()``.
    * ``keyword`` is an optional map of keyword-argument name to required literal
      value (e.g. ``{shell: True}``).
    * ``args`` is an optional list of positional-argument literal constraints,
      matched by position; a ``None`` entry means "any value must be present at
      this position". The call must have at least ``len(args)`` positional args.
    """

    call: str
    keyword: dict[str, Any] = field(default_factory=dict)
    args: tuple[Any, ...] = ()


@dataclass(frozen=True)
class Rule:
    """A validated rule loaded from a YAML file."""

    id: str
    rationale: str
    match: MatchSpec
    transform_prompt: str
    # Imports to ensure exist in a file whenever this rule applies a fix, e.g.
    # ("import secrets",) or ("from hashlib import sha256",). Added iff missing.
    ensure_imports: tuple[str, ...] = ()


def _require(mapping: dict[str, Any], key: str, source: Path, kind: type = str) -> Any:
    if key not in mapping:
        raise RuleError(f"{source}: missing required key '{key}'")
    value = mapping[key]
    if not isinstance(value, kind):
        raise RuleError(f"{source}: key '{key}' must be a non-empty {kind.__name__}")
    if isinstance(value, str) and not value.strip():
        raise RuleError(f"{source}: key '{key}' must be a non-empty {kind.__name__}")
    return value


def _normalize_imports(value: Any, source: Path) -> tuple[str, ...]:
    """Validate and normalize ``transform.ensure_import`` to a tuple of specs.

    Accepts a single string or a list of strings; each must be a plain ``import X``
    or ``from X import Y`` statement (this is validated so a typo fails at load
    time, not mid-fix).
    """

    if value is None:
        return ()
    specs = [value] if isinstance(value, str) else value
    if not isinstance(specs, list) or not all(isinstance(s, str) for s in specs):
        raise RuleError(f"{source}: transform.ensure_import must be a string or list of strings")

    out: list[str] = []
    for spec in specs:
        text = spec.strip()
        words = text.split()
        ok = (words[:1] == ["import"] and len(words) >= 2) or (
            words[:1] == ["from"] and len(words) >= 4 and words[2] == "import"
        )
        if not ok:
            raise RuleError(
                f"{source}: invalid ensure_import '{spec}' "
                "(expected 'import X' or 'from X import Y')"
            )
        out.append(text)
    return tuple(out)


def load_rule(path: str | Path) -> Rule:
    """Read one rule file, validate it, and return a :class:`Rule`."""

    source = Path(path)
    if not source.is_file():
        raise RuleError(f"rule file not found: {source}")

    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # malformed YAML
        raise RuleError(f"{source}: could not parse YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise RuleError(f"{source}: rule file must be a YAML mapping")

    rule_id = _require(raw, "id", source)
    rationale = _require(raw, "rationale", source)

    match_block = _require(raw, "match", source, kind=dict)
    call = _require(match_block, "call", source)
    keyword = match_block.get("keyword", {}) or {}
    if not isinstance(keyword, dict):
        raise RuleError(f"{source}: match.keyword must be a mapping")
    args = match_block.get("args", []) or []
    if not isinstance(args, list):
        raise RuleError(f"{source}: match.args must be a list")

    transform_block = _require(raw, "transform", source, kind=dict)
    prompt = _require(transform_block, "prompt", source)
    ensure_imports = _normalize_imports(transform_block.get("ensure_import"), source)

    return Rule(
        id=rule_id,
        rationale=rationale,
        match=MatchSpec(call=call, keyword=keyword, args=tuple(args)),
        transform_prompt=prompt,
        ensure_imports=ensure_imports,
    )


def load_rules(paths: Sequence[str | Path]) -> list[Rule]:
    """Load several rule files, preserving order."""

    return [load_rule(p) for p in paths]


def gather_rule_paths(
    rule_paths: list[str | Path] | None = None, rules_dir: str | Path | None = None
) -> list[Path]:
    """Collect rule file paths from explicit ``--rule`` args and/or a ``--rules-dir``.

    Explicit paths come first (order preserved); a directory contributes every
    ``*.yaml``/``*.yml`` file in sorted order. Raises if nothing is found.
    """

    paths: list[Path] = [Path(p) for p in (rule_paths or [])]
    if rules_dir is not None:
        directory = Path(rules_dir)
        if not directory.is_dir():
            raise RuleError(f"rules directory not found: {directory}")
        found = sorted(
            p for p in directory.iterdir() if p.suffix in (".yaml", ".yml") and p.is_file()
        )
        if not found:
            raise RuleError(f"no .yaml/.yml rule files in {directory}")
        paths.extend(found)
    if not paths:
        raise RuleError("no rules given (use --rule and/or --rules-dir)")
    return paths
