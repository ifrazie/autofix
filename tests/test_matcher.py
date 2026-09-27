from pathlib import Path

from autofix.matcher import find_matches
from autofix.rules import MatchSpec, Rule, load_rule
from autofix.target import python_files

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "sample-repo"


def _rule(call, keyword=None):
    return Rule(id="t", rationale="", match=MatchSpec(call=call, keyword=keyword or {}), transform_prompt="")


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


def test_seeded_sample_repo_counts_are_exact():
    files = python_files(SAMPLE)
    md5_rule = load_rule(ROOT / "rules" / "md5.yaml")
    shell_rule = load_rule(ROOT / "rules" / "shell.yaml")
    # Planted: auth, tokens, signatures, integrity, cache = 5 md5 calls.
    assert len(find_matches(md5_rule, files)) == 5
    # Planted: deploy, backup, tasks = 3 subprocess shell=True calls.
    assert len(find_matches(shell_rule, files)) == 3
