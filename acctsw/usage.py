"""Read live usage / rate-limit windows for each seat.

Endpoints (see docs/PLAN.md):
  - Codex/ChatGPT: GET https://chatgpt.com/backend-api/wham/usage
      Authorization: Bearer <access_token>, ChatGPT-Account-Id: <account_id>
  - Claude:        GET https://api.anthropic.com/api/oauth/usage
      Authorization: Bearer <access_token>, anthropic-beta: oauth-2025-04-20,
      User-Agent: claude-code/<version>   (else 401 / aggressive 429)

The exact JSON field names are not officially documented, so the parsers are DEFENSIVE: they
look for utilization/percent + reset/resets_at under 5h and weekly buckets, and degrade gracefully.
Claude's field shape is CONFIRMED live (2026-07-24): ``GET /api/oauth/usage`` returns ``utilization``
(and ``limits[].percent``) as USED percent — e.g. ``five_hour.utilization: 24.0`` means 24% used —
under ``five_hour`` (5h) and ``seven_day`` (weekly). So ``used_pct = utilization`` is correct: there
is NO inversion. The fixtures below encode this confirmed shape plus fallbacks (regression-tested).

Network access is injected (``get`` parameter) so unit tests never hit the wire.
"""
from __future__ import annotations

import json
import hashlib
import shutil
import subprocess
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import timedelta
from functools import lru_cache
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
    error: str | None = None  # "unauthorized" | "forbidden" | "rate_limited" | "network" | ...
    windows: dict[str, Window] = field(default_factory=dict)  # "5h" / "weekly"
    # Exact Codex window labels when the provider supplied durations. ``None`` means the response
    # used legacy positional buckets (or came from Claude/failed), so consumers must keep their
    # normal UI rather than infer that a missing 5h window was intentionally absent.
    reported_windows: list[str] | None = None
    limit_reached: bool | None = None  # authoritative flag when the API provides one (Codex)
    # Authoritative NON-percentage signals (codex). In the credits-depleted case the windows come
    # back null, so percentages prove nothing and only these say whether the seat can be used.
    plan_type: str | None = None            # "team" / "plus" / ... (display + triage)
    allowed: bool | None = None             # rate_limit.allowed: false ⇒ the API refuses work
    reached_type: str | None = None         # e.g. "workspace_member_credits_depleted"
    spend_control_reached: bool | None = None
    has_credits: bool | None = None         # DISPLAY ONLY — false on healthy subscriptions
    fetched_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error": self.error,
            "reported_windows": self.reported_windows,
            "limit_reached": self.limit_reached,
            "plan_type": self.plan_type,
            "allowed": self.allowed,
            "reached_type": self.reached_type,
            "spend_control_reached": self.spend_control_reached,
            "has_credits": self.has_credits,
            "fetched_at": self.fetched_at,
            "windows": {k: {"used_pct": w.used_pct, "resets_at": w.resets_at}
                        for k, w in self.windows.items()},
        }

    def soonest_reset(self) -> str | None:
        resets = [w.resets_at for w in self.windows.values() if w.resets_at]
        return min(resets) if resets else None


@dataclass(frozen=True)
class UsageFetchJob:
    """One detached provider request, tied to one exact seat incarnation and credential blob."""

    tool: str
    email: str
    blob: str
    credential_digest: str
    added_at: str | None
    active: bool
    user_agent: str | None = None
    recover_codex: bool = False


@dataclass(frozen=True)
class UsageFetchResult:
    job: UsageFetchJob
    usage: Usage


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

    Returns (new_blob, error). "invalidated" means the provider rejected the refresh credential;
    callers must rule out concurrent rotation before treating that as a need to log in again.
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


def account_fingerprint(tool: str, blob: str | None) -> str | None:
    """The PERSON behind a credential blob — the identity whose rate-limit windows a seat spends.
    Two seats that share a fingerprint are the same human on one subscription: one quota pool, so
    they can't cover each other when limited (a Gmail '+alias' codex login is still one ChatGPT
    user). Codex: ``chatgpt_user_id``/``user_id`` from the id/access token.

    NOT the ChatGPT account id: on Team/Business that id is the WORKSPACE every member shares, while
    each member keeps their OWN 5h/weekly windows (verified live — two members of one workspace read
    100% and 0% at the same moment, and hopping between them worked). Fingerprinting the workspace
    mis-flagged colleagues as one quota. Only workspace CREDITS are pooled, and the launcher's
    hard-limit landing pre-flight already proves the landing seat before it hops.

    Falls back to the workspace id (``account_workspace``) when no user claim is present — old
    blobs, API-key auth — which is the pre-user-id behaviour. Claude exposes no such id today →
    None (claude seats are distinct Anthropic accounts by email)."""
    if not blob or tool != "codex":
        return None
    from .util import jwt_payload
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return None
    tokens = data.get("tokens") or {}
    for tok in (tokens.get("id_token"), tokens.get("access_token")):
        auth = (jwt_payload(tok or "") or {}).get("https://api.openai.com/auth") or {}
        uid = auth.get("chatgpt_user_id") or auth.get("user_id")
        if uid:
            return uid
    return account_workspace(tool, blob)


