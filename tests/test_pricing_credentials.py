"""Adversarial catalog echoes: real callers/parsers/cache, fake HTTP and Keychain only."""
import io
import json
import socket
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from acctsw import bridge, cli, keyseats, launcher, pricing
from acctsw.providers import get_provider

SECRET = "sk-LEAKFIX-MARKER-0000"
NOW = datetime(2026, 9, 25, tzinfo=timezone.utc)
ENCODINGS = ("literal", "unicode", "partial")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def forbidden(*args, **kwargs):
    pytest.fail("unexpected network, subscription probe, or child launch")


def echo_body(encoding, target):
    row = {"id": "chosen-model", "name": "Ordinary name"}
    row[target] = f"echo-{SECRET}-tail"
    rows = [row, {"id": "untouched-model", "name": "Untouched name"}]
    body = json.dumps({"data": rows})
    if encoding != "literal":
        encoded = "".join(f"\\u{ord(c):04x}" if encoding == "unicode" or i % 2 == 0
                          else c for i, c in enumerate(SECRET))
        body = body.replace(SECRET, encoded)
        assert SECRET not in body  # A raw-body replace cannot match this response.
    assert SECRET in json.loads(body)["data"][0][target]
    return body


def assert_clean(ctx, capsys):
    output = capsys.readouterr()
    assert SECRET not in output.out, "credential in stdout"
    assert SECRET not in output.err, "credential in stderr"
    # Include state, backups, homes, sessions and leftover temporary files, not just pricing.json.
    for path in ctx.data_dir.parent.rglob("*"):
        assert SECRET not in str(path.relative_to(ctx.data_dir.parent))
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes(), path
            if path.suffix == ".json":
                assert SECRET not in json.dumps(json.loads(path.read_text())), path
    return output


def assert_models(models, target):
    assert len(models) == 2  # Redaction must not silently discard a selected model.
    first, untouched = models
    assert first["id"] == ("echo-[redacted]-tail" if target == "id" else "chosen-model")
    assert first["display_name"] == ("echo-[redacted]-tail" if target == "name"
                                     else "Ordinary name")
    assert untouched["id"] == "untouched-model"
    assert untouched["display_name"] == "Untouched name"


@pytest.mark.parametrize("encoding", ENCODINGS)
@pytest.mark.parametrize("target", ["id", "name"])
@pytest.mark.parametrize("caller", ["cli-json", "cli-human", "bridge", "launcher"])
def test_catalog_echo_through_real_callers(ctx, monkeypatch, capsys, encoding, target, caller):
    body = echo_body(encoding, target)
    requests = []

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == get_provider("openai").models_endpoint
            assert request.get_header("Authorization") == "Bearer " + SECRET
            requests.append(request)
            return Response(body.encode())

    # Exercise _default_get itself, including the launcher's bound default price_get. Replacing
    # pricing._default_get would only intercept the bridge and miss that default argument.
    monkeypatch.setattr(pricing.urllib.request, "build_opener", lambda *_: Opener())
    if caller.startswith("cli"):
        monkeypatch.setattr(cli.Context, "default", classmethod(lambda cls: ctx))
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SECRET + "\n"))
        args = ["keys", "models", "openai"] + (["--json"] if caller == "cli-json" else [])
        assert cli.main(args) == cli.EXIT_OK
    elif caller == "bridge":
        result = bridge.handle(ctx, {"action": "models_list", "provider": "openai",
                                     "secret": SECRET})
        assert result["ok"] and len(result["models"]) == 2
        assert SECRET not in json.dumps(result)
    else:
        # The display-name case keeps the exact selected id. The id case echoes on an extra
        # catalog model: seat metadata intentionally cannot contain the credential itself.
        selected = "chosen-model" if target == "name" else "untouched-model"
        seat = keyseats.add(ctx, get_provider("openai"), SECRET, label="Test key",
                            model=selected, get=lambda *_: (200, "{}"))
        state = ctx.load_state()
        state.set_setting("key_fallback", True)
        state.set_setting("confirm_key_switch", True)
        state.save()
        confirmations = []

        def decline(record):
            confirmations.append(record)
            assert record["key_seat"]["model"] == selected
            assert SECRET not in json.dumps(record)
            assert (ctx.data_dir / "pricing.json").exists()
            return False

        assert launcher.run(ctx, "codex", [], key=seat["id"], confirm=decline,
                            spawn=forbidden, get=forbidden, notify=print) == launcher.EXIT_GAVE_UP
        assert len(confirmations) == 1

    assert len(requests) == 1
    output = assert_clean(ctx, capsys)
    if caller == "cli-json":
        result = json.loads(output.out)
        assert result["ok"] and len(result["models"]) == 2
    elif caller == "cli-human":
        assert "untouched-model" in output.out
    cache = json.loads((ctx.data_dir / "pricing.json").read_text())
    entry, = cache.values()
    assert_models(entry["models"], target)


