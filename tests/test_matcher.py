from pathlib import Path

from autofix.matcher import find_matches, scan
from autofix.rules import MatchSpec, Rule, load_rule
from autofix.target import python_files

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "sample-repo"


def _rule(call, keyword=None, args=()):
    return Rule(
        id="t",
        rationale="",
        match=MatchSpec(call=call, keyword=keyword or {}, args=tuple(args)),
        transform_prompt="",
    )


def _write_py(tmp_path, text):
    p = tmp_path / "m.py"
    p.write_text(text, encoding="utf-8")
    return [p]


def test_matches_bare_and_attribute_call(tmp_path):
    files = _write_py(tmp_path, "import hashlib\nfrom hashlib import md5\na = md5(b'x')\nb = hashlib.md5(b'y')\n")
    matches = find_matches(_rule("md5"), files)
    assert len(matches) == 2


def test_keyword_constraint_true(tmp_path):
    files = _write_py(
        tmp_path,
        "import subprocess\n"
        "subprocess.run('a', shell=True)\n"
        "subprocess.run('b', shell=False)\n"
        "subprocess.run('c')\n",
    )
    matches = find_matches(_rule("subprocess", {"shell": True}), files)
    assert len(matches) == 1
    assert matches[0].line == 2


def test_no_match_reports_zero(tmp_path):
    files = _write_py(tmp_path, "x = 1 + 2\n")
    assert find_matches(_rule("md5"), files) == []


def test_dotted_call_matches_full_path_only(tmp_path):
    """A dotted spec (``yaml.load``) matches that path exactly, not every ``.load``."""
    files = _write_py(
        tmp_path,
        "import yaml\n"
        "import json\n"
        "a = yaml.load(s)\n"       # matches
        "b = json.load(f)\n"       # different module, must not match
        "c = other.load(x)\n",     # same final name, different root, must not match
    )
    matches = find_matches(_rule("yaml.load"), files)
    assert len(matches) == 1
    assert matches[0].line == 3


def test_bare_call_still_matches_root_or_final(tmp_path):
    """A bare spec keeps the broad root-or-final behavior."""
    files = _write_py(tmp_path, "import yaml\na = yaml.load(s)\nb = load(s)\n")
    assert len(find_matches(_rule("load"), files)) == 2


def test_positional_arg_literal_constraint(tmp_path):
    files = _write_py(
        tmp_path,
        "f('md5', x)\n"     # matches: first positional == 'md5'
        "f('sha256', x)\n"  # different literal, no match
        "f(name, x)\n",     # non-literal first arg, no match
    )
    matches = find_matches(_rule("f", args=("md5",)), files)
    assert len(matches) == 1
    assert matches[0].line == 1


def test_positional_arg_none_means_any_present(tmp_path):
    """A ``None`` positional constraint requires a value there, of any kind."""
    files = _write_py(tmp_path, "g(anything)\ng('literal')\ng()\n")
    matches = find_matches(_rule("g", args=(None,)), files)
    # First two have a positional arg; the third (no args) does not.
    assert [m.line for m in matches] == [1, 2]


def test_seeded_sample_repo_counts_are_exact():
    files = python_files(SAMPLE)
    md5_rule = load_rule(ROOT / "rules" / "md5.yaml")
    shell_rule = load_rule(ROOT / "rules" / "shell.yaml")
    # Planted: auth, tokens, signatures, integrity, cache = 5 md5 calls.
    # (Matching is purely structural; whether each is fixed, migrated, or left
    # alone is the confident/skip decision made later, not here.)
    assert len(find_matches(md5_rule, files)) == 5
    # Planted: deploy, backup, tasks, pipeline = 4 subprocess shell=True calls
    # (pipeline uses a shell pipe and is matched but not auto-fixable).
    assert len(find_matches(shell_rule, files)) == 4


# --- Import/alias resolution & scan coverage (P1 item 6) ------------------- #


def test_module_alias_is_resolved(tmp_path):
    """`import subprocess as sp` makes sp.run(...) match a subprocess rule."""
    files = _write_py(
        tmp_path,
        "import subprocess as sp\nsp.run('x', shell=True)\n",
    )
    matches = find_matches(_rule("subprocess", {"shell": True}), files)
    assert len(matches) == 1


def test_from_import_alias_is_resolved(tmp_path):
    """`from hashlib import md5 as h` makes a bare h(...) match an md5 rule."""
    files = _write_py(tmp_path, "from hashlib import md5 as h\nx = h(b'a')\n")
    matches = find_matches(_rule("md5"), files)
    assert len(matches) == 1


def test_dotted_rule_matches_through_module_alias(tmp_path):
    """A dotted `yaml.load` rule matches `y.load(...)` when `import yaml as y`."""
    files = _write_py(tmp_path, "import yaml as y\na = y.load(s)\n")
    matches = find_matches(_rule("yaml.load"), files)
    assert len(matches) == 1


def test_unrelated_method_name_is_not_a_false_positive(tmp_path):
    """A dotted rule must not match a same-named method on an unrelated object."""
    files = _write_py(
        tmp_path,
        "import yaml\n"
        "class Store:\n"
        "    def load(self): ...\n"
        "s = Store()\n"
        "s.load()\n",      # Store.load, NOT yaml.load
    )
    matches = find_matches(_rule("yaml.load"), files)
    assert matches == []


def test_scan_reports_unparseable_file(tmp_path):
    good = tmp_path / "good.py"
    good.write_text("import hashlib\nx = hashlib.md5(b'a')\n", encoding="utf-8")
    bad = tmp_path / "bad.py"
    bad.write_text("def oops(:\n", encoding="utf-8")  # syntax error

    result = scan([_rule("md5")], [good, bad])
    assert len(result.matches) == 1
    assert len(result.unreadable) == 1
    assert result.unreadable[0][0] == bad
    assert "parse" in result.unreadable[0][1]


def test_scan_parses_each_file_once_for_many_rules(tmp_path, monkeypatch):
    import autofix.matcher as matcher

    p = _write_py(tmp_path, "import hashlib\nx = hashlib.md5(b'a')\n")
    calls = {"n": 0}
    real_parse = matcher.cst.parse_module

    def counting_parse(src):
        calls["n"] += 1
        return real_parse(src)

    monkeypatch.setattr(matcher.cst, "parse_module", counting_parse)
    rules = [_rule("md5"), _rule("sha256"), _rule("subprocess")]
    scan(rules, p)
    assert calls["n"] == 1  # one file -> parsed once regardless of rule count
