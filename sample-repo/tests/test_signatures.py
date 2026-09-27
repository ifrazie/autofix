from app.signatures import sign_request, verify_signature


def test_signature_roundtrip():
    sig = sign_request(b"payload", "secret")
    assert isinstance(sig, str) and len(sig) > 0
    assert verify_signature(b"payload", "secret", sig)
    assert not verify_signature(b"tampered", "secret", sig)
