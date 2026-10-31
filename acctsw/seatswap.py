"""Main-thread glue for in-place seat switching: Claude keeps running while its requests move seats.

The launcher owns one ``SeatSwap`` per supervised Claude run. Its ``SeatProxy`` (``seatproxy``,
I/O only) sits between the child and the API on 127.0.0.1; the child keeps its own home-seat
login, and the proxy swaps in another seat's access token only while that seat is bound. Every
decision is made here, on the launcher's thread:

- the proxy thread posts a ``Hit`` (a limit or a 401) and blocks up to ``decide_timeout`` seconds;
  a byte on ``wake_fd`` wakes the launcher's select loop, which reads ``pending()`` and replies
  with ``answer(hit, seat)``. No reply in time means the proxy passes the response through and the
  ordinary restart path takes over (a late ``answer`` is refused);
- ``maintain()`` re-reads the bound seat's Keychain item at most once a minute: a newer token is
  rebound; a token about to lapse moves the session home, else to another seat, else asks for one
  "handover" restart once the proxy is idle. This app never refreshes a token itself.

Gates (``gate``/``enabled``): the setting ``in_place_switch``, ``ACCTSW_IN_PLACE_SWITCH=0``, and any
configuration that already routes or authenticates Claude differently (env or a Claude
settings.json ``env`` naming a base URL, an API key or token, Bedrock/Vertex/Foundry, or an
``apiKeyHelper``). Everything this module can't serve falls back to the restart path.

Eligibility (``eligible``): the seat's Keychain item (quarantined homes have none), its access
token, and a stated ``expiresAt`` more than ``MIN_TTL`` ahead — an unknown expiry is not eligible —
and the same ``subscriptionType`` as the home seat when both are known. Crossings between business
and personal seats follow ``seatkind``: never without an approval for that direction.

Tokens live in memory only and are never logged.

Additions to the frozen interface (nothing in it changed): ``gate()`` names why the swap is off;
``SeatSwap(ctx, home_email=None, proxy=..., proxy_factory=...)`` starts the proxy at once (an
``OSError`` from its bind propagates; the launcher then restarts instead); ``stop()`` is
idempotent; ``bind(seat)`` rebinds without a hit; ``bound()`` is the billed seat's email (None
before any bind); ``answer()`` returns whether the reply was accepted; ``pick()`` chooses (and by
default spends the approval for) the next seat, by the same rules ``maintain`` uses.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from . import seatkind
from .selection import choose
from .util import now

TOOL = "claude"
MIN_TTL = timedelta(minutes=15)
MAINTAIN_EVERY_S = 60.0     # at most one Keychain read of the bound seat per minute
HANDOVER_IDLE_S = 5.0       # a handover restart waits for this much proxy silence
SWITCH_ENV = "ACCTSW_IN_PLACE_SWITCH"
GATE_ENV = ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN")
GATE_FLAGS = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
# Claude Code's machine-wide policy file on macOS. Module-level so tests can point it elsewhere.
MANAGED_SETTINGS = Path("/Library/Application Support/ClaudeCode/managed-settings.json")


def _noop(*_a, **_k) -> None:
    pass


# The proxy module lands separately; these mirror its frozen shapes until it does.
@dataclass(frozen=True)
class _Seat:
    email: str
    token: str | None


@dataclass(frozen=True)
class _Hit:
    kind: str
    seat: str
    reset_at: str | None
    detail: str | None
    claim: str | None


def _types():
    try:
        from .seatproxy import Hit, Seat
    except ImportError:
        return _Seat, _Hit
    return Seat, Hit


def _seat(email: str, token: str | None):
    return _types()[0](email, token)


# --- gates ------------------------------------------------------------------------------------

def _set(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return value is not None and str(value).strip().lower() not in ("", "0", "false", "no", "off")


def _env_gate(env: dict) -> str | None:
    for name in GATE_ENV:
        if _set(env.get(name)):
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
    return files


def _read_settings(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def gate(ctx, environ=os.environ, *, cwd: Path | str | None = None) -> str | None:
    """Why in-place switching is off for a run started here, or None when it may run."""
    if str(environ.get(SWITCH_ENV, "")).strip() == "0":
        return f"{SWITCH_ENV}=0"
    if not ctx.load_state().settings().get("in_place_switch", True):
        return "setting off"
    found = _env_gate(environ)
    if found:
        return f"env {found}"
    cwd = Path(cwd) if cwd is not None else Path.cwd()
    for path in _settings_files(ctx, cwd):
        data = _read_settings(path)
        if _set(data.get("apiKeyHelper")):
            return f"{path.name} apiKeyHelper"
        env = data.get("env")
        found = _env_gate(env) if isinstance(env, dict) else None
        if found:
            return f"{path.name} env {found}"
    return None


def enabled(ctx, environ=os.environ, *, cwd: Path | str | None = None) -> bool:
    return gate(ctx, environ, cwd=cwd) is None


# --- eligibility --------------------------------------------------------------------------------

def _oauth(blob: str | None) -> dict:
    try:
        data = json.loads(blob or "")
    except (ValueError, TypeError):
        return {}
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    return oauth if isinstance(oauth, dict) else {}


def _tier(blob: str | None) -> str | None:
    raw = _oauth(blob).get("subscriptionType")
    return raw.strip().lower() if isinstance(raw, str) and raw.strip() else None


def eligible(ctx, email: str, *, home_email: str) -> str | None:
    """The access token to bill ``email`` in place of ``home_email``, or None if it can't be."""
    from . import claudehome
    from .usage import claude_token
    if (ctx.claude_home(email) / ".acctsw-quarantined").exists():
        return None
    blob = claudehome.load(ctx, email)
    token = claude_token(blob) if blob else None
    if not isinstance(token, str) or not token:
        return None
    expiry = _oauth(blob).get("expiresAt")
    if (not isinstance(expiry, (int, float)) or isinstance(expiry, bool)
            or expiry <= (now() + MIN_TTL).timestamp() * 1000):
        return None
    if home_email and email != home_email:
        tier, home_tier = _tier(blob), _tier(claudehome.load(ctx, home_email))
        if tier and home_tier and tier != home_tier:
            return None
    return token


