import textwrap

import pytest

from autofix.rules import RuleError, gather_rule_paths, load_rule


def _write(tmp_path, text):
    p = tmp_path / "rule.yaml"
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return p


def test_load_valid_rule(tmp_path):
    p = _write(
        tmp_path,
        """
        id: md5-to-sha256
        rationale: "md5 is broken"
        match:
          call: "md5"
        transform:
          prompt: "replace with sha256"
        """,
    )
    rule = load_rule(p)
    assert rule.id == "md5-to-sha256"
    assert rule.match.call == "md5"
    assert rule.match.keyword == {}
    assert "sha256" in rule.transform_prompt


def test_load_rule_with_keyword(tmp_path):
    p = _write(
        tmp_path,
        """
        id: shell
        rationale: "shell=True is unsafe"
        match:
          call: "subprocess"
          keyword: { shell: true }
        transform:
          prompt: "use shell=False"
        """,
    )
    rule = load_rule(p)
    assert rule.match.keyword == {"shell": True}


def test_missing_file(tmp_path):
    with pytest.raises(RuleError):
        load_rule(tmp_path / "nope.yaml")


def test_missing_required_key(tmp_path):
    p = _write(
        tmp_path,
        """
        id: broken
        match:
          call: "md5"
        transform:
          prompt: "x"
        """,
    )
    with pytest.raises(RuleError):
        load_rule(p)


def test_load_rule_with_positional_args(tmp_path):
    p = _write(
        tmp_path,
        """
        id: pos
        rationale: "first arg is a weak algo name"
        match:
          call: "new"
          args: ["md5", null]
        transform:
          prompt: "use sha256"
        """,
    )
    rule = load_rule(p)
    assert rule.match.args == ("md5", None)


def test_load_rule_with_ensure_import(tmp_path):
    p = _write(
        tmp_path,
        """
        id: rnd
        rationale: "random is not secure"
        match:
          call: "random"
        transform:
          prompt: "use secrets"
          ensure_import: "import secrets"
        """,
    )
    rule = load_rule(p)
    assert rule.ensure_imports == ("import secrets",)


def test_ensure_import_accepts_list(tmp_path):
    p = _write(
        tmp_path,
        """
        id: multi
        rationale: "needs two imports"
        match:
          call: "x"
        transform:
          prompt: "fix"
          ensure_import:
            - "import secrets"
            - "from hashlib import sha256"
        """,
    )
    rule = load_rule(p)
    assert rule.ensure_imports == ("import secrets", "from hashlib import sha256")


def test_invalid_ensure_import_is_rejected(tmp_path):
    p = _write(
        tmp_path,
        """
        id: bad
        rationale: "typo'd import"
        match:
          call: "x"
        transform:
          prompt: "fix"
          ensure_import: "improt secrets"
        """,
    )
    with pytest.raises(RuleError):
        load_rule(p)


def test_bad_args_type_is_rejected(tmp_path):
    p = _write(
        tmp_path,
        """
        id: bad
        rationale: "args must be a list"
        match:
          call: "x"
          args: "md5"
        transform:
          prompt: "fix"
        """,
    )
    with pytest.raises(RuleError):
        load_rule(p)


def test_gather_rule_paths_explicit_first_then_dir(tmp_path):
    (tmp_path / "b.yaml").write_text("", encoding="utf-8")
    (tmp_path / "a.yaml").write_text("", encoding="utf-8")
    (tmp_path / "ignore.txt").write_text("", encoding="utf-8")
    explicit = tmp_path / "z.yaml"
    explicit.write_text("", encoding="utf-8")

    paths = gather_rule_paths([str(explicit)], tmp_path)
    names = [p.name for p in paths]
    # Explicit path first (order preserved), then the directory's yaml in sorted order.
    assert names[0] == "z.yaml"
    assert names[1:] == ["a.yaml", "b.yaml", "z.yaml"]


def test_gather_rule_paths_requires_something(tmp_path):
    with pytest.raises(RuleError):
        gather_rule_paths([], None)


def test_gather_rule_paths_empty_dir_raises(tmp_path):
    with pytest.raises(RuleError):
        gather_rule_paths([], tmp_path)


def test_shipped_rules_load():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    md5 = load_rule(root / "rules" / "md5.yaml")
    shell = load_rule(root / "rules" / "shell.yaml")
    secrets = load_rule(root / "rules" / "secrets.yaml")
    assert md5.match.call == "md5"
    assert shell.match.keyword == {"shell": True}
    assert secrets.ensure_imports == ("import secrets",)