def account_workspace(tool: str, blob: str | None) -> str | None:
    """The provider ACCOUNT id behind a credential blob (codex: ``chatgpt_account_id``). For a
    personal login that is the account itself; on Team/Business it is the workspace every member
    shares — so it names the SUBSCRIPTION, not the person (that's ``account_fingerprint``). It is
    what changes on a cancel/re-subscribe or a workspace move, which is why the poll uses it to
    decide that a seat's saved limits belong to a subscription that no longer exists."""
    if not blob or tool != "codex":
        return None
    from .util import jwt_payload
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return None
    tokens = data.get("tokens") or {}
    if tokens.get("account_id"):
        return tokens["account_id"]
    for tok in (tokens.get("access_token"), tokens.get("id_token")):
        auth = (jwt_payload(tok or "") or {}).get("https://api.openai.com/auth") or {}
        if auth.get("chatgpt_account_id"):
            return auth["chatgpt_account_id"]
    return None


def claude_token(blob: str) -> str | None:
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return None
    return (data.get("claudeAiOauth") or {}).get("accessToken")


def _codex_access_token_expired(blob: str) -> bool:
    """Local expiry explains a usage 401; it does not prove the session was revoked.

    Read only the access token's structural expiry, never the provider's error prose. Without a
    refresh credential there is no path to renewal, so keep the sign-in prompt. Match Claude's
    clock boundary (no early-expiry allowance): a token that still works must not be called expired.
    """
    from .util import jwt_payload
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return False
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict) or not _codex_refresh_token(blob):
        return False
    claims = jwt_payload(tokens.get("access_token"))
    expiry = claims.get("exp") if isinstance(claims, dict) else None
    return (isinstance(expiry, (int, float)) and not isinstance(expiry, bool)
            and 0 < expiry <= now().timestamp())


def _claude_access_token_expired(blob: str) -> bool:
    """An expired access token with a refresh credential is not proof of a signed-out session.

    Claude owns token renewal. The usage reader must neither rotate its refresh token nor ask
    the user to sign in merely because this short-lived access token has reached its expiry.
    """
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return False
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return False
    refresh = oauth.get("refreshToken")
    expiry = oauth.get("expiresAt")
    return (isinstance(refresh, str) and bool(refresh)
            and isinstance(expiry, (int, float)) and not isinstance(expiry, bool)
            and 0 < expiry <= now().timestamp() * 1000)


@lru_cache(maxsize=8)
def _claude_user_agent_for_exe(exe: str | None) -> str:
    if exe:
        try:
            # CLI diagnostics need not be valid UTF-8; a version probe must still be safe.
            out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=10)
            if out.returncode == 0:
                ver = out.stdout.strip().split()[0]
                if ver:
                    return f"claude-code/{ver}"
        except (subprocess.SubprocessError, OSError):
            pass
    return P.CLAUDE_USER_AGENT_FALLBACK


def claude_user_agent(claude_bin: str | None = None) -> str:
    """Return the official CLI User-Agent, caching the version subprocess for this app process."""
    return _claude_user_agent_for_exe(claude_bin or shutil.which("claude"))


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


def _bool(obj: Any, key: str) -> bool | None:
    """A STRICTLY boolean field, else None. Truthiness is refused on purpose: this payload uses
    ``null`` for "unknown", and coercing null to False would invent an answer the API never gave."""
    if not isinstance(obj, dict):
        return None
    v = obj.get(key)
    return v if isinstance(v, bool) else None


def _codex_rate_src(payload: Any) -> dict:
    """The container holding the rate-limit fields, tolerating the documented key variants."""
    src = payload if isinstance(payload, dict) else {}
    for k in ("rate_limit", "rate_limits", "usage"):
        v = src.get(k)
        if isinstance(v, dict) and v:
            return v
    return src


def _codex_window_objs(src: dict) -> tuple[dict, dict]:
    primary = (src.get("primary_window") or src.get("primary") or src.get("five_hour")
               or src.get("5h") or src.get("hourly") or {})
    secondary = (src.get("secondary_window") or src.get("secondary") or src.get("weekly")
                 or src.get("seven_day") or src.get("week") or {})
    return (primary if isinstance(primary, dict) else {},
            secondary if isinstance(secondary, dict) else {})


def _codex_labeled_windows(src: dict) -> dict[str, dict]:
    """Map Codex windows by their declared duration, with a legacy positional fallback.

    Most payloads historically omitted ``limit_window_seconds`` and used primary=5h and
    secondary=weekly. Newer plans can expose only a seven-day *primary* window, so position is no
    longer a reliable label when a duration is present. Explicit unknown durations get an honest
    stable key so limit calculations retain them without presenting them as a 5h limit.
    """
    labeled: dict[str, dict] = {}
    positions = (("primary", "5h"), ("secondary", "weekly"))
    for window, (position, legacy_label) in zip(_codex_window_objs(src), positions):
        if not window:
            continue
        if "limit_window_seconds" not in window:
            label = legacy_label
        else:
            duration = _num(window, "limit_window_seconds")
            label = {5 * 60 * 60: "5h", 7 * 24 * 60 * 60: "weekly"}.get(duration)
            if label is None:
                if duration is None:
                    label = f"{position}_window"
                elif duration.is_integer():
                    label = f"window_{int(duration)}s"
                else:
                    label = f"window_{duration:g}s"
        if label in labeled:
            label = f"{label}_{position}"
        labeled[label] = window
    return labeled