# --- the per-run glue ---------------------------------------------------------------------------

@dataclass(eq=False)
class _Ask:
    hit: Any
    seat: Any = None
    done: bool = False


def _default_proxy(decide, *, decide_timeout: float, log):
    from .seatproxy import SeatProxy
    return SeatProxy(decide, decide_timeout=decide_timeout, log=log)


class SeatSwap:
    def __init__(self, ctx, home_email: str | None = None, *, proxy=None, proxy_factory=None,
                 decide_timeout: float = 10.0, clock: Callable[[], float] = time.monotonic,
                 log: Callable[..., None] = _noop):
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
        self.base_url: str | None = None
        r, w = os.pipe()
        os.set_blocking(r, False)
        os.set_blocking(w, False)
        self.wake_fd, self._wake_w = r, w
        self._stopped = False
        try:
            self.proxy = proxy if proxy is not None else (proxy_factory or _default_proxy)(
                self._decide, decide_timeout=decide_timeout, log=log)
            self.start()
        except BaseException:
            self._close_fds()
            raise

    # lifecycle ---------------------------------------------------------------------------------
    def start(self) -> str:
        """Start the proxy (the constructor already did; repeat calls return its URL)."""
        if not self.base_url:
            self.base_url = self.proxy.start()
            if self.home:
                self.proxy.bind(_seat(self.home, None))
        return self.base_url

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
            self._close_fds()

    def env(self, base: dict) -> dict:
        """The child's environment: requests go through the proxy, which no HTTP proxy may eat."""
        if not self.base_url:
            raise RuntimeError("SeatSwap.env() before start()")
        out = dict(base)
        out["ANTHROPIC_BASE_URL"] = self.base_url
        for name in ("NO_PROXY", "no_proxy"):
            parts = [p.strip() for p in str(out.get(name) or "").split(",") if p.strip()]
            parts += [h for h in ("127.0.0.1", "localhost") if h not in parts]
            out[name] = ",".join(parts)
        return out

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

    # proxy thread -> main thread ----------------------------------------------------------------
    def _decide(self, hit):
        """Runs on the proxy thread: hand the hit to the launcher and wait for its seat."""
        ask = _Ask(hit)
        with self._cv:
            if self._closed:
                return None
            self._asks.append(ask)
        try:
            os.write(self._wake_w, b"!")
        except OSError:
            pass  # the pipe is full: a wake is already pending
        with self._cv:
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
    def pick(self, ctx, leaving: str, *, exclude=(), consume: bool = True):
        """Where a session billed to ``leaving`` goes next in place: home when it is usable and
        the move needs no unapproved crossing, else the next seat ``choose`` offers that is also
        ``eligible``. None when nothing qualifies. With ``consume`` a "once" approval is spent in
        the same locked write that confirms it is still there."""
        at = now()
        state = ctx.load_state()
        seats = set(state.accounts(TOOL))
        skip = set(exclude) | {leaving}
        target = None
        if self.home not in skip and self.home in seats:
            home = choose(state, TOOL, at, exclude=seats - {self.home}, origin=leaving)
            if home.available and home.email == self.home:
                target = _seat(self.home, None)
            skip.add(self.home)
        while target is None:
            sel = choose(state, TOOL, at, exclude=skip, origin=leaving)
            if not (sel.available and sel.email):
                return None
            token = eligible(ctx, sel.email, home_email=self.home)   # Keychain: no lock held
            if token:
                target = _seat(sel.email, token)
            skip.add(sel.email)
        if not seatkind.crosses(state, TOOL, leaving, target.email):
            return target
        with ctx.locked():
            state = ctx.load_state()
            if consume:
                if not seatkind.consume(state, TOOL, leaving, target.email, now()):
                    return None
                state.save()
            elif not seatkind.approved(state, TOOL, leaving, target.email, now()):
                return None
        return target

    def maintain(self, ctx=None) -> str | None:
        """Keep a borrowed seat's token current. ``"handover"`` asks for one restart, now."""
        ctx = ctx or self._ctx
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
        fresh = eligible(ctx, seat.email, home_email=self.home)
        if fresh:
            if fresh != seat.token:
                self.proxy.bind(_seat(seat.email, fresh))
                self._log("seatswap: newer token rebound", seat=seat.email)
            return None
        target = self.pick(ctx, seat.email)
        if target is not None:
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
