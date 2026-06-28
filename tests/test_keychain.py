from acctsw.keychain import InMemoryKeychain


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
