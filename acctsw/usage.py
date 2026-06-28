"""Read live usage / rate-limit windows for each seat.

Endpoints (see docs/PLAN.md):
  - Codex/ChatGPT: GET https://chatgpt.com/backend-api/wham/usage
      Authorization: Bearer <access_token>, ChatGPT-Account-Id: <account_id>
  - Claude:        GET https://api.anthropic.com/api/oauth/usage
      Authorization: Bearer <access_token>, anthropic-beta: oauth-2025-04-20,
      User-Agent: claude-code/<version>   (else 401 / aggressive 429)

The exact JSON field names are not officially documented, so the parsers are DEFENSIVE: they
look for utilization/percent + reset/resets_at under 5h and weekly buckets, and degrade gracefully.
Live field shapes are confirmed during end-to-end verification (M8); fixtures below encode the
assumed shape plus fallbacks so the normaliser is regression-tested.

Network access is injected (``get`` parameter) so unit tests never hit the wire.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from . import paths as P
from .util import iso, now

# A transport: (url, headers, timeout) -> (status_code, body_text)
HttpGet = Callable[[str, dict, float], "tuple[int, str]"]


@dataclass
class Window:
    used_pct: float | None = None
    resets_at: str | None = None  # ISO 8601


@dataclass
class Usage:
    ok: bool = False
    error: str | None = None  # "unauthorized" | "rate_limited" | "network" | "parse" | "no_token"
    windows: dict[str, Window] = field(default_factory=dict)  # "5h" / "weekly"
    limit_reached: bool | None = None  # authoritative flag when the API provides one (Codex)
    fetched_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error": self.error,
            "limit_reached": self.limit_reached,
            "fetched_at": self.fetched_at,
            "windows": {k: {"used_pct": w.used_pct, "resets_at": w.resets_at}
                        for k, w in self.windows.items()},
        }

    def soonest_reset(self) -> str | None:
        resets = [w.resets_at for w in self.windows.values() if w.resets_at]
        return min(resets) if resets else None


# --- real transport ---------------------------------------------------------------------------

def _default_get(url: str, headers: dict, timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        return e.code, body
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0, ""  # network failure


# --- token extraction -------------------------------------------------------------------------

CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"  # codex's OAuth client (from id_token aud)


def _default_post(url: str, payload: dict, timeout: float) -> tuple[int, str]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:
            return e.code, ""
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0, ""


def refresh_codex_blob(blob: str, *, post=_default_post) -> tuple[str | None, str | None]:
    """Use the refresh_token to mint a fresh codex auth.json blob (what codex does on its own).

    Returns (new_blob, error). error == "invalidated" means the session ended (re-login needed).
    """
    from .util import jwt_payload
    try:
        d = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return None, "parse"
    t = d.get("tokens") or {}
    rt = t.get("refresh_token")
    if not rt:
        return None, "no_refresh"
    aud = jwt_payload(t.get("id_token", "")).get("aud") or CODEX_CLIENT_ID
    client = aud[0] if isinstance(aud, list) and aud else (aud if isinstance(aud, str) else CODEX_CLIENT_ID)
    status, body = post(CODEX_TOKEN_URL, {
        "client_id": client, "grant_type": "refresh_token",
        "refresh_token": rt, "scope": "openid profile email offline_access",
    }, 20)
    if status != 200:
        return None, ("invalidated" if status in (400, 401) else f"http_{status}")
    try:
        out = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None, "parse"
    for k in ("access_token", "id_token", "refresh_token"):
        if out.get(k):
            t[k] = out[k]
    d["tokens"] = t
    d["last_refresh"] = iso(now())
    return json.dumps(d, indent=2), None


def codex_token_account(blob: str) -> tuple[str | None, str | None]:
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return None, None
    tokens = data.get("tokens") or {}
    return tokens.get("access_token"), tokens.get("account_id")


def claude_token(blob: str) -> str | None:
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return None
    return (data.get("claudeAiOauth") or {}).get("accessToken")


def claude_user_agent(claude_bin: str | None = None) -> str:
    exe = claude_bin or shutil.which("claude")
    if exe:
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
            if out.returncode == 0:
                ver = out.stdout.strip().split()[0]
                if ver:
                    return f"claude-code/{ver}"
        except (subprocess.SubprocessError, OSError):
            pass
    return P.CLAUDE_USER_AGENT_FALLBACK


# --- defensive normalisers --------------------------------------------------------------------

def _num(d: dict, *keys) -> float | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, bool):  # guard: bools are ints in Python
            continue
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):  # undocumented shapes sometimes stringify numbers
            try:
                return float(v)
            except ValueError:
                pass
    return None


def _reset(d: dict, *keys) -> str | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v:
            return v
        if isinstance(v, (int, float)):  # epoch seconds → iso
            from datetime import datetime, timezone
            return datetime.fromtimestamp(v, tz=timezone.utc).isoformat()
    return None


def _window_from(obj: Any) -> Window:
    if not isinstance(obj, dict):
        return Window()
    pct = _num(obj, "utilization", "used_percent", "used_pct", "percent", "percent_used")
    return Window(used_pct=pct, resets_at=_reset(obj, "resets_at", "reset_at", "resets", "reset"))


def parse_claude(payload: dict) -> dict[str, Window]:
    return {
        "5h": _window_from(payload.get("five_hour") or payload.get("5h")),
        "weekly": _window_from(payload.get("seven_day") or payload.get("weekly")
                               or payload.get("week")),
    }


def parse_codex(payload: dict) -> dict[str, Window]:
    """Parse the real ChatGPT ``wham/usage`` shape (and tolerate minor variations).

    Real shape (verified live)::

        {"rate_limit": {"primary_window":   {"used_percent": int, "reset_at": <epoch>},
                        "secondary_window": {"used_percent": int, "reset_at": <epoch>}}}
    """
    src = (payload.get("rate_limit") or payload.get("rate_limits")
           or payload.get("usage") or payload)
    primary = (src.get("primary_window") or src.get("primary") or src.get("five_hour")
               or src.get("5h") or src.get("hourly") or {})
    secondary = (src.get("secondary_window") or src.get("secondary") or src.get("weekly")
                 or src.get("seven_day") or src.get("week") or {})
    return {"5h": _window_from(primary), "weekly": _window_from(secondary)}


def codex_limit_reached(payload: dict) -> bool | None:
    src = payload.get("rate_limit") or payload.get("rate_limits") or {}
    v = src.get("limit_reached")
    return v if isinstance(v, bool) else None


# --- fetchers ---------------------------------------------------------------------------------

def _classify(status: int) -> str | None:
    if status == 200:
        return None
    if status == 401 or status == 403:
        return "unauthorized"
    if status == 429:
        return "rate_limited"
    if status == 0:
        return "network"
    return f"http_{status}"


def fetch_codex(token: str | None, account_id: str | None, *,
                get: HttpGet = _default_get, timeout: float = 12.0) -> Usage:
    u = Usage(fetched_at=iso(now()))
    if not token:
        u.error = "no_token"
        return u
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if account_id:
        headers[P.CODEX_ACCOUNT_ID_HEADER] = account_id
    status, body = get(P.CODEX_USAGE_URL, headers, timeout)
    err = _classify(status)
    if err:
        u.error = err
        return u
    try:
        payload = json.loads(body)
        u.windows = parse_codex(payload)
        u.limit_reached = codex_limit_reached(payload)
        u.ok = True
    except (json.JSONDecodeError, AttributeError, TypeError):
        u.error = "parse"
    return u


def fetch_claude(token: str | None, *, user_agent: str | None = None,
                 get: HttpGet = _default_get, timeout: float = 12.0) -> Usage:
    u = Usage(fetched_at=iso(now()))
    if not token:
        u.error = "no_token"
        return u
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": P.CLAUDE_OAUTH_BETA,
        "anthropic-version": P.ANTHROPIC_VERSION,
        "User-Agent": user_agent or claude_user_agent(),
        "Accept": "application/json",
    }
    status, body = get(P.CLAUDE_USAGE_URL, headers, timeout)
    err = _classify(status)
    if err:
        u.error = err
        return u
    try:
        u.windows = parse_claude(json.loads(body))
        u.ok = True
    except (json.JSONDecodeError, AttributeError, TypeError):
        u.error = "parse"
    return u


# --- orchestration: refresh into state (cache + backoff + limit flagging) ----------------------

LIMIT_PCT = 100.0  # a window at/above this means the seat is out of credit


def _seat_blob(ctx, state, tool: str, email: str) -> str | None:
    """Freshest creds for a seat: live blob if it's active, else the keychain snapshot."""
    if state.active(tool) == email:
        live = ctx.cred[tool].get_live()
        if live:
            return live
    return ctx.keychain.get(ctx.keychain_service, ctx.snapshot_key(tool, email))


