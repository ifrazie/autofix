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

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class RuleError(ValueError):
    """Raised when a rule file is missing, malformed, or invalid."""


@dataclass(frozen=True)
class MatchSpec:
    """The small pattern DSL: what counts as a match.

    ``call`` is the function/attribute name of a call (e.g. ``md5`` matches both
    ``md5(...)`` and ``hashlib.md5(...)``). ``keyword`` is an optional map of
    keyword-argument name to required literal value (e.g. ``{shell: True}``).
    """

    call: str
    keyword: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Rule:
    """A validated rule loaded from a YAML file."""

    id: str
    rationale: str
    match: MatchSpec
    transform_prompt: str


def _require(mapping: dict, key: str, source: Path, kind: type = str) -> Any:
    if key not in mapping:
        raise RuleError(f"{source}: missing required key '{key}'")
    value = mapping[key]
    if not isinstance(value, kind) or (kind is str and not value.strip()):
        raise RuleError(f"{source}: key '{key}' must be a non-empty {kind.__name__}")
    return value


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

    transform_block = _require(raw, "transform", source, kind=dict)
    prompt = _require(transform_block, "prompt", source)

    return Rule(
        id=rule_id,
        rationale=rationale,
        match=MatchSpec(call=call, keyword=keyword),
        transform_prompt=prompt,
    )


def load_rules(paths: list[str | Path]) -> list[Rule]:
    """Load several rule files, preserving order."""

    return [load_rule(p) for p in paths]
