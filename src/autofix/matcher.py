"""Deterministic matcher.

Compiles a rule's ``match`` spec (the small pattern DSL) and walks every ``.py``
file in the target with libcst, collecting exact match sites. No AI is involved:
the pattern decides what counts, and the matched count is exact.

We use libcst (not stdlib ``ast``) so later edits preserve formatting and produce
minimal diffs — see spec.md > Stack.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import libcst as cst
from libcst.metadata import MetadataWrapper, PositionProvider

from .rules import MatchSpec, Rule


@dataclass
class Match:
    """A single located instance of a rule's pattern."""

    rule: Rule
    file_path: Path
    line: int  # 1-based start line of the matched call
    column: int  # 0-based start column
    end_line: int  # 1-based end line
    end_column: int  # 0-based end column
    node: cst.Call  # the matched call node
    snippet: str  # source text of the matched call

    @property
    def span(self) -> tuple[int, int, int, int]:
        """Full position span — uniquely identifies this node even when an outer
        call (e.g. ``.hexdigest()``) shares its start position."""
        return (self.line, self.column, self.end_line, self.end_column)


def _func_names(func: cst.BaseExpression) -> tuple[str | None, str | None]:
    """Return ``(root_name, final_name)`` of a call's function expression.

    ``md5``            -> ("md5", "md5")
    ``hashlib.md5``    -> ("hashlib", "md5")
    ``subprocess.run`` -> ("subprocess", "run")
    """

    if isinstance(func, cst.Name):
        return func.value, func.value
    if isinstance(func, cst.Attribute):
        final = func.attr.value
        root: cst.BaseExpression = func.value
        while isinstance(root, cst.Attribute):
            root = root.value
        root_name = root.value if isinstance(root, cst.Name) else None
        return root_name, final
    return None, None


def _full_dotted_name(func: cst.BaseExpression) -> str | None:
    """Return the full dotted path of a call's function, e.g. ``yaml.load``.

    Returns ``None`` for anything not made purely of names/attributes (e.g. a call
    on a subscript or another call), so a dotted ``call:`` spec matches exactly.
    """

    if isinstance(func, cst.Name):
        return func.value
    if isinstance(func, cst.Attribute):
        base = _full_dotted_name(func.value)
        return f"{base}.{func.attr.value}" if base is not None else None
    return None


def _literal_matches(node: cst.BaseExpression, expected: Any) -> bool:
    """Does a libcst literal node equal the expected YAML scalar?"""

    if isinstance(node, cst.Name):
        if node.value == "True":
            return expected is True
        if node.value == "False":
            return expected is False
        if node.value == "None":
            return expected is None
    if isinstance(node, (cst.SimpleString,)):
        return node.evaluated_value == expected
    if isinstance(node, cst.Integer):
        return int(node.value) == expected
    return False


def call_matches(node: cst.Call, spec: MatchSpec) -> bool:
    """Does one call node satisfy the match spec (name + positional/keyword args)?

    A dotted ``spec.call`` (``yaml.load``) matches the full attribute path exactly;
    a bare name (``md5``) matches either the root or final name.
    """

    if "." in spec.call:
        if _full_dotted_name(node.func) != spec.call:
            return False
    else:
        root, final = _func_names(node.func)
        if spec.call not in (root, final):
            return False

    if spec.args:
        positional = [a for a in node.args if a.keyword is None and a.star == ""]
        if len(positional) < len(spec.args):
            return False
        for constraint, arg in zip(spec.args, positional, strict=False):
            if constraint is None:  # None = "any value must be present here"
                continue
            if not _literal_matches(arg.value, constraint):
                return False

    for kw_name, kw_value in spec.keyword.items():
        found = False
        for arg in node.args:
            if arg.keyword is not None and arg.keyword.value == kw_name:
                if _literal_matches(arg.value, kw_value):
                    found = True
                break
        if not found:
            return False
    return True


class _MatchCollector(cst.CSTVisitor):
    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(self, rule: Rule, file_path: Path, source: str) -> None:
        self.rule = rule
        self.file_path = file_path
        self._source_lines = source.splitlines()
        self.matches: list[Match] = []

    def visit_Call(self, node: cst.Call) -> None:
        if not call_matches(node, self.rule.match):
            return
        pos = self.get_metadata(PositionProvider, node)
        snippet = cst.Module(body=[]).code_for_node(node)
        self.matches.append(
            Match(
                rule=self.rule,
                file_path=self.file_path,
                line=pos.start.line,
                column=pos.start.column,
                end_line=pos.end.line,
                end_column=pos.end.column,
                node=node,
                snippet=snippet,
            )
        )


def find_matches(rule: Rule, files: list[Path]) -> list[Match]:
    """Find every instance of ``rule``'s pattern across ``files``."""

    results: list[Match] = []
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
            module = cst.parse_module(source)
        except (UnicodeDecodeError, cst.ParserSyntaxError):
            continue  # skip files we can't read/parse; matching stays deterministic
        wrapper = MetadataWrapper(module)
        collector = _MatchCollector(rule, path, source)
        wrapper.visit(collector)
        results.extend(collector.matches)
    return results