@pytest.mark.parametrize("encoding", ENCODINGS)
@pytest.mark.parametrize("target", ["id", "name"])
def test_fetch_catalog_redacts_before_return_and_cache(ctx, capsys, encoding, target):
    catalog = pricing.fetch_catalog(get_provider("openai"), SECRET,
                                    get=lambda *_: (200, echo_body(encoding, target)),
                                    cache_path=ctx.data_dir / "pricing.json", at=NOW)
    assert catalog.source == "live"
    assert SECRET not in repr(catalog)
    assert_models([asdict(m) for m in catalog.models], target)
    assert_clean(ctx, capsys)


def poison_cache(ctx, location):
    path = ctx.data_dir / "pricing.json"
    pricing.fetch_catalog(get_provider("openai"), SECRET,
                          get=lambda *_: (200, '{"data":[{"id":"chosen-model"}]}'),
                          cache_path=path, at=NOW)
    cache = json.loads(path.read_text())
    entry, = cache.values()
    model, = entry["models"]
    if location in ("rates", "long_context_rates"):
        # Both a nested mapping key and a string inside a Rate; no provider adapter currently
        # populates these with arbitrary strings, but cache loading and future adapters can.
        model[location][f"dimension-{SECRET}"] = {
            "status": "unknown", "value": None, "same_as": f"alias-{SECRET}"}
    else:
        model[location] = f"prefix-{SECRET}-suffix"
    path.write_text(json.dumps(cache))
    assert SECRET in path.read_text()
    return path, cache


@pytest.mark.parametrize("location", ["id", "display_name", "currency", "provider", "source_url",
                                      "verified_at", "rates", "long_context_rates"])
@pytest.mark.parametrize("age", [timedelta(hours=1), timedelta(hours=25)], ids=["fresh", "stale"])
def test_existing_poisoned_cache_is_repaired(ctx, capsys, location, age):
    path, before = poison_cache(ctx, location)
    calls = []

    def unavailable(*_):
        calls.append(True)
        return 503, ""

    catalog = pricing.fetch_catalog(get_provider("openai"), SECRET, get=unavailable,
                                    cache_path=path, at=NOW + age)
    assert catalog.source == "cache" and len(catalog.models) == 1
    assert len(calls) == int(age >= pricing.CACHE_TTL)
    assert catalog.error == ("http_503" if calls else None)
    assert SECRET not in repr(catalog)
    after = json.loads(path.read_text())
    assert after == json.loads(json.dumps(before).replace(SECRET, "[redacted]"))
    assert_clean(ctx, capsys)
    # Persisted repairs must survive a second reader, with no change to cache identity or format.
    reread = pricing.fetch_catalog(get_provider("openai"), SECRET, get=forbidden,
                                   cache_path=path, at=NOW + timedelta(hours=1))
    assert reread.models == catalog.models


def test_poisoned_cache_is_purged_if_atomic_repair_fails(ctx, monkeypatch, capsys):
    path, _ = poison_cache(ctx, "display_name")

    def denied(*_):
        raise OSError("replacement denied")

    monkeypatch.setattr(pricing.os, "replace", denied)
    catalog = pricing.fetch_catalog(get_provider("openai"), SECRET, get=forbidden,
                                    cache_path=path, at=NOW)
    assert len(catalog.models) == 1 and SECRET not in repr(catalog)
    assert not path.exists()
    assert_clean(ctx, capsys)


