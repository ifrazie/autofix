"""Plan build/load/drift tests — pure, no model or git needed."""

import pytest

from autofix.matcher import find_matches
from autofix.plan import (
    PLAN_SCHEMA,
    PlanError,
    build_plan,
    load_plan,
    resolve_plan_patch,
    source_hash,
    write_plan,
)
from autofix.rules import MatchSpec, Rule
from autofix.transformer import FixResult


def _rule(call):
    return Rule(id="md5", rationale="r", match=MatchSpec(call=call), transform_prompt="x")


def _target(tmp_path):
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    p = root / "app" / "mod.py"
    p.write_text("import hashlib\nx = hashlib.md5(b'a')\n", encoding="utf-8")
    return root, p


def _plan_for(root, p):
    (m,) = find_matches(_rule("md5"), [p])
    new_source = p.read_text(encoding="utf-8").replace("md5", "sha256")
    fr = FixResult(m, "fixed", "integrity use", replacement_code="hashlib.sha256(b'a')")
    return build_plan([fr], {p: new_source}, root, provider="bedrock", model="test-model")


def test_build_plan_records_hash_metadata_and_fixes(tmp_path):
    root, p = _target(tmp_path)
    plan = _plan_for(root, p)

    assert plan["schema"] == PLAN_SCHEMA
    assert plan["provider"] == "bedrock" and plan["model"] == "test-model"
    (entry,) = plan["files"]
    assert entry["path"] == "app/mod.py"
    assert entry["original_sha"] == source_hash(p.read_text(encoding="utf-8"))
    assert "sha256" in entry["new_source"]
    assert entry["fixes"][0]["rule"] == "md5"


def test_write_then_load_roundtrips(tmp_path):
    root, p = _target(tmp_path)
    plan = _plan_for(root, p)
    out = tmp_path / "plan.json"
    write_plan(plan, out)
    loaded = load_plan(out)
    assert loaded["files"][0]["path"] == "app/mod.py"


def test_resolve_patch_when_source_unchanged(tmp_path):
    root, p = _target(tmp_path)
    plan = _plan_for(root, p)
    patch, stale = resolve_plan_patch(plan, root)
    assert list(patch) == [p]
    assert stale == []


def test_resolve_patch_detects_drift(tmp_path):
    root, p = _target(tmp_path)
    plan = _plan_for(root, p)
    p.write_text(p.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")

    patch, stale = resolve_plan_patch(plan, root)
    assert patch == {}
    assert len(stale) == 1
    assert "changed" in stale[0].reason


def test_resolve_patch_detects_deleted_file(tmp_path):
    root, p = _target(tmp_path)
    plan = _plan_for(root, p)
    p.unlink()

    patch, stale = resolve_plan_patch(plan, root)
    assert patch == {}
    assert "no longer exists" in stale[0].reason


def test_load_rejects_non_plan_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema": "something-else"}', encoding="utf-8")
    with pytest.raises(PlanError):
        load_plan(bad)


def test_load_rejects_missing_file(tmp_path):
    with pytest.raises(PlanError):
        load_plan(tmp_path / "nope.json")