def _codex_reported_window_labels(src: dict) -> list[str] | None:
    """Provider-declared Codex window labels, without treating legacy positions as facts.

    A primary-only legacy payload cannot tell us whether it was actually a 5h or weekly tier. Only
    duration-bearing windows can drive a display decision such as hiding the 5h bar. Unknown
    valid durations still remain explicit labels (``window_86400s``) for callers
    that want to show the provider's real shape.
    """
    labeled = _codex_labeled_windows(src)
    if not labeled:
        return None
    for window in labeled.values():
        duration = _num(window, "limit_window_seconds")
        if duration is None or not 0 < duration < float("inf"):
            return None  # missing/malformed durations cannot establish that the 5h quota is absent
    return list(labeled)


def parse_codex(payload: dict) -> dict[str, Window]:
    """Parse the real ChatGPT ``wham/usage`` shape (and tolerate minor variations).

    Real shape (verified live)::

        {"rate_limit": {"primary_window":   {"used_percent": int, "reset_at": <epoch>},
                        "secondary_window": {"used_percent": int, "reset_at": <epoch>}}}
    """
    labeled = _codex_labeled_windows(_codex_rate_src(payload))
    windows = {"5h": _window_from(labeled.get("5h")),
               "weekly": _window_from(labeled.get("weekly"))}
    windows.update({key: _window_from(window) for key, window in labeled.items()
                    if key not in windows})
    return windows


def codex_limit_reached(payload: dict) -> bool | None:
    src = payload.get("rate_limit") or payload.get("rate_limits") or {}
    v = src.get("limit_reached")
    return v if isinstance(v, bool) else None


def parse_codex_flags(payload: dict) -> dict[str, Any]:
    """The AUTHORITATIVE non-percentage signals of the codex ``wham/usage`` payload.

    Percentages are BLIND in the credits-depleted case: both windows come back ``null`` while the
    only evidence is ``rate_limit.allowed: false`` plus ``rate_limit_reached_type``
    (e.g. "workspace_member_credits_depleted"). ``credits.has_credits`` is carried for DISPLAY only —
    it is false on perfectly healthy subscription accounts, so treating it as a limit signal would
    rest every seat we own.

    ``reset_after_seconds`` is the window's RELATIVE unlock, present even when ``reset_at`` is not.

    Never raises: every field is type-checked, so garbage or missing input yields Nones.
    """
    p = payload if isinstance(payload, dict) else {}
    rl = _codex_rate_src(p)
    def _reached_type(value: Any) -> str | None:
        if isinstance(value, dict):
            value = value.get("type")
        return value if (isinstance(value, str) and value) else None

    reached = _reached_type(p.get("rate_limit_reached_type"))
    if reached is None:
        reached = _reached_type(rl.get("rate_limit_reached_type"))
    labeled = _codex_labeled_windows(rl)

    def _secs(w: dict) -> int | None:
        n = _num(w, "reset_after_seconds")
        return int(n) if n is not None else None

    relative_resets = {key: _secs(window) for key, window in labeled.items()}
    relative_resets.setdefault("5h", None)
    relative_resets.setdefault("weekly", None)
    plan = p.get("plan_type")
    return {
        "plan_type": plan if isinstance(plan, str) else None,
        "allowed": _bool(rl, "allowed"),
        "reached_type": reached,
        "spend_control_reached": _bool(p.get("spend_control"), "reached"),
        "has_credits": _bool(p.get("credits"), "has_credits"),
        "reset_after_seconds": relative_resets,
    }


# --- fetchers ---------------------------------------------------------------------------------

def _classify(status: int) -> str | None:
    if status == 200:
        return None
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "forbidden"
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
        rate_src = _codex_rate_src(payload)
        u.windows = parse_codex(payload)
        u.reported_windows = _codex_reported_window_labels(rate_src)
        u.limit_reached = codex_limit_reached(payload)
        flags = parse_codex_flags(payload)
        u.plan_type = flags["plan_type"]
        u.allowed = flags["allowed"]
        u.reached_type = flags["reached_type"]
        u.spend_control_reached = flags["spend_control_reached"]
        u.has_credits = flags["has_credits"]
        # A window can carry only its RELATIVE unlock ("in 320961s") and no absolute stamp. Turn it
        # into one here so a limited seat rests until its real reset instead of a blind estimate.
        at = now()
        for key, secs in flags["reset_after_seconds"].items():
            w = u.windows.get(key)
            if w is not None and secs is not None and not w.resets_at:
                w.resets_at = iso(at + timedelta(seconds=secs))
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
USAGE_MIN_REFRESH_SECONDS = P.USAGE_MIN_REFRESH_SECONDS

# Last-good percentages are useful through a brief endpoint wobble, but after this age they are
# context, not current fact. The seat view exposes that distinction so a terminated subscription
# cannot keep painting frozen bars forever.
MAX_TRUSTED_AGE_S = 900

# Default cooldown when a limit is known but no authoritative reset is (shared with the launcher,
# which re-exports it; defined here so usage's own limit-flagging can use it without a cycle).
DEFAULT_COOLDOWN = timedelta(hours=5)

# A limit-signal is dismissed as a false positive (model prose / a stale reactive flag, not a real
# limit) when a FRESH fetch says the seat's busiest window is still below this. Well clear of a real
# ~100% limit even if the endpoint lags a few percent behind reality — a lagging truly-maxed seat
# still reads >= this, so it is never mistaken for healthy.
FALSE_ALARM_MAX_PCT = 90.0


