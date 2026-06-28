import binascii

from acctsw.keychain import InMemoryKeychain, _encode, _decode


def test_encode_decode_roundtrip_multiline():
    blob = '{\n  "auth_mode": "chatgpt",\n  "tokens": {"access_token": "abc"}\n}\n'
    assert _decode(_encode(blob)) == blob
    assert "\n" not in _encode(blob)  # single-line → security won't hex-mangle it


def test_decode_legacy_hex():
    blob = '{\n "x": 1}\n'
    hexed = binascii.hexlify(blob.encode()).decode()  # how security returned multi-line data
    assert _decode(hexed) == blob


def test_decode_legacy_plain_json():
    # a compact JSON blob that is neither base64 nor hex must pass through unchanged
    blob = '{"claudeAiOauth":{"accessToken":"x"}}'
    assert _decode(blob) == blob


def test_set_get_delete_roundtrip():
    kc = InMemoryKeychain()
    assert kc.get("svc", "acct") is None
    kc.set("svc", "acct", "secret")
    assert kc.get("svc", "acct") == "secret"
    # -U semantics: set again updates
    kc.set("svc", "acct", "secret2")
    assert kc.get("svc", "acct") == "secret2"
    assert kc.delete("svc", "acct") is True
    assert kc.get("svc", "acct") is None
    assert kc.delete("svc", "acct") is False


def test_service_account_namespacing():
    kc = InMemoryKeychain()
    kc.set("svc", "a", "1")
    kc.set("svc", "b", "2")
    kc.set("other", "a", "3")
    assert kc.get("svc", "a") == "1"
    assert kc.get("svc", "b") == "2"
    assert kc.get("other", "a") == "3"
