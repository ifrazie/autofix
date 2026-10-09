"""Target discovery tests: include/exclude globbing and skip dirs."""

from autofix.target import python_files


def _tree(root):
    (root / "src" / "app").mkdir(parents=True)
    (root / "src" / "app" / "main.py").write_text("x = 1\n", encoding="utf-8")
    (root / "src" / "app" / "util.py").write_text("y = 2\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_main.py").write_text("z = 3\n", encoding="utf-8")
    (root / "migrations").mkdir()
    (root / "migrations" / "0001.py").write_text("w = 4\n", encoding="utf-8")
    # A virtualenv dir that must always be skipped.
    (root / ".venv").mkdir()
    (root / ".venv" / "lib.py").write_text("v = 5\n", encoding="utf-8")
    return root


def _names(paths, root):
    return sorted(p.relative_to(root).as_posix() for p in paths)


def test_default_lists_all_py_skipping_venv(tmp_path):
    _tree(tmp_path)
    names = _names(python_files(tmp_path), tmp_path)
    assert names == [
        "migrations/0001.py",
        "src/app/main.py",
        "src/app/util.py",
        "tests/test_main.py",
    ]


def test_include_restricts_to_matching_glob(tmp_path):
    _tree(tmp_path)
    names = _names(python_files(tmp_path, include=["src/**"]), tmp_path)
    assert names == ["src/app/main.py", "src/app/util.py"]


def test_exclude_drops_matching_glob(tmp_path):
    _tree(tmp_path)
    names = _names(python_files(tmp_path, exclude=["migrations/*", "tests/*"]), tmp_path)
    assert names == ["src/app/main.py", "src/app/util.py"]


def test_include_and_exclude_combine(tmp_path):
    _tree(tmp_path)
    names = _names(
        python_files(tmp_path, include=["src/**"], exclude=["**/util.py"]), tmp_path
    )
    assert names == ["src/app/main.py"]