def _seat_blob(ctx, state, tool: str, email: str) -> str | None:
    """Freshest credentials that can be proved to belong to the requested seat."""
    snapshot = ctx.snapshot_get(tool, email)
    if state.active(tool) != email:
        return snapshot
    if tool == "codex":
        # A supervised Codex process rotates the auth file in its private CODEX_HOME. The shared
        # mirror may be both stale and pointed at another account, so the private snapshot is the
        # source of truth while that process owns this seat.
        from . import session
        running = session.active_session(ctx.data_dir, "codex", email=email)
        if running and running.get("email") == email:
            return snapshot
        live = ctx.cred[tool].get_live()
        if live:
            from .credlocations import codex_jwt_matches
            if codex_jwt_matches(email, live):
                return live
        return snapshot
    live = ctx.cred[tool].get_live()
    return live or snapshot


def _fetch_for(tool: str, blob: str, get: HttpGet, ua: str | None) -> Usage:
    if tool == "codex":
        token, account = codex_token_account(blob)
        fetched = fetch_codex(token, account, get=get)
        if fetched.error == "unauthorized" and _codex_access_token_expired(blob):
            fetched.error = "token_expired"
        return fetched
    token = claude_token(blob)
    fetched = fetch_claude(token, user_agent=ua, get=get)
    if fetched.error == "unauthorized" and _claude_access_token_expired(blob):
        fetched.error = "token_expired"
    return fetched


MAX_BACKOFF_SECONDS = 3600
ACTIVE_MAX_BACKOFF_SECONDS = 300


def _backoff_seconds(prev_usage: dict | None, base: int, *, active: bool = False) -> float:
    """Exponential backoff, capped at 1h for stored seats and 5m for the active seat.

    Claude's usage endpoint rate-limits hard; sustained 429s must not be retried every `base`s.
    The seat actually on the floor gets the tighter cap so restored entitlement/network service is
    re-validated promptly instead of remaining hidden behind an hour-long historical error streak.
    """
    streak = int((prev_usage or {}).get("error_streak", 0) or 0)
    if streak <= 0:
        wait = base
    else:
        # A visible popover asks healthy active seats for a fresher cadence (30s), but an error must
        # retain the existing provider-safe 150s base.  In particular, an Anthropic 429 may not be
        # retried every 60 seconds merely because the card is on screen.
        error_base = max(base, USAGE_MIN_REFRESH_SECONDS)
        wait = min(error_base * (2 ** streak), MAX_BACKOFF_SECONDS)
    return min(wait, ACTIVE_MAX_BACKOFF_SECONDS) if active else wait


def _due(prev_usage: dict | None, at, base: int, *, active: bool = False) -> bool:
    from .util import parse_iso
    # ``fetched_at`` is deliberately the LAST SUCCESS so the UI can age the preserved windows.
    # Backoff needs the last ATTEMPT instead, otherwise a stale success would make every failed
    # popover refresh immediately retry the endpoint.
    prev = parse_iso((prev_usage or {}).get("last_attempted_at")
                     or (prev_usage or {}).get("fetched_at"))
    if prev is None:
        return True
    return (at - prev).total_seconds() >= _backoff_seconds(prev_usage, base, active=active)