def _fetch_for(tool: str, blob: str, get: HttpGet, ua: str | None) -> Usage:
    if tool == "codex":
        token, account = codex_token_account(blob)
        return fetch_codex(token, account, get=get)
    token = claude_token(blob)
    return fetch_claude(token, user_agent=ua, get=get)


MAX_BACKOFF_SECONDS = 3600


def _backoff_seconds(prev_usage: dict | None, base: int) -> float:
    """Exponential backoff: each consecutive error doubles the wait, capped at 1h.

    Claude's usage endpoint rate-limits hard; sustained 429s must not be retried every `base`s.
    """
    streak = int((prev_usage or {}).get("error_streak", 0) or 0)
    if streak <= 0:
        return base
    return min(base * (2 ** streak), MAX_BACKOFF_SECONDS)


def _due(prev_usage: dict | None, at, base: int) -> bool:
    from .util import parse_iso
    prev = parse_iso((prev_usage or {}).get("fetched_at"))
    if prev is None:
        return True
    return (at - prev).total_seconds() >= _backoff_seconds(prev_usage, base)


def refresh(ctx, state, tool: str | None = None, *, only: str | None = None,
            force: bool = False, get: HttpGet = _default_get, post=_default_post,
            min_seconds: int = P.USAGE_MIN_REFRESH_SECONDS,
            user_agent: str | None = None) -> dict[str, Any]:
    """Refresh cached usage for seats and flag limited seats. Persists state. Returns a summary.

    - Caching with EXPONENTIAL backoff: a seat is skipped if polled within its current backoff
      window (grows on consecutive errors), unless ``force``.
    - On error the last-known-good ``windows`` are PRESERVED (menubar keeps showing prior usage,
      marked stale); only ``error``/``fetched_at``/``error_streak`` are updated.
    """
    at = now()
    tools = [tool] if tool else ["codex", "claude"]
    # Lazily compute the Claude UA only if there is actually a Claude seat to poll.
    need_claude = "claude" in tools and bool(state.accounts("claude"))
    ua = user_agent or (claude_user_agent(getattr(ctx, "claude_bin", None)) if need_claude else None)
    summary: dict[str, Any] = {}
    for t in tools:
        summary[t] = {}
        for email in list(state.accounts(t)):
            if only and email != only:
                continue
            seat = state.get_seat(t, email)
            prev_usage = seat.get("usage") or {}
            if not force and not _due(prev_usage, at, min_seconds):
                summary[t][email] = "cached"
                continue
            blob = _seat_blob(ctx, state, t, email)
            if not blob:
                summary[t][email] = "no_creds"
                continue
            u = _fetch_for(t, blob, get, ua)
            # NOTE: we deliberately do NOT auto-refresh/rotate the token here. Codex's refresh
            # tokens are single-use; rotating one that codex itself owns (the active auth.json) can
            # invalidate codex's own session (reviewer KR-B2). For usage display we report
            # last-known/unauthorized instead. Per-account isolation (each account owning its home)
            # makes codex maintain its own tokens — refresh moves there.
            d = u.to_dict()
            if u.ok:
                d["error_streak"] = 0
                state.set_usage(t, email, d)
                _apply_limit(state, t, email, u, at)
            else:
                # preserve last-known-good windows; bump the error streak for backoff
                d["windows"] = prev_usage.get("windows", d["windows"])
                d["stale"] = True
                d["error_streak"] = int(prev_usage.get("error_streak", 0) or 0) + 1
                state.set_usage(t, email, d)
            summary[t][email] = u.error or "ok"
    state.save()
    return summary


