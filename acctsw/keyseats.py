"""API keys are seats paired to an existing harness, never another tool.

Only metadata goes through State; secrets use Context's Keychain service and account naming
convention. Keeping the forward-compatible keys default here lets this engine ship before the
state/UI integration. Validation proves permission for one cheap operation, not inference access.
"""
from __future__ import annotations

import builtins
from copy import deepcopy
from typing import Any, Literal
from uuid import uuid4

from .context import Context
from .errors import AcctswError
from .keychain import KeychainError
from .pricing import HttpGet, _default_get
from .providers import Provider, classify_error
from .state import State
from .util import iso, now

Harness = Literal["codex", "claude"]


def harness_for(provider: Provider, *, allow_unverified: bool = False) -> Harness:
    """Pair a provider with the harness that can drive it, or refuse it.

    Chat-Completions-only providers are refused outright: Codex custom providers require the
    Responses wire API, so no acknowledgement can make one work. A Responses endpoint we have not
    confirmed — a custom base_url, or a beta implementation — is a different case: it may well
    work, we simply cannot promise it, so the caller may opt in and the seat records that the
    promise is missing. Refusing those outright would drop every self-hosted and compatible
    endpoint, which is most of the reason to support pasted keys at all.
    """
    if provider.wire_api == "messages":
        return "claude"
    if provider.wire_api != "responses":
        raise ValueError("This provider offers only Chat Completions; a Codex key seat requires "
                         "the Responses API, so no acknowledgement can make this work")
    if provider.responses_support == "verified" or allow_unverified:
        return "codex"
    raise ValueError("This provider's Responses support is not verified; pass allow_unverified to "
                     "add it anyway, and the seat will be marked as unproven")


def _keys(state: State) -> dict[str, dict[str, Any]]:
    return state.data.setdefault("keys", {})


def validate(ctx: Context, provider: Provider, secret: str, *,
             get: HttpGet = _default_get) -> dict[str, Any]:
    """Probe only the selected provider, with the redirect-disabled pricing GET transport.

    ctx is the same dependency boundary as CRUD; this standalone check writes neither store.
    Never persist response bodies or exception messages: a server may echo the credential.
    A denied models list does not establish that inference would be denied too.
    """
    operation = {"openrouter": "key_info", "deepseek": "balance"}.get(provider.id, "models_list")
    if not provider.validation_endpoint:
        raise ValueError("Provider has no validation endpoint")
    try:
        status, body = get(provider.validation_endpoint, provider.headers(secret), 20)
    except (OSError, ValueError, TypeError):
        # HTTP header validation can raise ValueError containing the original header value.
        status, body = 0, ""
    return {"checked_at": iso(now()), "operation": operation, "http_status": status,
            "operation_permitted": status == 200, "inference_verified": False,
            "error": None if status == 200 else classify_error(provider, status, body)}


def add(ctx: Context, provider: Provider, secret: str, *, label: str, model: str,
        get: HttpGet = _default_get, allow_unverified: bool = False) -> dict[str, Any]:
    """Store a new seat, retaining failed validation because operation scopes can differ.

    Validate outside the state lock, then write the secret before publishing its metadata.
    No secret-bearing Codex home or config is created by this module.
    """
    harness = harness_for(provider, allow_unverified=allow_unverified)  # Before sending any key.
    if not isinstance(secret, str) or len(secret) <= 4 or not secret.strip():
        raise ValueError("API key must contain more than four characters")
    if not label.strip() or not model.strip():
        raise ValueError("A label and model id are required")
    seat = {"id": uuid4().hex, "label": label, "provider": provider.id,
            "harness": harness, "model": model, "fingerprint": secret[-4:],
            # Recorded so the UI can say the endpoint is unproven rather than implying it works.
            "responses_verified": provider.responses_support == "verified",
            "created_at": iso(now())}
    if provider.region is not None:
        seat["region"] = provider.region
    if provider.id == "openai_compatible":
        seat["base_url"] = provider.base_url
    # Catch a pasted credential in a metadata field as well as in an echoed validation error.
    if any(secret in value for value in seat.values() if isinstance(value, str)):
        raise ValueError("API key must not appear in seat metadata")
    seat["last_validation"] = validate(ctx, provider, secret, get=get)
    account = ctx.snapshot_key("key", seat["id"])
    with ctx.locked():
        state = ctx.load_state()
        try:
            ctx.keychain.set(ctx.keychain_service, account, secret)
        except Exception:
            raise KeychainError("Could not store API key in Keychain") from None
        _keys(state)[seat["id"]] = seat
        try:
            state.save()
        except Exception:
            # Neither store is transactional with the other. If publication fails, try to
            # remove the unpublished secret, and name its account if cleanup also fails.
            try:
                removed = ctx.keychain.delete(ctx.keychain_service, account)
            except Exception:
                removed = False
            if not removed:
                raise KeychainError(f"Seat was not saved; Keychain cleanup failed for {account}") from None
            raise
    return deepcopy(seat)


def get(ctx: Context, id: str, *, harness: Harness | None = None) -> dict[str, Any] | None:
    """Return metadata only; a requested harness must match before a seat can be used."""
    seat = _keys(ctx.load_state()).get(id)
    if seat is not None and harness is not None and seat["harness"] != harness:
        raise ValueError("Key seat cannot take over a session of another harness")
    return deepcopy(seat)


def resolve(ctx: Context, value: str, *, harness: Harness) -> dict[str, Any]:
    """An exact id wins; labels must identify exactly one seat, never store-order roulette."""
    keys = _keys(ctx.load_state())
    matches = [keys[value]] if value in keys else [s for s in keys.values() if s["label"] == value]
    if not matches:
        raise AcctswError(f"no key seat named {value!r}")
    if len(matches) > 1:
        candidates = ", ".join(f"{s['id']} ({s['harness']}, {s['provider']}, {s['model']})"
                               for s in matches)
        raise AcctswError(f"ambiguous key label {value!r} — choose an id: {candidates}")
    if matches[0]["harness"] != harness:
        raise AcctswError(f"that key seat needs {matches[0]['harness']}, not {harness}")
    return deepcopy(matches[0])


def list(ctx: Context, *, harness: Harness | None = None) -> builtins.list[dict[str, Any]]:
    """List metadata without even reading Keychain; returned dictionaries are independent."""
    return [deepcopy(seat) for seat in _keys(ctx.load_state()).values()
            if harness is None or seat["harness"] == harness]


def get_secret(ctx: Context, id: str, *, harness: Harness) -> str | None:
    """Explicit credential access requires a matching harness, separate from metadata reads."""
    if harness not in ("codex", "claude"):
        raise ValueError("Credential access requires a codex or claude harness")
    with ctx.locked():
        if get(ctx, id, harness=harness) is None:
            return None
        return ctx.keychain.get(ctx.keychain_service, ctx.snapshot_key("key", id))


def remove(ctx: Context, id: str) -> bool:
    """Unpublish first so a failed Keychain deletion cannot leave an eligible seat behind.

    Report deletion failures, including False (the backend cannot distinguish missing from
    denied). Retrying with the same id also cleans up an orphan after metadata has gone.
    If saving state fails, do not delete the secret behind still-published metadata.
    """
    with ctx.locked():
        state = ctx.load_state()
        existed = _keys(state).pop(id, None) is not None
        if existed:
            state.save()
        account = ctx.snapshot_key("key", id)
        try:
            removed = ctx.keychain.delete(ctx.keychain_service, account)
        except Exception:
            removed = False
        if not removed:
            raise KeychainError(f"Seat metadata removed; Keychain deletion failed or item missing: {account}")
        return existed
