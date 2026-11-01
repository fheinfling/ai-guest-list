"""Main-thread glue for in-place seat switching: Claude keeps running while its requests move seats.

The launcher owns one ``SeatSwap`` per supervised Claude run. Its ``SeatProxy`` (``seatproxy``,
I/O only) is the child's HTTPS proxy on 127.0.0.1 (transport v2: ``HTTPS_PROXY``, never
``ANTHROPIC_BASE_URL``, so Claude Code stays first-party). It terminates TLS for api.anthropic.com
only, with a per-run ``SessionCA`` the child trusts through its CA bundle variables, and tunnels
every other host untouched (chained to the user's own proxy, if any). The child keeps its own
home-seat login, and the proxy swaps in another seat's access token only while that seat is bound.
A child that rejects the session CA flips ``tls_rejected()`` and wakes the launcher. Every
decision is made here, on the launcher's thread:

- the proxy thread posts a ``Hit`` (a limit or a 401) and blocks up to ``decide_timeout`` seconds;
  a byte on ``wake_fd`` wakes the launcher's select loop, which reads ``pending()`` and replies
  with ``answer(hit, seat)``. No reply in time means the proxy passes the response through and the
  ordinary restart path takes over (a late ``answer`` is refused);
- ``maintain()`` re-reads the bound seat's Keychain item at most once a minute: a newer token is
  rebound; a token about to lapse moves the session home, else to another seat, else asks for one
  "handover" restart once the proxy is idle. This app never refreshes a token itself.

Gates (``gate``/``enabled``): the setting ``in_place_switch``, a falsy ``ACCTSW_IN_PLACE_SWITCH``
(0/false/no/off), no ``/usr/bin/openssl`` (no session CA), a launcher nested inside another
supervised session (its inherited HTTPS proxy is ours), and any configuration that already routes or authenticates Claude differently:
the env, or the ``env``/``apiKeyHelper`` of a Claude settings source (user and project
settings.json/settings.local.json, managed-settings.json and managed-settings.d/*.json, and the
child's own ``--settings <file|json>``; one that can't be read gates too) naming a base URL other
than https://api.anthropic.com, an API key or token, Bedrock/Vertex/Foundry, or a key helper. Everything this module can't serve falls
back to the restart path.

Eligibility (``eligible``): the seat's Keychain item (quarantined homes have none), its access
token, and a stated ``expiresAt`` more than ``MIN_TTL`` ahead — an unknown expiry is not eligible —
and the same ``subscriptionType`` as the home seat when both are known. A Keychain read error is
not eligible either, but ``maintain`` keeps a binding it can't re-read rather than moving the
session. Crossings between business and personal seats follow ``seatkind``: never without an
approval for that direction, and never for a move back to the session's own home seat.

Tokens live in memory only and are never logged.

Additions to the frozen interface (nothing in it changed): ``gate()`` names why the swap is off;
``SeatSwap(ctx, home_email=None, proxy=..., proxy_factory=...)`` starts the proxy at once (an
``OSError`` from its bind propagates; the launcher then restarts instead); ``stop()`` is
idempotent; ``bind(seat)`` rebinds without a hit; ``bound()`` is the billed seat's email (None
before any bind); ``answer()`` returns whether the reply was accepted; ``pick()`` chooses (and by
default spends the approval for) the next seat, by the same rules ``maintain`` uses;
``preflight()`` adds a local session-CA handshake self-test to the proxy's (cached) reachability
check; ``proxy_used()`` is the proxy's ``messages_seen() > 0``; ``tls_rejected()``.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from . import seatkind, seattier
from .seatproxy import Seat
from .selection import choose
from .util import iso, now, parse_iso, write_json

TOOL = "claude"
MIN_TTL = timedelta(minutes=15)
MAINTAIN_EVERY_S = 60.0     # at most one Keychain read of the bound seat per minute
HANDOVER_IDLE_S = 5.0       # a handover restart waits for this much proxy silence
UPSTREAM = "https://api.anthropic.com"
API_HOST = "api.anthropic.com"
OPENSSL = "/usr/bin/openssl"   # SessionCA's tool; present on every Mac. Module-level for tests.
SELFTEST_TIMEOUT_S = 5.0
PROXY_USER = "acctsw"          # the user name in every SeatProxy URL (http://acctsw:<secret>@…)
PREFLIGHT_CACHE = "seatproxy-preflight.json"
PREFLIGHT_TTL = timedelta(minutes=10)
SWITCH_ENV = "ACCTSW_IN_PLACE_SWITCH"
GATE_ENV = ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN")
GATE_FLAGS = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
# Claude Code's machine-wide policy on macOS: one file plus a drop-in directory of *.json.
# Module-level so tests can point them elsewhere.
MANAGED_SETTINGS = Path("/Library/Application Support/ClaudeCode/managed-settings.json")
MANAGED_SETTINGS_DIR = Path("/Library/Application Support/ClaudeCode/managed-settings.d")
FALSY = ("0", "false", "no", "off")
# Variables that, in a settings ``env`` block, override the ones we set inside Claude Code and so
# defeat the interception (in the process env, HTTPS_PROXY and NODE_EXTRA_CA_CERTS are chained
# and merged instead).
SETTINGS_TLS_ENV = ("NODE_EXTRA_CA_CERTS", "CLAUDE_CODE_CERT_STORE", "HTTPS_PROXY", "https_proxy",
                    "NODE_TLS_REJECT_UNAUTHORIZED")
VERDICT_FILE = "seatswap-ca-verdict.json"
VERDICT_TTL = timedelta(days=7)
VERSION_TIMEOUT_S = 3.0


def _noop(*_a, **_k) -> None:
    pass


def _seat(email: str, token: str | None) -> Seat:
    return Seat(email, token)


# --- gates ------------------------------------------------------------------------------------

def _set(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return value is not None and str(value).strip().lower() not in ("", *FALSY)


def _first_party(url: str) -> bool:
    try:
        parts = urlsplit(url.strip())
        return parts.scheme == "https" and parts.hostname == API_HOST and parts.port in (None, 443)
    except ValueError:
        return False


def _env_gate(env: dict) -> str | None:
    for name in GATE_ENV:
        if _set(env.get(name)) and not (name == "ANTHROPIC_BASE_URL" and _first_party(env[name])):
            return name
    for name in GATE_FLAGS:
        if _flag(env.get(name)):
            return name
    return None


def _settings_files(ctx, cwd: Path | None) -> list[Path]:
    real = ctx._claude_real
    files = [real / "settings.json", real / "settings.local.json"]
    if cwd is not None:
        files += [cwd / ".claude" / "settings.json", cwd / ".claude" / "settings.local.json"]
    files.append(MANAGED_SETTINGS)
    try:
        files += sorted(MANAGED_SETTINGS_DIR.glob("*.json"))
    except OSError:
        pass
    return files


def _cli_settings(args, cwd: Path) -> list[tuple[str, dict | None]]:
    """Every ``--settings <file|json>`` (or ``--settings=…``) in the child's argv, parsed. None
    marks one this module cannot read: it may route Claude elsewhere, so it gates."""
    out = []
    args = [str(a) for a in (args or ())]
    for i, arg in enumerate(args):
        if arg == "--settings":
            value = args[i + 1] if i + 1 < len(args) else ""
        elif arg.startswith("--settings="):
            value = arg.split("=", 1)[1]
        else:
            continue
        text = value.strip()
        try:
            if text.startswith("{"):
                data = json.loads(text)
            else:
                path = Path(text).expanduser()
                data = json.loads((path if path.is_absolute() else cwd / path).read_text(
                    encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        out.append(("--settings", data if isinstance(data, dict) else None))
    return out


def _settings_gate(where: str, data: dict) -> str | None:
    if _set(data.get("apiKeyHelper")):
        return f"{where} apiKeyHelper"
    env = data.get("env")
    found = (_env_gate(env) or _settings_tls_gate(env)) if isinstance(env, dict) else None
    return f"{where} env {found}" if found else None


def _settings_tls_gate(env: dict) -> str | None:
    """A settings ``env`` entry that overrides our proxy or CA variables inside Claude Code."""
    for name in SETTINGS_TLS_ENV:
        if env.get(name) is not None and str(env[name]).strip():
            return name
    for name in ("NO_PROXY", "no_proxy"):
        entries = [e.strip() for e in str(env.get(name) or "").split(",")]
        if any(e == "*" or _routes_api(e) for e in entries):
            return name
    return None


def _claude_key(ctx, environ) -> str | None:
    """Identifies the claude binary a verdict is about: its realpath plus ``claude --version``
    (path alone if the version can't be had in time). The binary is the one the launcher runs
    (``ctx.claude_bin``), else the first on PATH; None when there is none."""
    found = getattr(ctx, "claude_bin", None) or shutil.which(
        "claude", path=environ.get("PATH") or os.defpath)
    if not found:
        return None
    real = os.path.realpath(found)
    try:
        out = subprocess.run([real, "--version"], capture_output=True, text=True,
                             timeout=VERSION_TIMEOUT_S, stdin=subprocess.DEVNULL).stdout.strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        out = ""
    return f"{real}\n{out}" if out else real


def _verdicts(ctx) -> dict:
    return _read_settings(Path(ctx.data_dir) / VERDICT_FILE)


def _rejected_before(ctx, environ) -> bool:
    """Whether this claude binary refused the session CA within ``VERDICT_TTL``."""
    entries = _verdicts(ctx)
    if not any(_live(v) for v in entries.values()):
        return False   # nothing to match: don't spawn ``claude --version`` on every launch
    key = _claude_key(ctx, environ)
    return key is not None and _live(entries.get(key))


def _live(stamp) -> bool:
    at = parse_iso(stamp) if isinstance(stamp, str) else None
    return at is not None and timedelta(0) <= now() - at < VERDICT_TTL


def _remember_rejection(ctx, environ) -> None:
    key = _claude_key(ctx, environ)
    if key is None:
        return
    entries = {k: v for k, v in _verdicts(ctx).items() if _live(v)}
    entries[key] = iso(now())
    try:
        write_json(Path(ctx.data_dir) / VERDICT_FILE, entries, mode=0o600)
    except OSError:
        pass   # an unremembered verdict only costs one more try


def _read_settings(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _nested(environ) -> bool:
    """Whether this launcher runs inside a child another SeatSwap already proxies: its inherited
    HTTPS proxy is one of ours (user ``acctsw`` on 127.0.0.1)."""
    for name in ("HTTPS_PROXY", "https_proxy"):
        try:
            parts = urlsplit(str(environ.get(name) or "").strip())
            if parts.username == PROXY_USER and parts.hostname == "127.0.0.1":
                return True
        except ValueError:
            continue
    return False


def gate(ctx, environ=os.environ, *, cwd: Path | str | None = None, args=()) -> str | None:
    """Why in-place switching is off for a run started here, or None when it may run. ``args``
    is the child's argv (its ``--settings`` counts like any settings file)."""
    switch = str(environ.get(SWITCH_ENV, "")).strip()
    if switch.lower() in FALSY:
        return f"{SWITCH_ENV}={switch}"
    if not ctx.load_state().settings().get("in_place_switch", True):
        return "setting off"
    if not os.access(OPENSSL, os.X_OK):
        return "openssl missing"   # no session CA, so no interception
    if _nested(environ):
        # The outer session's proxy already serves this one; a second proxy (and token
        # injection) inside it would fight over which seat gets billed. Leave the env alone.
        return "nested in another acctsw session"
    found = _env_gate(environ)
    if found:
        return f"env {found}"
    for name in ("NO_PROXY", "no_proxy"):
        if "*" in [e.strip() for e in str(environ.get(name) or "").split(",")]:
            return "NO_PROXY=* bypasses the proxy"
    if _rejected_before(ctx, environ):
        return "client rejected the session certificate"
    cwd = Path(cwd) if cwd is not None else Path.cwd()
    for where, data in _cli_settings(args, cwd):
        if data is None:
            return f"{where} unreadable"
        found = _settings_gate(where, data)
        if found:
            return found
    for path in _settings_files(ctx, cwd):
        found = _settings_gate(path.name, _read_settings(path))
        if found:
            return found
    return None


def enabled(ctx, environ=os.environ, *, cwd: Path | str | None = None, args=()) -> bool:
    return gate(ctx, environ, cwd=cwd, args=args) is None


# --- eligibility --------------------------------------------------------------------------------

def _oauth(blob: str | None) -> dict:
    try:
        data = json.loads(blob or "")
    except (ValueError, TypeError):
        return {}
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    return oauth if isinstance(oauth, dict) else {}


class _Unreadable(Exception):
    """The Keychain (or a home directory) could not be read: no verdict either way."""


def _load(ctx, email: str) -> str | None:
    from . import claudehome, usage
    try:
        # Eligibility and maintenance classify whether this login can still serve a session.
        # A denied read is unknown, not an absent token that warrants moving or restarting it.
        return claudehome.load(ctx, email, strict=True)
    except usage.UNREADABLE as exc:
        raise _Unreadable(str(exc)) from None


def eligible(ctx, email: str, *, home_email: str | None,
             allow_tier: bool | None = None) -> str | None:
    """The access token to bill ``email`` in place of ``home_email``, or None if it can't be.
    An unreadable Keychain is None too: nothing proves the seat can serve. ``allow_tier``:
    True lets a different plan tier through, False refuses it, None asks ``seattier.allowed``
    (the setting and the pair approvals)."""
    try:
        return _check(ctx, email, home_email, allow_tier)[0]
    except _Unreadable:
        return None


def eligible_reason(ctx, email: str, *, home_email: str | None,
                    allow_tier: bool | None = None) -> str | None:
    """Why ``eligible`` is None: ``"tier"`` when only the plan tier blocks it (the caller may
    ask the user), else ``"seat"`` (quarantined, no token, expiring) or ``"unreadable"``.
    None when the seat is eligible."""
    try:
        return _check(ctx, email, home_email, allow_tier)[1]
    except _Unreadable:
        return "unreadable"


def _eligible(ctx, email: str, *, home_email: str | None,
              allow_tier: bool | None = None) -> str | None:
    return _check(ctx, email, home_email, allow_tier)[0]


def _check(ctx, email: str, home_email: str | None,
           allow_tier: bool | None) -> tuple[str | None, str | None]:
    """``(token, None)`` or ``(None, reason)``."""
    from .usage import claude_token
    if (ctx.claude_home(email) / ".acctsw-quarantined").exists():
        return None, "seat"
    blob = _load(ctx, email)
    token = claude_token(blob) if blob else None
    if not isinstance(token, str) or not token:
        return None, "seat"
    expiry = _oauth(blob).get("expiresAt")
    if (not isinstance(expiry, (int, float)) or isinstance(expiry, bool)
            or expiry <= (now() + MIN_TTL).timestamp() * 1000):
        return None, "seat"
    if home_email and email != home_email:
        tier, home_tier = seattier.tier(blob), seattier.tier(_load(ctx, home_email))
        if tier and home_tier and tier != home_tier:
            ok = allow_tier if allow_tier is not None else seattier.allowed(
                ctx.load_state(), TOOL, tier, home_tier)
            if not ok:
                return None, "tier"
    return token, None


# --- the per-run glue ---------------------------------------------------------------------------

@dataclass(eq=False)
class _Ask:
    hit: Any
    seat: Any = None
    done: bool = False


def _default_proxy(decide, *, decide_timeout: float, log, upstream: str = UPSTREAM, **transport):
    from .seatproxy import SeatProxy
    return SeatProxy(decide, upstream=upstream, decide_timeout=decide_timeout, log=log, **transport)


def _default_ca():
    from .sessionca import SessionCA
    return SessionCA((API_HOST,), openssl=OPENSSL)


def _upstream_proxy(environ) -> str | None:
    """The proxy the user's own traffic to api.anthropic.com went through, if any: ours chains
    to it, since HTTPS_PROXY now points the child at us."""
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        if _set(environ.get(name)):
            return environ[name].strip()
    return None


def _routes_api(entry: str) -> bool:
    """Whether a NO_PROXY entry would send api.anthropic.com around the proxy."""
    host = entry.strip().lower()
    if host.startswith("[") or host.count(":") > 1:
        return False   # an IPv6 literal, never a DNS name
    host = host.split(":", 1)[0].lstrip("*").lstrip(".")
    return bool(host) and (API_HOST == host or API_HOST.endswith("." + host))


class SeatSwap:
    def __init__(self, ctx, home_email: str | None = None, *, proxy=None, proxy_factory=None,
                 ca=None, ca_factory=None, environ=None,
                 decide_timeout: float = 10.0, clock: Callable[[], float] = time.monotonic,
                 log: Callable[..., None] = _noop, upstream: str = UPSTREAM):
        self._ctx = ctx
        self.home = home_email
        self._timeout = decide_timeout
        self._clock = clock
        self._log = log
        self._cv = threading.Condition()
        self._asks: list[_Ask] = []
        self._closed = False
        self._next_read = 0.0
        self._handover = False
        self.third_seat_moves = 0
        self.upstream = upstream
        self.proxy_url: str | None = None
        self._tls_rejected = False
        self._upstream_worked = False
        self._preflight_marked = False
        self._environ = os.environ if environ is None else environ
        self.upstream_proxy = _upstream_proxy(self._environ)
        r, w = os.pipe()
        os.set_blocking(r, False)
        os.set_blocking(w, False)
        self.wake_fd, self._wake_w = r, w
        self._stopped = False
        self.ca = ca
        self.proxy = None
        try:
            if proxy is not None:
                self.proxy = proxy
            else:
                if self.ca is None:   # SessionCAError (no openssl, a failing one) propagates
                    self.ca = (ca_factory or _default_ca)()
                transport = {"ca": self.ca, "upstream_proxy": self.upstream_proxy,
                             "on_tls_reject": self._on_tls_reject}
                if proxy_factory is not None:
                    self.proxy = proxy_factory(self._decide, decide_timeout=decide_timeout,
                                               log=log, **transport)
                else:
                    if upstream != UPSTREAM:
                        transport["upstream"] = upstream
                    self.proxy = _default_proxy(self._decide, decide_timeout=decide_timeout,
                                                log=log, **transport)
            self.start()
        except BaseException:
            self._close_fds()
            if self.ca is not None and ca is None:
                self.ca.close()
            raise

    # lifecycle ---------------------------------------------------------------------------------
    def start(self) -> str:
        """Start the proxy (the constructor already did; repeat calls return its URL)."""
        if not self.proxy_url:
            self.proxy_url = self.proxy.start()
            if self.home:
                self.proxy.bind(_seat(self.home, None))
        return self.proxy_url

    def _close_fds(self) -> None:
        for fd in (self.wake_fd, self._wake_w):
            try:
                os.close(fd)
            except OSError:
                pass

    def stop(self) -> None:
        with self._cv:
            if self._stopped:
                return
            self._stopped = self._closed = True
            self._cv.notify_all()
        try:
            self.proxy.stop()
        finally:
            try:
                if self.ca is not None:
                    self.ca.close()
            finally:
                self._close_fds()

    def env(self, base: dict) -> dict:
        """The child's environment. HTTPS goes through the proxy (HTTPS_PROXY), which intercepts
        only api.anthropic.com and tunnels every other host untouched; the child trusts the
        session CA through its CA bundle variables. No ANTHROPIC_BASE_URL: Claude Code keeps
        talking to api.anthropic.com as itself. Nothing else the user set is removed."""
        if not self.proxy_url:
            raise RuntimeError("SeatSwap.env() before start()")
        out = dict(base)
        out["HTTPS_PROXY"] = out["https_proxy"] = self.proxy_url
        if self.ca is not None:
            out.update(self.ca.bundle_paths(base))
        parts: list[str] = []
        for name in ("NO_PROXY", "no_proxy"):
            for p in str(out.get(name) or "").split(","):
                p = p.strip()
                if p and p not in parts and not _routes_api(p):
                    parts.append(p)
        parts += [h for h in ("127.0.0.1", "localhost") if h not in parts]
        out["NO_PROXY"] = out["no_proxy"] = ",".join(parts)
        return out

    def _on_tls_reject(self, reason: str) -> None:
        """Proxy thread: the child refused the session CA. The launcher stops using the swap."""
        with self._cv:
            if self._tls_rejected or self._closed:
                return
            self._tls_rejected = True
            self._log("seatswap: client rejected the session CA", reason=str(reason)[:200])
            try:
                os.write(self._wake_w, b"!")
            except OSError:
                pass
        # Only when no intercepted handshake ever succeeded: a later reject is another tool, not
        # Claude Code refusing our CA. Outside the lock: ``claude --version`` can take a moment.
        if self.proxy.intercepts_ok() == 0:
            _remember_rejection(self._ctx, self._environ)

    def tls_rejected(self) -> bool:
        return self._tls_rejected

    def intercepts_ok(self) -> int:
        """Successful intercepted TLS handshakes (the proxy's count)."""
        return self.proxy.intercepts_ok()

    def upstream_failing(self, n: int = 3) -> bool:
        """The upstream has failed ``n`` times running and nothing has ever got through it.
        "Ever" is a latch on ``proxy.upstream_successes() > 0`` (the failure streak alone resets
        on any success). The first time this is true the current preflight cache entry is marked
        failed, so the next launches don't reuse a stale "ok"."""
        if self.proxy.upstream_successes() > 0:
            self._upstream_worked = True
        failing = self.proxy.upstream_failures() >= n and not self._upstream_worked
        if failing and not self._preflight_marked:
            self._preflight_marked = True
            self._cache_preflight("upstream failing: no request succeeded")
        return failing

    def set_home(self, email: str) -> None:
        """After a restart onto ``email``'s own login: that seat is home and passthrough again."""
        self.home = email
        self._handover = False
        self._next_read = 0.0
        self.proxy.bind(_seat(email, None))

    def bind(self, seat) -> None:
        """Rebind without a pending hit (a manual or approved move, a limit seen elsewhere)."""
        self.proxy.bind(seat)

    def bound(self) -> str | None:
        """The email requests are billed to now; None before anything was bound."""
        return self.proxy.bound().email or None

    def preflight(self) -> str | None:
        """Can the proxy reach the API (TLS included)? None when it can, else a short reason.
        The verdict is cached per upstream for ``PREFLIGHT_TTL`` in the data dir, so starting a
        terminal doesn't cost a TLS handshake each time."""
        key = self._preflight_key()
        path = Path(self._ctx.data_dir) / PREFLIGHT_CACHE
        try:
            cache = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
        cache = cache if isinstance(cache, dict) else {}
        entry = cache.get(key)
        at = parse_iso(entry.get("at")) if isinstance(entry, dict) else None
        local = self._ca_selftest()
        if local:
            return local
        if at is not None and timedelta(0) <= now() - at < PREFLIGHT_TTL:
            return None if entry.get("ok") is True else str(entry.get("reason") or "preflight failed")
        reason = self.proxy.preflight()
        self._cache_preflight(reason)
        return reason

    def _preflight_key(self) -> str:
        env = self._environ
        return json.dumps([self.upstream, self.upstream_proxy,
                           str(env.get("NO_PROXY") or "") + "|" + str(env.get("no_proxy") or ""),
                           str(env.get("SSL_CERT_FILE") or ""),
                           str(env.get("NODE_EXTRA_CA_CERTS") or "")])

    def _cache_preflight(self, reason: str | None) -> None:
        path = Path(self._ctx.data_dir) / PREFLIGHT_CACHE
        try:
            cache = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
        cache = cache if isinstance(cache, dict) else {}
        cache[self._preflight_key()] = {"ok": reason is None, "reason": reason, "at": iso(now())}
        try:
            write_json(path, cache, mode=0o600)
        except OSError:
            pass   # an uncached verdict is still a verdict

    def _ca_selftest(self) -> str | None:
        """One TLS handshake, in process over a socketpair (no port), between the proxy's server
        context and a client that trusts only the session CA, for api.anthropic.com."""
        if self.ca is None:
            return None
        server_sock, client_sock = socket.socketpair()
        failure: list[str] = []
        try:
            server_sock.settimeout(SELFTEST_TIMEOUT_S)
            client_sock.settimeout(SELFTEST_TIMEOUT_S)
            client_ctx = ssl.create_default_context(cadata=self.ca.ca_pem())   # nothing else
            client_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            server_ctx = self.ca.server_context()

            def serve():
                try:
                    server_ctx.wrap_socket(server_sock, server_side=True).close()
                except (OSError, ssl.SSLError) as exc:
                    failure.append(f"server: {exc.__class__.__name__}")
            worker = threading.Thread(target=serve, daemon=True)
            worker.start()
            try:
                client_ctx.wrap_socket(client_sock, server_hostname=API_HOST).close()
            except ssl.SSLCertVerificationError as exc:
                return f"local ca: {getattr(exc, 'verify_message', None) or exc}"[:200]
            except (OSError, ssl.SSLError) as exc:
                return f"local ca: {exc.__class__.__name__}: {exc}"[:200]
            finally:
                worker.join(SELFTEST_TIMEOUT_S)
        except (OSError, ssl.SSLError, ValueError) as exc:   # ValueError: unparseable CA PEM
            return f"local ca: {exc.__class__.__name__}: {exc}"[:200]
        finally:
            server_sock.close()
            client_sock.close()
        return f"local ca: {failure[0]}" if failure else None

    def proxy_used(self) -> bool:
        """Has the child sent a /v1/messages request through the proxy yet?"""
        return self.proxy.messages_seen() > 0

    # proxy thread -> main thread ----------------------------------------------------------------
    def _decide(self, hit):
        """Runs on the proxy thread: hand the hit to the launcher and wait for its seat."""
        ask = _Ask(hit)
        with self._cv:
            # Under the cv: stop() flips _closed under it before closing the pipe, so this write
            # can never land on a closed (or reused) descriptor.
            if self._closed:
                return None
            self._asks.append(ask)
            try:
                os.write(self._wake_w, b"!")
            except OSError:
                pass  # the pipe is full: a wake is already pending
            self._cv.wait_for(lambda: ask.done or self._closed, timeout=self._timeout)
            if ask in self._asks:
                self._asks.remove(ask)
            if not ask.done or self._closed:
                ask.done = True   # a late answer() is refused: the restart path owns this hit now
                return None
            return ask.seat

    def pending(self) -> list:
        try:
            while os.read(self.wake_fd, 512):
                pass
        except OSError:
            pass  # drained (EAGAIN) or closed
        with self._cv:
            return [a.hit for a in self._asks if not a.done]

    def answer(self, hit, seat) -> bool:
        """Reply to a pending hit: ``seat`` to replay on (and bind), None to pass it through."""
        with self._cv:
            ask = next((a for a in self._asks if a.hit is hit and not a.done), None)
            if ask is None:
                ask = next((a for a in self._asks if a.hit == hit and not a.done), None)
            if ask is None:
                return False
            if seat is not None:
                self.proxy.bind(seat)
            ask.seat, ask.done = seat, True
            self._cv.notify_all()
            return True

    # decisions -----------------------------------------------------------------------------------
    def pick(self, ctx, leaving: str, *, exclude=(), consume: bool = True,
             third_seat: bool = True):
        """Where a session billed to ``leaving`` goes next in place: home when it is usable, else
        the next seat ``choose`` offers that is also ``eligible`` and needs no unapproved
        crossing. A third seat only with ``third_seat`` and both ``auto_switch`` and
        ``in_place_switch`` on, read now. None when nothing qualifies. With ``consume`` a "once"
        approval is spent in the same locked write that confirms it is still there."""
        at = now()
        state = ctx.load_state()
        seats = set(state.accounts(TOOL))
        skip = set(exclude) | {leaving}
        target = None
        if self.home not in skip and self.home in seats:
            home = choose(state, TOOL, at, exclude=seats - {self.home}, origin=leaving,
                          home=self.home)
            if home.available and home.email == self.home:
                return _seat(self.home, None)   # the session's own login: never a crossing
            skip.add(self.home)
        settings = state.settings()
        if not (third_seat and settings.get("auto_switch", True)
                and settings.get("in_place_switch", True)):
            return None
        while target is None:
            sel = choose(state, TOOL, at, exclude=skip, origin=leaving, home=self.home)
            if not (sel.available and sel.email):
                return None
            token = eligible(ctx, sel.email, home_email=self.home)   # Keychain: no lock held
            if token:
                target = _seat(sel.email, token)
            skip.add(sel.email)
        if not seatkind.crosses(state, TOOL, leaving, target.email, home=self.home):
            return target
        with ctx.locked():
            state = ctx.load_state()
            if consume:
                if not seatkind.consume(state, TOOL, leaving, target.email, now(), home=self.home):
                    return None
                state.save()
            elif not seatkind.approved(state, TOOL, leaving, target.email, now(), home=self.home):
                return None
        return target

    def maintain(self, ctx=None, *, third_seat: bool = True) -> str | None:
        """Keep a borrowed seat's token current. ``"handover"`` asks for one restart, now.
        Rebinding a renewed token and going home are always allowed; moving to a third seat
        also needs ``third_seat`` (the caller's hop budget) and both settings on. Such a move
        counts in ``third_seat_moves``."""
        ctx = ctx or self._ctx
        self.upstream_failing()   # keeps the "upstream has worked" latch current
        seat = self.proxy.bound()
        if seat.token is None or seat.email == self.home:
            self._handover = False
            return None   # home: pure passthrough, Claude renews its own login
        if self._handover:
            return self._handover_if_idle()
        t = self._clock()
        if t < self._next_read:
            return None
        self._next_read = t + MAINTAIN_EVERY_S
        try:
            fresh = _eligible(ctx, seat.email, home_email=self.home)
        except _Unreadable as exc:
            # A locked or failing Keychain says nothing about the seat: keep serving it and look
            # again next minute. Never move or restart a session on a read error.
            self._log("seatswap: keychain unreadable, binding kept", seat=seat.email,
                      error=type(exc).__name__)
            return None
        if fresh:
            if fresh != seat.token:
                self.proxy.bind(_seat(seat.email, fresh))
                self._log("seatswap: newer token rebound", seat=seat.email)
            return None
        target = self.pick(ctx, seat.email, third_seat=third_seat)
        if target is not None:
            if target.email != self.home:
                self.third_seat_moves += 1
            self.proxy.bind(target)
            self._log("seatswap: token lapsing, moved", seat=seat.email, to=target.email)
            return None
        self._handover = True
        return self._handover_if_idle()

    def _handover_if_idle(self) -> str | None:
        if self.proxy.in_flight() == 0 and self.proxy.idle_for() >= HANDOVER_IDLE_S:
            self._handover = False
            return "handover"
        return None