def refresh(ctx, state, tool: str | None = None, *, only: str | None = None,
            force: bool = False, get: HttpGet = _default_get, post=_default_post,
            min_seconds: int = USAGE_MIN_REFRESH_SECONDS,
            user_agent: str | None = None) -> dict[str, Any]:
    """Refresh cached usage for seats and flag limited seats. Persists state. Returns a summary.

    - Caching with EXPONENTIAL backoff: a seat is skipped if polled within its current backoff
      window (grows on consecutive errors), unless ``force``.
    - On error the last-known-good ``windows`` are PRESERVED (menubar keeps showing prior usage,
      marked stale); ``fetched_at`` remains the last success while ``last_attempted_at`` and the
      error/backoff fields record the failed poll.
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
            if not force and not _due(
                    prev_usage, at, min_seconds, active=(state.active(t) == email)):
                summary[t][email] = "cached"
                continue
            blob = _seat_blob(ctx, state, t, email)
            if not blob:
                summary[t][email] = "no_creds"
                continue
            u = _fetch_for(t, blob, get, ua)
            # NOTE (KR-B2): Codex's refresh tokens are single-use; rotating the active auth.json
            # can invalidate Codex's own session, so active seats remain observers. Parked seats
            # differ: no child renews their expired token, leaving usage too stale for handoff.
            # Only refresh_live recovers them: expiry + refresh-token checks prove renewal is
            # possible, existing backoff limits retries, and session/incarnation/active checks
            # avoid applying usage to a seat now owned by a child or a different login. Its token CAS
            # preserves a newer refresh credential; comparing the sent refresh token on failure
            # prevents a lost race from being called revocation. This accepts, rather than closes,
            # the remaining window: cx launching on this seat between the session check and the
            # token POST can still double-spend the token. No network I/O runs under the flock.
            summary[t][email] = store_fetch(state, t, email, u, at=at, blob=blob)
    state.save()
    return summary


def _credential_digest(blob: str | None) -> str | None:
    return hashlib.sha256(blob.encode("utf-8")).hexdigest() if blob else None


def _detached_blob(ctx, state, tool: str, email: str, *, active: bool) -> str | None:
    """Read credentials for a detached fetch without assigning a live login to the wrong seat.

    Codex credentials identify themselves, so a mismatched live mirror falls back to that seat's
    private home.  Claude's blob carries no identity; its live bytes are usable only when they match
    the named snapshot.  The bridge reconciles a changed Claude login before reaching this helper.
    """
    snapshot = ctx.snapshot_get(tool, email)
    if not active:
        return snapshot
    if tool == "codex":
        # A supervised child owns and rotates its private CODEX_HOME.  The canonical ~/.codex mirror
        # may still contain the pre-launch bytes, so observer polling must read the child's home.
        from . import session
        running = session.active_session(ctx.data_dir, "codex")
        if running and running.get("email") == email:
            return snapshot
    live = ctx.cred[tool].get_live()
    if not live:
        return snapshot
    if tool == "codex":
        return live if ctx.cred[tool].email_of(live) == email else snapshot
    return live if live == snapshot else None


def _fetch_job(job: UsageFetchJob, get: HttpGet) -> UsageFetchResult:
    attempted_at = iso(now())
    try:
        fetched = _fetch_for(job.tool, job.blob, get, job.user_agent)
    except Exception:
        # A custom transport may raise even though the stdlib transport normally classifies errors.
        # Background polling must still release its in-flight gate and record a retryable failure.
        # Order failures by request start, like successful fetches. A late exception must not
        # overwrite a successful response from a request that started while this one was pending.
        fetched = Usage(error="network", fetched_at=attempted_at)
    return UsageFetchResult(job, fetched)


def _commit_fetch_result(ctx, result: UsageFetchResult) -> str:
    """Merge one detached result into freshly loaded state, rejecting every stale-result race."""
    from .util import parse_iso

    job, fetched = result.job, result.usage
    before = ctx.load_state()
    before_rev = int(before.data.get("rev", 0))
    was_active = before.active(job.tool) == job.email
    # Keychain reads are subprocesses on macOS.  Keep them outside the flock, then verify below that
    # the active pointer did not change while the credential bytes were being read.
    current_blob = _detached_blob(ctx, before, job.tool, job.email, active=was_active)
    with ctx.locked():
        state = ctx.load_state()
        # Credential reads above are intentionally outside the flock.  Any intervening mutation may
        # be a same-email re-login whose upsert preserves added_at and clears attempt timestamps, so
        # conservatively discard and let the next tick retry rather than merge across that gap.
        if int(state.data.get("rev", 0)) != before_rev:
            return "stale"
        seat = state.get_seat(job.tool, job.email)
        # added_at is the seat incarnation: removing and re-adding the same email (even with the same
        # credential bytes) must not let the old request paint the new card.
        if seat is None or seat.get("added_at") != job.added_at:
            return "stale"
        if (state.active(job.tool) == job.email) != was_active:
            return "stale"
        if _credential_digest(current_blob) != job.credential_digest:
            return "stale"
        # Two independent processes may poll at once.  A response that started earlier may finish
        # later; never let it replace the newer attempt already stored by the faster request.
        attempt_at = parse_iso(fetched.fetched_at)
        latest_at = parse_iso(((seat.get("usage") or {}).get("last_attempted_at")))
        if attempt_at is not None and latest_at is not None and latest_at > attempt_at:
            return "stale"
        status = store_fetch(state, job.tool, job.email, fetched, blob=job.blob)
        state.save()
        return status


def _codex_refresh_token(blob: str | None) -> str | None:
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return None
    tokens = data.get("tokens") if isinstance(data, dict) else None
    token = tokens.get("refresh_token") if isinstance(tokens, dict) else None
    return token if isinstance(token, str) and token else None


def _recover_parked_codex(ctx, job: UsageFetchJob, *, post,
                          min_seconds: int) -> tuple[UsageFetchJob | None, str | None]:
    """Spend outside the flock, then CAS the private snapshot; never write the shared mirror.

    A None job is a benign skip: in particular, no stale usage GET may turn a lost refresh race
    into an auth error. See the KR-B2 note above for the accepted concurrent-launch window.
    """
    from . import session

    sent_refresh_token = _codex_refresh_token(job.blob)
    try:
        new_blob, error = refresh_codex_blob(job.blob, post=post)
    except Exception:
        # Match the detached GET's handling of custom transports: leave credentials alone and
        # let the ordinary usage attempt record its existing error/backoff classification.
        return job, None
    if not new_blob and error != "invalidated":
        return job, None

    with ctx.locked():
        state = ctx.load_state()
        seat = state.get_seat("codex", job.email)
        # Codex snapshots are local files, so this CAS read belongs inside the short lock.
        current_blob = ctx.snapshot_get("codex", job.email)
        same_refresh = _codex_refresh_token(current_blob) == sent_refresh_token
        if error == "invalidated" and not same_refresh:
            return None, "refresh_raced"
        if new_blob and same_refresh:
            # The POST spent the token still on disk. Even a cadence/ownership skip must save its
            # replacement; preserving these old bytes would turn a healthy seat into a logout.
            try:
                ctx.snapshot_set("codex", job.email, new_blob)
            except Exception:
                # Identity validation or disk I/O must not abort the other parked jobs. Let the
                # ordinary usage attempt record a retryable failure using the stored credential.
                return job, None
        is_active = state.active("codex") == job.email
        running = session.active_session(ctx.data_dir, "codex", email=job.email)
        if (seat is None or seat.get("added_at") != job.added_at or is_active
                or running is not None):
            # Credential rotation is saved above, but this request no longer owns the usage result.
            return None, "stale"
        if _credential_digest(current_blob) != job.credential_digest:
            return None, "refresh_raced"
        if not _due(seat.get("usage"), now(), min_seconds, active=False):
            return None, "cached"
        if not (_codex_access_token_expired(current_blob)
                and _codex_refresh_token(current_blob)):
            return job, None
        if new_blob:
            return replace(job, blob=new_blob,
                           credential_digest=_credential_digest(new_blob) or "",
                           recover_codex=False), None
        # Only the still-current refresh credential can prove revocation. A different token
        # above means another consumer succeeded, even though our POST reported invalidated.
        seat["auth_error"] = "refresh_token_revoked"
        state.save()
        return job, None


def refresh_live(ctx, tool: str | None = None, *, only: str | None = None,
                 active_only: bool = False, force: bool = False,
                 get: HttpGet = _default_get, post=_default_post,
                 min_seconds: int = USAGE_MIN_REFRESH_SECONDS,
                 active_min_seconds: int = P.USAGE_ACTIVE_REFRESH_SECONDS,
                 user_agent: str | None = None) -> dict[str, Any]:
    """Refresh usage without holding the cross-process flock during Keychain or network I/O.

    Preparation records one immutable job per due seat.  Active Codex and Claude requests start in
    parallel so a slow provider cannot postpone the other provider's live card; each response is
    committed immediately under a fresh, short lock.  Parked seats follow afterward on the gentler
    cadence.  Commit revalidates the seat incarnation, credential bytes, and attempt ordering.
    """
    from . import session

    tools = [tool] if tool else ["codex", "claude"]
    summary: dict[str, Any] = {t: {} for t in tools}

    # State metadata is atomic/read-only here.  Credential reads deliberately happen after this
    # lock-free snapshot because macOS Keychain access is itself a subprocess.
    initial = ctx.load_state()
    candidates: list[tuple[str, str, str | None, bool]] = []
    at = now()
    for t in tools:
        active_email = initial.active(t)
        for email, seat in list(initial.accounts(t).items()):
            if only and email != only:
                continue
            active = email == active_email
            if active_only and not active:
                continue
            interval = active_min_seconds if active else min_seconds
            previous = seat.get("usage") or {}
            if (not force or previous.get("error")) and not _due(previous, at, interval, active=active):
                summary[t][email] = "cached"
                continue
            candidates.append((t, email, seat.get("added_at"), active))

    ua = user_agent
    if ua is None and any(t == "claude" for t, *_rest in candidates):
        ua = claude_user_agent(getattr(ctx, "claude_bin", None))

    prepared: list[UsageFetchJob] = []
    for t, email, added_at, was_active in candidates:
        blob = _detached_blob(ctx, initial, t, email, active=was_active)
        if not blob:
            summary[t][email] = "no_creds"
            continue
        prepared.append(UsageFetchJob(
            tool=t, email=email, blob=blob,
            credential_digest=_credential_digest(blob) or "",
            added_at=added_at, active=was_active,
            user_agent=ua if t == "claude" else None,
        ))

    # Session probes can spawn ps and prune stale heartbeats. Keep that sweep outside the flock;
    # this is only an eligibility hint, and recovery rechecks ownership after its POST.
    running_codex = {
        job.email: session.active_session(ctx.data_dir, "codex", email=job.email) is not None
        for job in prepared if job.tool == "codex" and not job.active
        and _codex_access_token_expired(job.blob)
    }

    # Recheck eligibility after slow Keychain reads.  Another poll, switch, remove, or re-add may
    # have happened since the initial atomic state read.
    eligible: list[UsageFetchJob] = []
    with ctx.locked():
        current = ctx.load_state()
        checked_at = now()
        for job in prepared:
            seat = current.get_seat(job.tool, job.email)
            if seat is None or seat.get("added_at") != job.added_at:
                summary[job.tool][job.email] = "stale"
                continue
            is_active = current.active(job.tool) == job.email
            if active_only and not is_active:
                summary[job.tool][job.email] = "stale"
                continue
            interval = active_min_seconds if is_active else min_seconds
            previous = seat.get("usage") or {}
            if (not force or previous.get("error")) and not _due(previous, checked_at, interval,
                                                               active=is_active):
                summary[job.tool][job.email] = "cached"
                continue
            eligible.append(UsageFetchJob(
                tool=job.tool, email=job.email, blob=job.blob,
                credential_digest=job.credential_digest, added_at=job.added_at,
                active=is_active, user_agent=job.user_agent,
                recover_codex=(job.tool == "codex" and not is_active and not job.active
                               and _codex_access_token_expired(job.blob)
                               and bool(_codex_refresh_token(job.blob))
                               and _due(previous, checked_at, min_seconds, active=False)
                               and not running_codex.get(job.email, False)),
            ))

    active_jobs = [job for job in eligible if job.active]
    parked_jobs = [job for job in eligible if not job.active]
    if active_jobs:
        with ThreadPoolExecutor(max_workers=len(active_jobs),
                                thread_name_prefix="acctsw-usage") as pool:
            futures = {pool.submit(_fetch_job, job, get): job for job in active_jobs}
            for future in as_completed(futures):
                result = future.result()
                summary[result.job.tool][result.job.email] = _commit_fetch_result(ctx, result)
    for job in parked_jobs:
        if job.recover_codex:
            recovered, skipped = _recover_parked_codex(ctx, job, post=post,
                                                      min_seconds=min_seconds)
            if recovered is None:
                summary[job.tool][job.email] = skipped
                continue
            job = recovered
        result = _fetch_job(job, get)
        summary[job.tool][job.email] = _commit_fetch_result(ctx, result)
    return summary


def store_fetch(state, tool: str, email: str, u: Usage, at=None, *,
                trust_reactive_lag: bool = True, blob: str | None = None) -> str:
    """Persist ONE fetch result onto a seat (windows, limit flags, error backoff) and return its
    summary status ("ok" or the error kind). Shared by refresh() and the launcher's inline probe,
    which must fetch WITHOUT the state lock held and only take it for this quick write. The caller
    saves state. ``trust_reactive_lag`` is threaded to _apply_limit (see there): default True keeps
    in-session/poll behaviour; the launcher's cold-start sweep passes False. A successful caller
    should pass the exact fetched ``blob`` so plan/account identity are refreshed from the same
    credentials that just authenticated."""
    at = at if at is not None else now()
    prev_usage = (state.get_seat(tool, email) or {}).get("usage") or {}
    d = u.to_dict()
    if u.ok:
        # Bind handoff decisions to the credentials authenticated by this reading.
        d["credential_digest"] = _credential_digest(blob)
        d["fetched_at"] = u.fetched_at or iso(at)
        d["last_attempted_at"] = d["fetched_at"]
        d["stale"] = False
        d["error_streak"] = 0
        if blob is not None:
            # accounts imports this module for account_fingerprint, so keep the reverse dependency
            # lazy. A successful poll is the proof that this exact current blob is meaningful.
            from . import accounts
            seat = state.get_seat(tool, email)
            if seat is not None:
                # Two ids, two jobs: ``account_id`` is the PERSON (shared-quota detection) and
                # ``workspace_id`` the subscription. The re-subscription check must compare the
                # SUBSCRIPTION — a workspace move keeps the person but not their windows, and the
                # person is stable across a re-login. Seats stamped before workspace_id existed
                # carry None and simply adopt it on this poll (no phantom re-subscription).
                old_workspace = seat.get("workspace_id")
                new_workspace = account_workspace(tool, blob)
                seat["plan"] = accounts.plan_of(tool, blob)
                seat["account_id"] = account_fingerprint(tool, blob)
                seat["workspace_id"] = new_workspace
                if old_workspace and new_workspace and old_workspace != new_workspace:
                    # Same email, different provider account means cancellation/re-subscription or
                    # a workspace move. Old rests and auth backoff belong to the old subscription.
                    state.set_limited_until(tool, email, None)
                    # ``d`` is this fresh successful fetch and already carries a cleared error,
                    # zero streak and non-stale status; set_usage below replaces the old dict whole.
                    state.data["moved_note"] = (
                        f"new {tool} subscription detected for {email} — saved limits were reset"
                    )
        state.set_usage(tool, email, d)
        _apply_limit(state, tool, email, u, at, trust_reactive_lag=trust_reactive_lag)
    else:
        # Preserve last-known-good windows AND their successful timestamp. The failed-attempt time is
        # separate so backoff still works while the renderer can age the data actually on screen.
        d["windows"] = prev_usage.get("windows", d["windows"])
        d["reported_windows"] = prev_usage.get("reported_windows", d["reported_windows"])
        if prev_usage.get("stale") and "last_attempted_at" not in prev_usage:
            # Pre-fix stale records used fetched_at for the failed attempt, not the retained windows;
            # there is no honest success age to recover, so treat them as never successfully fetched.
            d["fetched_at"] = None
        else:
            d["fetched_at"] = prev_usage.get("fetched_at") or None
        d["last_attempted_at"] = u.fetched_at or iso(at)
        d["stale"] = True
        d["error_streak"] = int(prev_usage.get("error_streak", 0) or 0) + 1
        state.set_usage(tool, email, d)
    return u.error or "ok"


def _is_limited(u: Usage) -> bool:
    """True when THIS fetch shows the seat out: a window at/above LIMIT_PCT, the authoritative
    ``limit_reached`` flag, or one of the other authoritative signals — ``allowed: false``, a
    non-empty ``rate_limit_reached_type``, or a reached spend control.

    Those three matter because the credits-depleted payload ("workspace_member_credits_depleted")
    reports NULL windows: there is no percentage to read, so percent-based logic is blind and only
    the flags can tell that the seat cannot be used. ``has_credits`` is deliberately NOT consulted —
    it is false on perfectly healthy accounts and would rest everything.

    A limited seat must ALWAYS end up rested even when the payload carries no reset
    timestamp (e.g. a codex workspace out of credits) — otherwise the seat reads "available" while
    the display shows 100% and the launcher picks a maxed seat."""
    return (bool(u.limit_reached) or u.allowed is False or bool(u.reached_type)
            or u.spend_control_reached is True
            or any(w.used_pct is not None and w.used_pct >= LIMIT_PCT
                   for w in u.windows.values()))


def _limit_reset(u: Usage) -> str | None:
    """The payload's own unlock time for a limited seat, or None if it carries no reset data.

    When MULTIPLE windows are maxed (e.g. both 5h and weekly), the seat stays blocked until the
    LATER reset, so we take ``max()`` over maxed windows (using ``min()`` would mark the seat
    available too early and the launcher would switch back to a still-capped seat). With no reset
    on the maxed window(s), the latest known reset across all windows is the best estimate.

    A window that only reported ``reset_after_seconds`` already had its ``resets_at`` synthesised in
    ``fetch_codex`` (now + n seconds), so relative-only payloads land here as ordinary resets and
    the caller never falls back to the blind DEFAULT_COOLDOWN estimate for them.
    """
    maxed = [w for w in u.windows.values() if w.used_pct is not None and w.used_pct >= LIMIT_PCT]
    resets = [w.resets_at for w in maxed if w.resets_at]
    if resets:
        return max(resets)
    allr = [w.resets_at for w in u.windows.values() if w.resets_at]
    return max(allr) if allr else None


def _confirmed_healthy(u: Usage) -> bool:
    """True only when THIS fresh fetch proves the seat has clear headroom (mirror of the launcher's
    ``_seat_confirmed_healthy``, for a Usage object in hand rather than persisted state): ok, no
    authoritative limit flag, and the busiest window well under the false-alarm bar. A lagging
    endpoint on a truly-maxed seat reads ~95-100% — above the bar — so lag can never look healthy.

    An authoritative "out" (``allowed: false`` / a reached type / spend control) vetoes health even
    when the windows look fine or are absent entirely: those are exactly the credits-depleted
    payloads whose percentages say nothing."""
    if not u.ok or u.limit_reached:
        return False
    if u.allowed is False or bool(u.reached_type) or u.spend_control_reached is True:
        return False
    pcts = [w.used_pct for w in u.windows.values() if w.used_pct is not None]
    return bool(pcts) and max(pcts) < FALSE_ALARM_MAX_PCT


def snapshot_says_out(u_dict: dict) -> bool:
    """The persisted-state mirror of ``_is_limited``'s flag half: True when a seat's stored ``usage``
    dict carries an authoritative out signal (``limit_reached``, ``allowed`` false, a non-empty
    ``reached_type`` or a reached spend control). Callers that hold state rather than a fresh
    ``Usage`` — the launcher — need the credits-depleted verdict too, and that case has NO
    percentages to inspect. Snapshots written before these fields existed simply lack the keys, so
    every lookup is a ``.get`` and an old state degrades to the ``limit_reached`` answer it had.
    ``has_credits`` is NOT consulted: it is false on perfectly healthy accounts."""
    d = u_dict if isinstance(u_dict, dict) else {}
    return (bool(d.get("limit_reached")) or d.get("allowed") is False
            or bool(d.get("reached_type")) or d.get("spend_control_reached") is True)


def _apply_limit(state, tool: str, email: str, u: Usage, at, *,
                 trust_reactive_lag: bool = True) -> None:
    """Update limited_until from usage, without prematurely clearing a rest the fetch can't disprove.

    - A still-future ``hard`` flag survives ordinary and in-session polling. At a new Codex
      launch only, an explicit provider ``allowed: true`` with clear headroom can disprove an
      old billing block. Percentages alone cannot: they may look healthy while credits are out.
    - A limited fetch whose payload carries NO reset data stamps a DEFAULT_COOLDOWN estimate ONCE:
      an existing still-future stamp is kept STABLE rather than re-anchored to ``at`` on every poll,
      which would make the launcher's wait target recede forever (and re-notify on each poll).
    - A still-future ``reactive`` flag (limit caught mid-session) is kept while the endpoint lags a
      real limit — but a CONFIRMED-healthy fetch (<FALSE_ALARM_MAX_PCT, no limit flag) cannot be
      lag, so it clears the flag: that stale false positive is what wrongly blocked launches with
      "all seats resting" when capacity actually existed.

    ``trust_reactive_lag`` (default True) also preserves hard billing blocks in-session:
      - True  (in-session probes, menubar poll, in-wait polling sweeps): keep a reactive rest whenever
        the busiest window sits in the lag band (>=FALSE_ALARM_MAX_PCT). Mid-run the endpoint can trail
        a real limit by a few percent, and clearing it here would ping-pong the launcher back onto a
        seat that's genuinely out.
      - False (the launcher's PRE-LAUNCH / cold-start forced sweep only): clear a reactive flag as long
        as the fetch is ok and NOT actually limited (below the real 100% limit / no limit_reached).
        Rationale for the startup difference — a reactive flag is the WEAKEST evidence we hold (a guess
        stamped from a prior stdout match, not the authoritative API), the endpoint says there is still
        credit, and if the seat really is out the in-session limit guard will re-catch it and hop. So at
        cold start we start the session rather than kill it on a stale near-max guess.
        TRADEOFF (flagged for review): a seat truly sitting at e.g. 95-99% used gets ONE launch attempt
        before in-session detection rests it again, instead of waiting out the 5h cooldown up front.
    """
    from .util import parse_iso
    seat = state.get_seat(tool, email)
    src = (seat or {}).get("limit_source")
    until = parse_iso((seat or {}).get("limited_until"))
    live = until is not None and until > at
    if src == "hard" and live:
        if (tool == "codex" and not trust_reactive_lag
                and u.allowed is True and _confirmed_healthy(u)):
            state.set_limited_until(tool, email, None)
        return
    if _is_limited(u):
        reset = _limit_reset(u)
        if reset:
            state.set_limited_until(tool, email, reset, source="usage")
        elif not live:
            state.set_limited_until(tool, email, iso(at + DEFAULT_COOLDOWN), source="usage")
        return
    if live and src == "reactive" and not _confirmed_healthy(u) and trust_reactive_lag:
        return  # inconclusive / near-max reading: keep the rest until it expires (in-session guard)
    state.set_limited_until(tool, email, None)