def test_recursive_redaction_preserves_types_prices_and_future_fields(ctx, monkeypatch, capsys):
    @dataclass(frozen=True)
    class FutureModel(pricing.Model):
        metadata: dict = field(default_factory=dict)

    model = FutureModel("openai", "chosen-model", "ordinary", "responses",
                        currency=f"currency-{SECRET}",
                        rates={f"dimension-{SECRET}": pricing.Rate(same_as=f"alias-{SECRET}"),
                               "input": pricing.Rate("known", Decimal("0.1234567890123456789"))},
                        long_context_rates={"output": pricing.Rate(same_as=SECRET)},
                        metadata={SECRET: [({"nested": SECRET},), "ordinary", 42, None]})
    monkeypatch.setattr(pricing, "parse_catalog", lambda *a, **kw: [model])
    catalog = pricing.fetch_catalog(get_provider("openai"), SECRET, get=lambda *_: (200, "{}"),
                                    cache_path=ctx.data_dir / "pricing.json", at=NOW)
    clean, = catalog.models
    assert isinstance(clean, FutureModel)
    assert SECRET not in repr(clean)
    assert clean.currency == "currency-[redacted]"
    assert clean.rates["dimension-[redacted]"].same_as == "alias-[redacted]"
    assert clean.long_context_rates["output"].same_as == "[redacted]"
    assert clean.metadata == {"[redacted]": [({"nested": "[redacted]"},), "ordinary", 42, None]}
    assert clean.rate("input") == Decimal("0.1234567890123456789")
    assert SECRET in repr(model)  # No mutation of the parser's frozen objects/nested containers.
    assert_clean(ctx, capsys)


@pytest.mark.parametrize("key", ["", "m", "id", "USD", "name"])
def test_empty_and_tiny_keys_leave_legitimate_catalog_unchanged(ctx, capsys, key):
    body = '{"data":[{"id":"model-id","name":"USD model name"}]}'
    expected = pricing.parse_catalog(get_provider("openai"), body, verified_at=NOW.isoformat())
    path = ctx.data_dir / "pricing.json"
    catalog = pricing.fetch_catalog(get_provider("openai"), key, get=lambda *_: (200, body),
                                    cache_path=path, at=NOW)
    before = path.read_bytes()
    cached = pricing.fetch_catalog(get_provider("openai"), key, get=forbidden,
                                   cache_path=path, at=NOW)
    assert catalog.models == cached.models == expected
    assert path.read_bytes() == before
    assert_clean(ctx, capsys)


@pytest.mark.parametrize("key", ["[redacted]", "sk-[redacted]"])
def test_redaction_marker_cannot_recreate_the_key(ctx, capsys, key):
    body = json.dumps({"data": [{"id": "chosen-model", "name": "sk-" + key}]})
    path = ctx.data_dir / "pricing.json"
    catalog = pricing.fetch_catalog(get_provider("openai"), key, get=lambda *_: (200, body),
                                    cache_path=path, at=NOW)
    assert len(catalog.models) == 1
    assert key not in repr(catalog)
    assert key not in path.read_text()
    assert_clean(ctx, capsys)


FRAGMENT_KEYS = ("sk-proj-" + "A" * 40 + "-TAILPART-9999",
                 "sk-proj-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
CATALOG_CALLERS = ("fetch", "cli-json", "cli-human", "bridge")


def fragment_rows(key):
    # Keep the preservation controls in the same catalog as the attacks: each regression test
    # must fail without the fix, while also rejecting an overbroad or whole-catalog redaction.
    cases = [
        ("full", key, "[redacted]"),
        ("minus-first", key[1:], "[redacted]"),
        ("suffix-40", key[-40:], "[redacted]"),
        ("suffix-16", key[-16:], "[redacted]"),
        ("middle", key[12:36], "[redacted]"),
        ("exactly-16", key[8:24], "[redacted]"),
        ("only-15", key[8:23], key[8:23]),
        ("separate", key[1:] + "|" + key[-40:], "[redacted]|[redacted]"),
        ("ordinary", "ordinary-model", "ordinary-model"),
    ]
    rows = [{"id": f"{label}:{echo}:end", "name": f"name:{echo}:end"}
            for label, echo, _ in cases]
    expected = [(f"{label}:{clean}:end", f"name:{clean}:end")
                for label, _, clean in cases]
    return rows, expected


def run_fragment_caller(ctx, monkeypatch, capsys, caller, key, body):
    def get(*_):
        return 200, body

    monkeypatch.setattr(pricing, "_default_get", get)
    if caller == "fetch":
        catalog = pricing.fetch_catalog(get_provider("openai"), key, get=get,
                                        cache_path=ctx.data_dir / "pricing.json", at=NOW)
        assert catalog.source == "live"
        models = [asdict(model) for model in catalog.models]
    elif caller == "bridge":
        result = bridge.handle(ctx, {"action": "models_list", "provider": "openai", "secret": key})
        assert result["ok"]
        models = result["models"]
    else:
        monkeypatch.setattr(cli.Context, "default", classmethod(lambda cls: ctx))
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO(key + "\n"))
        args = ["keys", "models", "openai"] + (["--json"] if caller == "cli-json" else [])
        assert cli.main(args) == cli.EXIT_OK
        models = None
    output = capsys.readouterr()
    assert output.err == ""  # Check stderr independently of the serialized/model output.
    if caller == "cli-json":
        models = json.loads(output.out)["models"]
    elif caller in ("fetch", "bridge"):
        assert output.out == ""
    return models, output.out


