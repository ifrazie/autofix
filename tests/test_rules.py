import textwrap

import pytest

from autofix.rules import RuleError, load_rule


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


def test_shipped_rules_load():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    md5 = load_rule(root / "rules" / "md5.yaml")
    shell = load_rule(root / "rules" / "shell.yaml")
    assert md5.match.call == "md5"
    assert shell.match.keyword == {"shell": True}
