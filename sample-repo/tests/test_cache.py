from app.cache import cache_key, get_or_set


def test_cache_key_shape():
    k = cache_key("GET", "/docs", "page=2")
    assert isinstance(k, str) and len(k) > 0


def test_get_or_set_memoizes():
    calls = []

    def factory():
        calls.append(1)
        return "value"

    a = get_or_set(("x", "y"), factory)
    b = get_or_set(("x", "y"), factory)
    assert a == b == "value"
    assert len(calls) == 1
