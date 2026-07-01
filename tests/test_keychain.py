import binascii

from acctsw import keychain as kc_mod
from acctsw.keychain import InMemoryKeychain, SecurityKeychain, _encode, _decode


def test_security_keychain_set_passes_secret_inline_not_stdin(monkeypatch):
    """The OAuth blob must be passed inline via `-w <value>`, NOT over stdin. `security`'s stdin
    prompt reads with readpassphrase(), which silently truncates the blob to 128 bytes — corrupting
    every real (larger-than-128-byte) credential. Inline has no length cap. The `ps` exposure it
    trades for is moot: a same-user process that can read our argv can just read the item directly."""
    captured = {}

    class _R:
        returncode = 0
        stderr = ""

    def fake_run(argv, input=None, capture_output=False, text=False):
        captured["argv"], captured["input"] = argv, input
        return _R()

    monkeypatch.setattr(kc_mod.subprocess, "run", fake_run)
    SecurityKeychain().set("svc", "acct", "my-oauth-secret")
    enc = _encode("my-oauth-secret")
    assert captured["input"] is None                     # nothing fed over stdin
    assert captured["argv"][-2:] == ["-w", enc]          # -w carries the (encoded) value inline


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
