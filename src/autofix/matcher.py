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


class _ImportBindings(cst.CSTVisitor):
    """Collect the import bindings in a module so aliases resolve to real names.

    Produces two maps:

    * ``modules`` — a local name bound to a module path, covering ``import x`` and
      ``import x.y as z`` (so ``z`` -> ``x.y``, and ``sp`` -> ``subprocess`` for
      ``import subprocess as sp``).
    * ``names`` — a local name bound to a ``module.attr`` pair, covering
      ``from m import f`` and ``from m import f as g`` (so ``g`` -> ``("m", "f")``).

    These let the matcher see through aliases: ``import subprocess as sp`` makes
    ``sp.run(...)`` match a ``subprocess`` rule, and ``from hashlib import md5 as
    h`` makes ``h(...)`` match an ``md5`` rule — without matching an unrelated
    object that merely happens to share the method name.
    """

    def __init__(self) -> None:
        self.modules: dict[str, str] = {}
        self.names: dict[str, tuple[str, str]] = {}

    def visit_Import(self, node: cst.Import) -> None:
        for alias in node.names:
            dotted = _full_dotted_name(alias.name)
            if dotted is None:
                continue
            if alias.asname is not None and isinstance(alias.asname.name, cst.Name):
                self.modules[alias.asname.name.value] = dotted
            else:
                # ``import x.y`` binds the top-level name ``x`` locally.
                self.modules[dotted.split(".")[0]] = dotted.split(".")[0]

    def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
        if node.module is None or isinstance(node.names, cst.ImportStar):
            return
        module = _full_dotted_name(node.module)
        if module is None:
            return
        for alias in node.names:
            if not isinstance(alias.name, cst.Name):
                continue
            imported = alias.name.value
            local = (
                alias.asname.name.value
                if alias.asname is not None and isinstance(alias.asname.name, cst.Name)
                else imported
            )
            self.names[local] = (module, imported)


def collect_import_bindings(module: cst.Module) -> _ImportBindings:
    """Walk a parsed module and return its import bindings."""
    bindings = _ImportBindings()
    module.visit(bindings)
    return bindings


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


def _resolved_names(
    func: cst.BaseExpression, bindings: _ImportBindings
) -> tuple[str | None, str | None, str | None]:
    """Return ``(root, final, dotted)`` for a call's function, alias-resolved.

    ``root``/``final`` are the (possibly alias-resolved) first and last name of the
    call target; ``dotted`` is its full resolved dotted path when it is a plain
    name/attribute chain, else ``None``.

    Resolution rules:
    * ``sp.run`` with ``import subprocess as sp`` -> root ``subprocess``, dotted
      ``subprocess.run``.
    * ``h(...)`` with ``from hashlib import md5 as h`` -> root/final ``md5``,
      dotted ``hashlib.md5``.
    """

    raw_root, raw_final = _func_names(func)
    raw_dotted = _full_dotted_name(func)

    # ``from m import f as g`` -> a bare call g(...) resolves to m.f.
    if isinstance(func, cst.Name) and func.value in bindings.names:
        module, imported = bindings.names[func.value]
        return module.split(".")[0], imported, f"{module}.{imported}"

    # ``import m as alias`` (or ``import m``) -> alias.attr resolves to m.attr.
    if raw_root is not None and raw_root in bindings.modules:
        real_root = bindings.modules[raw_root]
        if raw_dotted is not None:
            resolved_dotted = real_root + raw_dotted[len(raw_root):]
            return real_root.split(".")[0], raw_final, resolved_dotted
        return real_root.split(".")[0], raw_final, None

    return raw_root, raw_final, raw_dotted


def call_matches(
    node: cst.Call, spec: MatchSpec, bindings: _ImportBindings | None = None
) -> bool:
    """Does one call node satisfy the match spec (name + positional/keyword args)?

    A dotted ``spec.call`` (``yaml.load``) matches the full attribute path exactly;
    a bare name (``md5``) matches either the root or final name. When ``bindings``
    is supplied, import aliases are resolved first, so ``sp.run`` matches a
    ``subprocess`` rule and ``from hashlib import md5 as h`` makes ``h(...)`` match.
    """

    bindings = bindings or _ImportBindings()
    root, final, dotted = _resolved_names(node.func, bindings)

    if "." in spec.call:
        if dotted != spec.call:
            return False
    else:
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
    """Collect matches for several rules in one pass over a parsed module.

    Walking the tree once and testing every rule per call node means each file is
    parsed a single time no matter how many rules are loaded (the previous design
    re-parsed every file once per rule).
    """

    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(
        self, rules: list[Rule], file_path: Path, bindings: _ImportBindings
    ) -> None:
        self.rules = rules
        self.file_path = file_path
        self.bindings = bindings
        self.matches: list[Match] = []

    def visit_Call(self, node: cst.Call) -> None:
        matched = [r for r in self.rules if call_matches(node, r.match, self.bindings)]
        if not matched:
            return
        pos = self.get_metadata(PositionProvider, node)
        snippet = cst.Module(body=[]).code_for_node(node)
        for rule in matched:
            self.matches.append(
                Match(
                    rule=rule,
                    file_path=self.file_path,
                    line=pos.start.line,
                    column=pos.start.column,
                    end_line=pos.end.line,
                    end_column=pos.end.column,
                    node=node,
                    snippet=snippet,
                )
            )


@dataclass
class ScanResult:
    """Everything one scan produced: matches plus files that could not be read."""

    matches: list[Match]
    unreadable: list[tuple[Path, str]]  # (path, reason) for files skipped


def scan(rules: list[Rule], files: list[Path]) -> ScanResult:
    """Find every match of every rule across ``files``, parsing each file once.

    Files that can't be read or parsed are not silently dropped: they are returned
    in ``unreadable`` with a reason, so the caller can surface incomplete coverage
    instead of pretending a broken file simply had no matches.
    """

    all_matches: list[Match] = []
    unreadable: list[tuple[Path, str]] = []
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            unreadable.append((path, f"could not read: {exc}"))
            continue
        try:
            module = cst.parse_module(source)
        except cst.ParserSyntaxError as exc:
            unreadable.append((path, f"could not parse: {exc}"))
            continue
        bindings = collect_import_bindings(module)
        wrapper = MetadataWrapper(module)
        collector = _MatchCollector(rules, path, bindings)
        wrapper.visit(collector)
        all_matches.extend(collector.matches)
    return ScanResult(matches=all_matches, unreadable=unreadable)


def find_matches(rule: Rule, files: list[Path]) -> list[Match]:
    """Find every instance of ``rule``'s pattern across ``files``.

    Thin wrapper over :func:`scan` for a single rule; unreadable files are skipped
    (use :func:`scan` directly to learn which files could not be processed).
    """

    return scan([rule], files).matches
