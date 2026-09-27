from app.tokens import make_session_token


def test_token_shape_and_stability():
    t1 = make_session_token("user-1", "s3cr3t")
    t2 = make_session_token("user-1", "s3cr3t")
    assert isinstance(t1, str) and len(t1) > 0
    assert t1 == t2


def test_token_varies_by_user():
    assert make_session_token("user-1", "s") != make_session_token("user-2", "s")
