from pathlib import Path

from autofix.matcher import find_matches
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
    assert len(find_matches(md5_rule, files)) == 5
    # Planted: deploy, backup, tasks = 3 subprocess shell=True calls.
    assert len(find_matches(shell_rule, files)) == 3
