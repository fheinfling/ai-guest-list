"""CLI key entry uses a terminal prompt or stdin, never argv."""
import io
import json
import warnings
from decimal import Decimal

import pytest

from acctsw import cli, keyseats, pricing

SECRET = "sk-cli-private-key-123456"
ADD = ["keys", "add", "openrouter", "--label", "work", "--model", "example-model"]


@pytest.fixture
def isolated(ctx, monkeypatch):
    monkeypatch.setattr(cli.Context, "default", classmethod(lambda cls: ctx))
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SECRET + "\n"))

    def get(url, headers, timeout):
        if url.endswith("/key"):
            assert headers["Authorization"] == "Bearer " + SECRET
            return 200, '{"data": {}}'
        return 200, json.dumps({"data": [
            {"id": "example-model", "name": "example", "context_length": 100000,
             "pricing": {"prompt": "0.000002", "completion": "0.000006"}},
        ]})

    monkeypatch.setattr(pricing, "_default_get", get)
    return ctx


def test_stdin_add_list_remove_json(isolated, capsys):
    assert cli.main([*ADD, "--json"]) == cli.EXIT_OK
    saved = json.loads(capsys.readouterr().out)
    assert saved["ok"] and saved["added"] == saved["seat"]["id"]
    assert SECRET not in json.dumps(saved)
    assert keyseats.get_secret(isolated, saved["added"], harness="codex") == SECRET

    assert cli.main(["keys", "list", "--json"]) == cli.EXIT_OK
    listed = json.loads(capsys.readouterr().out)
    assert listed == {"ok": True, "keys": [saved["seat"]]}
    assert SECRET not in json.dumps(listed)
    assert cli.main(["keys", "remove", saved["added"], "--json"]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out) == {"ok": True, "removed": True}
    assert cli.main(["keys", "list", "--json"]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out) == {"ok": True, "keys": []}


@pytest.mark.parametrize("args", [ADD, ["keys", "models", "openrouter"]])
@pytest.mark.parametrize("extra", [[SECRET], ["--secret", SECRET], ["--key", SECRET], ["--api-key", SECRET]])
def test_key_cannot_be_passed_in_argv(isolated, args, extra):
    with pytest.raises(SystemExit) as exc:
        cli.main([*args, *extra])
    assert exc.value.code == 2
    assert keyseats.list(isolated) == []


def test_tty_uses_hidden_prompt(isolated, monkeypatch, capsys):
    class Terminal(io.StringIO):
        def isatty(self):
            return True

        def read(self, *args):
            pytest.fail("terminal secret must use getpass")

    monkeypatch.setattr(cli.sys, "stdin", Terminal())
    prompts = []
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: prompts.append(prompt) or SECRET)
    assert cli.main(ADD) == cli.EXIT_OK
    assert prompts == ["api key: "]
    output = capsys.readouterr()
    assert "key seat saved" in output.out and SECRET not in output.out + output.err


def test_tty_never_falls_back_to_echo(isolated, monkeypatch, capsys):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    def no_tty(*_):
        warnings.warn("cannot hide input", cli.getpass.GetPassWarning)
        pytest.fail("must stop before getpass falls back to visible input")

    monkeypatch.setattr(cli.getpass, "getpass", no_tty)
    assert cli.main([*ADD, "--json"]) == cli.EXIT_ERR
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert keyseats.list(isolated) == []


@pytest.mark.parametrize("provider", ["openrouter", "openai"])
def test_models_json(isolated, capsys, provider):
    assert cli.main(["keys", "models", provider, "--json"]) == cli.EXIT_OK
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] and result["source"] == "live"
    assert SECRET not in json.dumps(result)
    model = result["models"][0]
    assert model["id"] == "example-model" and model["display_name"] == "example"
    if provider == "openrouter":
        assert result["sort_key"] == "input_usd_per_million_tokens"
        assert Decimal(model["price"]["rates"]["input"]["value"]) == 2
    else:
        assert result["sort_key"] == "id" and "price" not in model


@pytest.mark.parametrize("provider", ["openrouter", "openai"])
def test_models_human_prices_or_plain_absence(isolated, capsys, provider):
    assert cli.main(["keys", "models", provider]) == cli.EXIT_OK
    output = capsys.readouterr().out
    assert SECRET not in output
    if provider == "openrouter":
        assert "input 2" in output and "output 6" in output and "estimate" in output
    else:
        assert "does not publish" in output and "sorted by id" in output
        assert "price unavailable" in output


def test_human_list_and_remove(isolated, capsys):
    assert cli.main(ADD) == cli.EXIT_OK
    capsys.readouterr()
    id = keyseats.list(isolated)[0]["id"]
    assert cli.main(["keys", "list"]) == cli.EXIT_OK
    assert f"[codex] ({id})" in capsys.readouterr().out
    assert cli.main(["keys", "remove", id]) == cli.EXIT_OK
    assert "goodbye" in capsys.readouterr().out
    assert cli.main(["keys", "list"]) == cli.EXIT_OK
    assert "no key seats" in capsys.readouterr().out


@pytest.mark.parametrize("json_output", [False, True])
def test_failures_follow_exit_convention_without_leaking(isolated, monkeypatch, capsys, json_output):
    def fail(*_):
        raise RuntimeError(f"transport echoed {SECRET}")

    monkeypatch.setattr(pricing, "_default_get", fail)
    assert cli.main([*ADD, *(["--json"] if json_output else [])]) == cli.EXIT_ERR
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    if json_output:
        assert json.loads(output.out)["ok"] is False and not output.err
    else:
        assert output.err.startswith("acctsw: ")
    assert keyseats.list(isolated) == []


def test_empty_stdin_is_expected_error(isolated, monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO())
    assert cli.main([*ADD, "--json"]) == cli.EXIT_ERR
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_ctrl_c_during_key_entry_exits_cleanly(isolated, monkeypatch, capsys):
    def interrupted():
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_read_key_secret", interrupted)
    assert cli.main(ADD) == 130
    assert "interrupted" in capsys.readouterr().err
