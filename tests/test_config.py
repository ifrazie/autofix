"""Config discovery and CLI > env > config > default precedence."""

import textwrap

from autofix.config import load_config, resolve


def test_load_dedicated_autofix_toml(tmp_path):
    (tmp_path / ".autofix.toml").write_text(
        textwrap.dedent(
            """
            provider = "ollama"
            concurrency = 8
            unknown_key = "ignored"
            """
        ),
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg["provider"] == "ollama"
    assert cfg["concurrency"] == 8
    assert "unknown_key" not in cfg  # only recognized keys survive


def test_load_pyproject_tool_table(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent(
            """
            [tool.autofix]
            provider = "ollama"
            region = "eu-west-1"
            """
        ),
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg == {"provider": "ollama", "region": "eu-west-1"}


def test_dedicated_file_wins_over_pyproject(tmp_path):
    (tmp_path / ".autofix.toml").write_text('provider = "ollama"\n', encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[tool.autofix]\nprovider = \"bedrock\"\n", encoding="utf-8"
    )
    assert load_config(tmp_path)["provider"] == "ollama"


def test_no_config_returns_empty(tmp_path):
    assert load_config(tmp_path) == {}


def test_config_found_in_parent_dir(tmp_path):
    (tmp_path / ".autofix.toml").write_text('provider = "ollama"\n', encoding="utf-8")
    child = tmp_path / "a" / "b"
    child.mkdir(parents=True)
    assert load_config(child)["provider"] == "ollama"


def test_resolve_cli_wins(monkeypatch):
    monkeypatch.setenv("AUTOFIX_PROVIDER", "env")
    assert resolve("provider", "cli", {"provider": "cfg"}, "default") == "cli"


def test_resolve_env_beats_config(monkeypatch):
    monkeypatch.setenv("AUTOFIX_PROVIDER", "env")
    assert resolve("provider", None, {"provider": "cfg"}, "default") == "env"


def test_resolve_config_beats_default(monkeypatch):
    monkeypatch.delenv("AUTOFIX_PROVIDER", raising=False)
    assert resolve("provider", None, {"provider": "cfg"}, "default") == "cfg"


def test_resolve_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("AUTOFIX_PROVIDER", raising=False)
    assert resolve("provider", None, {}, "default") == "default"


def test_resolve_casts_env_and_config(monkeypatch):
    monkeypatch.setenv("AUTOFIX_CONCURRENCY", "7")
    assert resolve("concurrency", None, {}, 4, int) == 7
    monkeypatch.delenv("AUTOFIX_CONCURRENCY", raising=False)
    assert resolve("concurrency", None, {"concurrency": "9"}, 4, int) == 9