def assert_fragment_models(models, output, expected):
    if models is not None:
        assert sorted((m["id"], m["display_name"]) for m in models) == sorted(expected)
    else:
        for id, name in expected:
            assert f"  {id} — {name} — " in output
        assert len(output.splitlines()) == len(expected) + 1


@pytest.mark.parametrize("key", FRAGMENT_KEYS, ids=["repeated", "distinct"])
@pytest.mark.parametrize("encoding", ["literal", "unicode"])
@pytest.mark.parametrize("caller", CATALOG_CALLERS)
def test_partial_echo_catalog(ctx, monkeypatch, capsys, key, encoding, caller):
    path = ctx.data_dir / "pricing.json"
    ordinary = '{"data":[{"id":"ordinary-model","name":"Ordinary name"}]}'
    models, output = run_fragment_caller(ctx, monkeypatch, capsys, caller, key, ordinary)
    assert_fragment_models(models, output, [("ordinary-model", "Ordinary name")])
    entry, = json.loads(path.read_text()).values()
    original = pricing.parse_catalog(get_provider("openai"), ordinary,
                                     verified_at=entry["fetched_at"])
    assert entry["models"] == json.loads(json.dumps([asdict(m) for m in original], default=str))
    path.unlink()
    rows, expected = fragment_rows(key)
    body = json.dumps({"data": rows})
    if encoding == "unicode":
        body = "".join(f"\\u{ord(c):04x}" if c.isalnum() else c for c in body)
    models, output = run_fragment_caller(ctx, monkeypatch, capsys, caller, key, body)
    # Read the actual persisted cache, not just the returned Catalog or bridge envelope.
    entry, = json.loads(path.read_text()).values()
    assert_fragment_models(entry["models"], "", expected)
    assert_fragment_models(models, output, expected)
    for i in range(len(key) - 15):
        assert key[i:i + 16] not in output, "credential fragment in stdout"
        assert key[i:i + 16] not in path.read_text(), "credential fragment on disk"
    cached = pricing.fetch_catalog(get_provider("openai"), key, get=forbidden, cache_path=path,
                                   at=datetime.fromisoformat(entry["fetched_at"]))
    assert cached.source == "cache"
    assert_fragment_models([asdict(m) for m in cached.models], "", expected)


@pytest.mark.parametrize("age", [timedelta(hours=1), timedelta(hours=25)], ids=["fresh", "stale"])
def test_partial_echo_cached_catalog(ctx, capsys, age):
    key = FRAGMENT_KEYS[1]
    rows, expected = fragment_rows(key)
    path = ctx.data_dir / "pricing.json"
    pricing.fetch_catalog(get_provider("openai"), key, cache_path=path, at=NOW,
                          get=lambda *_: (200, '{"data":[{"id":"ordinary-model"}]}'))
    cache = json.loads(path.read_text())
    entry, = cache.values()
    entry["models"] = [asdict(m) for m in pricing.parse_catalog(
        get_provider("openai"), json.dumps({"data": rows}), verified_at=NOW.isoformat())]
    # Exercise nested mapping keys and values too, independently of today's provider adapters.
    entry["models"][0]["rates"][key[-40:]] = {"status": "unknown", "same_as": key[8:24]}
    path.write_text(json.dumps(cache))
    catalog = pricing.fetch_catalog(get_provider("openai"), key, cache_path=path, at=NOW + age,
                                    get=lambda *_: (503, ""))
    assert catalog.source == "cache"
    repaired, = json.loads(path.read_text()).values()
    assert_fragment_models(repaired["models"], "", expected)
    assert_fragment_models([asdict(m) for m in catalog.models], "", expected)
    assert catalog.models[0].rates["[redacted]"].same_as == "[redacted]"
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == ""


