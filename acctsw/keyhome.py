"""Prepare BYOK runtimes without launching a process or borrowing subscription settings.

Reuse codexhome's short, hashed home layout: the app-server socket has a byte budget, so adding
another directory or putting the key-seat UUID in the path would break ordinary macOS homes.
Unlike subscription homes these homes contain no links into ~/.codex or ~/.claude. In particular,
ensure_home's shared-config linking and promotion are inappropriate for a key seat.

prepare returns only non-secret launch metadata. build_env reads Keychain afresh at launch time;
its dict is suitable for subprocess env, but redacts its representation. Deliberately serializing
or logging individual environment values is still the caller's responsibility.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path

from . import codexhome, keyseats, procenv, providers
from .context import Context
from .keychain import KeychainError
from .util import atomic_write_text, chmod_dir


class ChildEnv(dict[str, str]):
    """Keep ordinary diagnostics from printing credentials, including inherited credentials."""

    def __repr__(self) -> str:
        return "ChildEnv(<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True)
class KeyHome:
    seat_id: str
    harness: keyseats.Harness
    home: Path
    model: str
    base_url: str
    env_key: str
    model_provider: str | None = None


def _secret(ctx: Context, runtime: KeyHome) -> str:
    try:
        secret = keyseats.get_secret(ctx, runtime.seat_id, harness=runtime.harness)
    except Exception:
        # Keychain backends may include the credential in an exception message.
        raise KeychainError("Could not read key-seat credential") from None
    if not secret:
        raise KeychainError("Key-seat credential is missing")
    if any(secret in str(value) for value in vars(runtime).values() if value is not None):
        raise ValueError("API key must not appear in runtime metadata")
    return secret


def _quote(value: str) -> str:
    """A TOML basic string, including control characters and non-BMP Unicode.

    JSON's surrogate-pair escapes are not TOML Unicode scalars, so do not use ensure_ascii=True.
    Emit TOML escapes ourselves; quotes, backslashes and line breaks never become syntax.
    """
    out = []
    for char in value:
        code = ord(char)
        if char in ('"', '\\'):
            out.append('\\' + char)
        elif code < 0x20 or code == 0x7f:
            out.append(f"\\u{code:04x}")
        elif 0xd800 <= code <= 0xdfff:
            raise ValueError("Runtime metadata must contain valid Unicode")
        else:
            out.append(char)
    return '"' + ''.join(out) + '"'


def _config(runtime: KeyHome, name: str, *, stateless: bool) -> str:
    assert runtime.model_provider is not None
    if name.strip().casefold() == "openai" or runtime.model_provider.casefold() == "openai":
        raise ValueError('A custom Codex provider must not be named "OpenAI"')
    lines = [f"model_provider = {_quote(runtime.model_provider)}",
             f"model = {_quote(runtime.model)}", 'web_search = "disabled"',
             "check_for_update_on_startup = false", "",
             f"[model_providers.{_quote(runtime.model_provider)}]",
             f"name = {_quote(name)}", f"base_url = {_quote(runtime.base_url)}",
             'wire_api = "responses"', f"env_key = {_quote(runtime.env_key)}",
             "requires_openai_auth = false", "request_max_retries = 0",
             "stream_max_retries = 0"]
    if stateless:
        # Langdock rejects previous_response_id, used by Codex's WebSocket continuation path.
        lines.append("supports_websockets = false")
    # env_key is read by Codex itself. Its shell subprocesses must not inherit that key.
    # Default secret-name exclusions are OFF in Codex 0.157.0. Keep an explicit exclusion
    # for the actual provider variable as well as enabling the default secret filters.
    lines += ["", "[shell_environment_policy]", 'inherit = "all"',
              "ignore_default_excludes = false", f"exclude = [{_quote(runtime.env_key)}]",
              "", "[analytics]", "enabled = false", "", "[feedback]", "enabled = false",
              "", "[otel]", 'exporter = "none"', 'trace_exporter = "none"',
              # Load-bearing: the default metrics exporter is Statsig -> ab.chatgpt.com,
              # even with a custom inference provider. Disabling analytics alone is insufficient.
              'metrics_exporter = "none"', "log_user_prompt = false",
              "", "[features]", "apps = false", "plugins = false",
              # Verified with a real 0.157.0 session: snapshot generation bypasses the
              # shell environment exclusions and writes env_key to disk. Both controls
              # are required; do not re-enable snapshots based on policy config alone.
              "shell_snapshot = false", ""]
    return "\n".join(lines)


def prepare(ctx: Context, id: str, *, pin: str | None = None) -> KeyHome:
    """Write private configuration and return metadata; never launch or persist a credential.

    Existing seats have already passed keyseats' harness admission check. Custom Responses
    verification is not persisted by that module, so do not reclassify an admitted custom seat
    using the registry's default 'unverified'. Read the secret only to reject contaminated
    metadata before any write; build_env retrieves it again so key rotation is respected.
    """
    seat = keyseats.get(ctx, id)
    if seat is None:
        raise ValueError("Unknown key seat")
    harness = seat["harness"]
    if harness not in ("codex", "claude"):
        raise ValueError("Unknown key-seat harness")
    provider = providers.get_provider(seat["provider"], region=seat.get("region"),
                                      base_url=seat.get("base_url"))
    # Namespace the identity, not the path: key IDs cannot alias subscription email identities.
    # Pinned terminals can share a key, but not a daemon or transcript tree. Hash the identity
    # through the existing short-home layout so the app-server socket still fits on macOS.
    identity = codexhome.key_home_identity(harness, id, pin)
    home = codexhome.home_dir(identity, ctx._homes_root)
    model_provider = None
    base_url = provider.base_url
    name = provider.display_name
    if harness == "codex":
        if provider.wire_api != "responses":
            raise ValueError("Codex requires a Responses provider")
        model_provider = provider.id
        if provider.id == "openai":
            # Even direct API seats use a custom identity, avoiding first-party auth branches.
            model_provider, name = "openai_api", "OpenAI API"
        elif provider.id == "langdock":
            model_provider = f"langdock_{provider.region}"
        env_key = {"openai": "OPENAI_API_KEY", "langdock": "LANGDOCK_API_KEY",
                   "openrouter": "OPENROUTER_API_KEY"}.get(provider.id, "ACCTSW_API_KEY")
    else:
        if provider.id in ("langdock", "langdock_anthropic"):
            base_url = f"https://api.langdock.com/anthropic/{provider.region}"
        elif provider.id == "anthropic":
            # The registry's catalog base includes /v1; Claude adds /v1/messages itself.
            base_url = provider.base_url.removesuffix("/v1")
        else:
            raise ValueError("Provider has no supported Claude endpoint")
        env_key = "ANTHROPIC_AUTH_TOKEN"
    runtime = KeyHome(id, harness, home, seat["model"], base_url, env_key, model_provider)
    secret = _secret(ctx, runtime)
    if harness == "codex":
        content = _config(runtime, name, stateless=provider.id == "langdock")
        filename = "config.toml"
    else:
        # The nonessential-traffic flag does NOT suppress WebFetch preflight. This file lives
        # only in the seat's CLAUDE_CONFIG_DIR; no user's Claude settings need to be edited.
        content = json.dumps({"skipWebFetchPreflight": True}) + "\n"
        filename = "settings.json"
    # Check raw values as well as escaped output (a quoted key may not survive TOML escaping).
    if any(secret in value for value in (name, runtime.model, base_url, content)):
        raise ValueError("API key must not appear in runtime configuration")
    # Refuse redirected homes rather than writing/chmodding through a link into user config.
    # /tmp itself may legitimately be a symlink on macOS; guard the managed root and leaf.
    if ctx._homes_root.is_symlink() or home.is_symlink():
        raise ValueError("Key-seat home must be a private directory")
    if home.exists() and any(p.is_symlink() for p in home.rglob("*")):
        raise ValueError("Key-seat home must not contain shared links")
    chmod_dir(home, 0o700)
    atomic_write_text(home / filename, content, mode=0o600)
    return runtime


def build_env(ctx: Context, runtime: KeyHome, *, env: dict[str, str] | None = None) -> ChildEnv:
    """Build a child-only environment from a prepared seat, reading its current Keychain key.

    No filesystem writes or process-global environment mutations. The caller must prepare again
    after changing seat metadata; rotating only the Keychain secret needs no config rewrite.
    """
    secret = _secret(ctx, runtime)
    child = ChildEnv(procenv.harden_env(env))
    if runtime.harness == "codex":
        child["CODEX_HOME"] = str(runtime.home)
    else:
        # Inherited subscription/cloud routing must not override this API-key seat.
        for key in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS",
                    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
            child.pop(key, None)
        child.update(CLAUDE_CONFIG_DIR=str(runtime.home), ANTHROPIC_BASE_URL=runtime.base_url,
                     ANTHROPIC_MODEL=runtime.model, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                     # Verified with Claude Code 2.1.282: the parent retains the auth token,
                     # while Bash and snapshot subprocesses receive a scrubbed environment.
                     CLAUDE_CODE_SUBPROCESS_ENV_SCRUB="1",
                     DISABLE_TELEMETRY="1", DISABLE_ERROR_REPORTING="1", DISABLE_AUTOUPDATER="1")
    child[runtime.env_key] = secret
    return child


def publish_resume(ctx: Context, runtime: KeyHome) -> None:
    """After a pinned child flushes, make its conversations discoverable by subscription resume.

    Only transcripts travel. Keep the private originals, and never copy auth, configuration or
    live databases. A pin owns its home, so another terminal's unfinished turns cannot be copied.
    Codex's normal resume picker filters by provider: the shared copy belongs to the subscription
    provider, while the private original retains its metered provenance.
    """
    from . import paths
    if runtime.harness == "codex":
        source = runtime.home / "sessions"
        target = ctx._codex_real / "sessions"
        files = source.rglob("rollout-*.jsonl")
    else:
        source = runtime.home / "projects"
        target = Path(os.environ.get("CLAUDE_CONFIG_DIR", paths.CLAUDE_CONFIG_DIR)) / "projects"
        files = source.glob("*/*.jsonl")
    for path in files:
        text = path.read_text(encoding="utf-8")
        if runtime.harness == "codex":
            lines = text.splitlines(keepends=True)
            for i, line in enumerate(lines):
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # retain an incomplete final record exactly as the child left it
                if row.get("type") == "session_meta":
                    row["payload"]["model_provider"] = "openai"
                    lines[i] = json.dumps(row, ensure_ascii=False) + "\n"
                    break
            text = "".join(lines)
        atomic_write_text(target / path.relative_to(source), text, mode=0o600)