def _limit_reset(u: Usage) -> str | None:
    """Return when a maxed-out seat unlocks, or None if it isn't limited.

    When MULTIPLE windows are maxed (e.g. both 5h and weekly), the seat stays blocked until the
    LATER reset, so we take ``max()`` over maxed windows (using ``min()`` would mark the seat
    available too early and the launcher would switch back to a still-capped seat).
    """
    maxed = [w.resets_at for w in u.windows.values()
             if w.used_pct is not None and w.used_pct >= LIMIT_PCT and w.resets_at]
    if maxed:
        return max(maxed)
    # Authoritative API flag with no window over 100 → use the latest known reset as best estimate.
    if u.limit_reached:
        allr = [w.resets_at for w in u.windows.values() if w.resets_at]
        return max(allr) if allr else None
    return None


def _apply_limit(state, tool: str, email: str, u: Usage, at) -> None:
    """Update limited_until from usage, WITHOUT prematurely clearing a reactive flag.

    A still-future ``reactive`` flag (set when the launcher caught a real limit mid-session) is
    authoritative over the usage endpoint, which can lag. We only clear it once it has expired.
    """
    from .util import parse_iso
    maxed_reset = _limit_reset(u)
    if maxed_reset:
        state.set_limited_until(tool, email, maxed_reset, source="usage")
        return
    seat = state.get_seat(tool, email)
    src = (seat or {}).get("limit_source")
    until = parse_iso((seat or {}).get("limited_until"))
    if src == "reactive" and until is not None and until > at:
        return  # keep the reactive flag until it expires
    state.set_limited_until(tool, email, None)
