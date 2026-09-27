from app.utils import slugify, truncate


def test_slugify():
    assert slugify("Hello World Again") == "hello-world-again"


def test_truncate_short():
    assert truncate("short") == "short"


def test_truncate_long():
    out = truncate("x" * 100, limit=10)
    assert len(out) == 10