@pytest.mark.parametrize("caller", ["cli-json", "cli-human", "bridge"])
def test_partial_echo_bridge_without_catalog_sanitizer(ctx, monkeypatch, capsys, caller):
    key = FRAGMENT_KEYS[1]
    rows, expected = fragment_rows(key)
    body = json.dumps({"data": rows})
    # Isolate the outgoing bridge boundary: a pricing-only fix must not satisfy this test.
    catalog = pricing.Catalog(pricing.parse_catalog(get_provider("openai"), body), "live", NOW, NOW)
    monkeypatch.setattr(pricing, "fetch_catalog", lambda *a, **kw: catalog)
    models, output = run_fragment_caller(ctx, monkeypatch, capsys, caller, key, body)
    assert_fragment_models(models, output, expected)


@pytest.mark.parametrize("caller", ["cli-json", "cli-human", "bridge"])
def test_partial_echo_error_streams(ctx, monkeypatch, capsys, caller):
    key = FRAGMENT_KEYS[1]
    rows, expected = fragment_rows(key)
    echoed_error = " | ".join(row["name"] for row in rows)
    clean_error = " | ".join(name for _, name in expected)

    def rejected(*_):
        # Exercise the expected-error path; unexpected exceptions already use fixed prose.
        raise bridge._KeyRequestError(echoed_error)

    monkeypatch.setattr(bridge, "_key_provider", rejected)
    if caller == "bridge":
        result = bridge.handle(ctx, {"action": "models_list", "provider": "openai", "secret": key})
        output = capsys.readouterr()
        assert output.out == ""
        assert output.err == ""
    else:
        monkeypatch.setattr(cli.Context, "default", classmethod(lambda cls: ctx))
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO(key + "\n"))
        args = ["keys", "models", "openai"] + (["--json"] if caller == "cli-json" else [])
        assert cli.main(args) == cli.EXIT_ERR
        output = capsys.readouterr()
        if caller == "cli-human":
            assert output.out == ""
            assert output.err == f"acctsw: {clean_error}\n"
            return
        assert output.err == ""
        result = json.loads(output.out)
    assert result["ok"] is False
    assert result["error"] == clean_error


@pytest.mark.parametrize("caller", CATALOG_CALLERS)
def test_partial_echo_marker_recheck(ctx, monkeypatch, capsys, caller):
    # The first replacement creates a *different* 17-character fragment of the key. Subsequent
    # fallback matches cross runs of literal stars, exercising repeated shrinking to termination.
    cases = [
        ("sk:[redacted]:tail|0123456789ABCDEF", "sk:0123456789ABCDEF:tail", "***"),
        ("1234567[redacted]|ABCDEFGHIJKLMNOP|" + "*" * 16,
         "*" * 15 + "1234567ABCDEFGHIJKLMNOP" + "*" * 15, "***"),
        ("0123456789ABCDEF", "0123456789ABCDEF", "[redacted]"),
        ("123456789ABCDEF0", "23456789ABCDEF0|123456789ABCDEF0", "23456789ABCDEF0|[redacted]"),
        ("sk-[redacted]", "sk-sk-[redacted]", "***"),
        ("*****", "*" * 15, "[redacted]" * 3),
    ]
    for key, echo, clean in cases:
        path = ctx.data_dir / "pricing.json"
        path.unlink(missing_ok=True)
        rows = [{"id": "chosen-model", "name": echo}]
        models, output = run_fragment_caller(ctx, monkeypatch, capsys, caller, key,
                                             json.dumps({"data": rows}))
        entry, = json.loads(path.read_text()).values()
        expected = [("chosen-model", clean)]
        assert_fragment_models(entry["models"], "", expected)
        assert_fragment_models(models, output, expected)
        for i in range(len(key) - 15):
            assert key[i:i + 16] not in path.read_text()
            assert key[i:i + 16] not in output
    for key in ("", "m", "id", "USD", "name"):
        ordinary = {"id": ["model-id", "USD model name"]}
        assert pricing._sanitize_catalog(ordinary, key) == ordinary
        assert bridge._redact_key(ordinary, key) == ordinary
