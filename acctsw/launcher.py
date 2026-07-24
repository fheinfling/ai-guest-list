"""Supervised launcher: run an agent, auto-switch on usage limit, resume the same work.

Design (testability): the *decisions* (limit detection, what to do on a limit) are pure functions;
the messy PTY I/O is isolated behind an injectable ``spawn`` callable so ``run()`` is unit-tested
with a scripted fake child (no real PTY, no network).

Flow:
  1. pick a seat (prefer active; else available; else soonest-unlock + report) and switch to it
  2. spawn the agent under a PTY, teeing output while scanning for the limit signal
  3. on a stdout limit-signal: VERIFY BEFORE KILLING — probe the usage endpoint while the child is
     still running; a false positive (the model narrating about limits) is dismissed in place and
     the session lives on. Only a corroborated (or unverifiable) signal stops the child, flags the
     seat (reactive), picks another seat, and relaunches with the tool's RESUME command; repeat
  4. on normal exit: sync-back the (refreshed) creds and return the child's exit status
"""
from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import time
import tty
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from . import usage as usage_mod
from .context import Context
from .errors import AcctswError
from .procenv import harden_env
from .selection import Selection, choose
from .switch import switch, sync_back
from .util import iso, now, parse_iso

# A spawn function: (argv, on_output) -> exit_status.
#   on_output(chunk: bytes) -> bool ; returning True asks the supervisor to stop the child.
SpawnFn = Callable[[list, Callable[[bytes], bool]], int]
Notifier = Callable[[str], None]

# Default cooldown when a limit is caught but no authoritative reset is known (owned by usage so
# its limit-flagging can share it; re-exported here for the handlers and existing callers/tests).
DEFAULT_COOLDOWN = usage_mod.DEFAULT_COOLDOWN
MAX_SWITCHES = 6      # safety bound on auto-relaunches within one `run`
MAX_FALSE_ALARMS = 3  # dismissed stdout matches per run before stdout scanning is switched OFF
                      # (supervision continues — only the untrustworthy signal is dropped)
PROBE_COOLDOWN_S = 30.0  # after a dismissed match, skip re-probing usage for this long: a TUI
                         # redraws the same prose every frame, and without a cooldown each redraw
                         # would force-hit the usage endpoint
# A stdout limit-signal is dismissed as a false positive (model prose, not a real banner) when the
# usage endpoint says the active seat's busiest window is still below this (owned by usage, which
# uses the same bar to clear stale reactive flags; re-exported for _seat_confirmed_healthy/tests).
FALSE_ALARM_MAX_PCT = usage_mod.FALSE_ALARM_MAX_PCT
EXIT_GAVE_UP = 75     # EX_TEMPFAIL: distinguishes "we gave up / all limited" from a child failure
WAIT_ON_ALL_RESTING_ENV = "ACCTSW_WAIT_ON_ALL_RESTING"
_ENV_FALSE = frozenset({"0", "false", "no", "off"})
# A usage-limit exit is an ordinary POSITIVE failure code; a user abort is not. Signal deaths come
# back NEGATIVE (os.waitstatus_to_exitcode), and a tool that catches the signal exits 128+N — so the
# exit-time safety net skips both rather than pay a usage fetch (and delay teardown) on a Ctrl-C/kill.
_ABORT_EXITS = frozenset({129, 130, 131, 143})   # SIGHUP, SIGINT (Ctrl-C), SIGQUIT, SIGTERM

# Limit signals in the agents' output. This buffer ALSO carries the model's own generated prose —
# which, especially when THIS repo is the thing under development, routinely mentions "usage limit"
# and "out of credits" in passing. A false positive kills+restarts a healthy session, so every phrase
# here must be the tool's actual out-of-quota BANNER (committal wording), never a word the model can
# utter mid-sentence. Bare "usage limit" / "limit reached" / "out of credits" are intentionally NOT
# here for exactly this reason. A missed real limit is corroborated separately by the usage endpoint
# (handle_limit), so erring toward specificity here is safe.
_LIMIT_SHARED = [
    r"usage limit (?:reached|exceeded)",
    r"you[''`]?ve (?:hit|reached) your (?:usage )?limit",
    # A limit paired with a reset time IS a committal banner ("usage limit · resets 8pm",
    # "your limit will reset at …"), not mid-sentence prose. Requiring "reset(s)" close after the
    # word "limit" keeps benign mentions out ("approaching the recursion limit", "the cache resets
    # at midnight" — neither has both), and any false positive is still vetoed by the usage endpoint.
    r"\blimit\b[^.\n]{0,20}?\bresets?\b",
    r"rate[ -]?limit(?:ed| reached| exceeded)",
    r"too many requests",
]
LIMIT_PATTERNS = {
    # "out of credits" is real ChatGPT/Codex wording, but it's also exactly what the model narrates
    # about Codex — so it stays out of the CLAUDE list (a Claude session never emits it as a banner).
    "codex": [*_LIMIT_SHARED, r"out of (?:credits?|usage)"],
    # "5-hour limit" / "weekly limit" are Claude's OWN window-limit banners (e.g. a status line
    # "5-hour limit · resets 8pm" that omits "reached"). They're specific enough to rarely appear in
    # the model's prose, and the corroboration guard (handle_limit) vetoes any that slip through — so
    # we keep them loose here to catch the real banner regardless of its exact committal wording.
    "claude": [*_LIMIT_SHARED, r"5-?hour limit", r"weekly limit"],
}

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# A transient SERVER-side throttle (HTTP 429/529 overload) is NOT the account's usage limit — Claude
# Code says so verbatim: "Server is temporarily limiting requests (not your usage limit) · Rate
# limited". Switching or resting a seat for a server blip is a false positive (the same class that
# killed healthy sessions), so an explicit disclaimer vetoes an otherwise-matching limit phrase.
_NOT_A_LIMIT = re.compile(r"not (?:your|a) usage limit|temporarily limiting requests", re.IGNORECASE)

# Claude Code prints its OWN pre-limit WARNING (a heads-up, NOT an out-of-quota HIT) at startup and
# mid-session: "Approaching your 5-hour limit", "Approaching weekly limit". That benign status trips
# the deliberately loose "5-hour limit"/"weekly limit" patterns. Without a veto it would classify as a
# real limit — and because an "approaching" warning only appears in the 90-100% band, the usage-probe
# fallback (healthy only BELOW FALSE_ALARM_MAX_PCT=90) cannot dismiss it, so a still-usable seat gets
# rested for a full cooldown and a switch is burned. The veto is scoped to the MATCHED LINE (see
# _is_limit), not the whole rolling buffer: a real hit on another line ("5-hour limit reached") still
# fires, and unrelated buffer text ("hit enter") can't defeat it. A line saying "approaching … limit"
# is by definition not-yet-reached, so vetoing exactly those lines cannot mask a genuine hit.
_CLAUDE_APPROACHING = re.compile(r"approaching\b[^.\n]{0,40}\blimit", re.IGNORECASE)

# Auth-death signals (token revoked / signed out). DISTINCT from a usage limit: switching+resuming on
# the SAME seat can't help — the seat needs re-login — so we hop to a DIFFERENT seat. Kept to the
# tools' SPECIFIC error wording (not loose phrases like "sign in again") because this buffer also
# carries the model's own generated text; a loose match would kill a healthy session on benign output.
# Shared base + per-tool extras (one source of truth, so the common patterns can't drift apart).
_AUTH_DEAD_SHARED = [r"refresh token (?:was |is )?revoked", r"log ?out and sign in again"]
AUTH_DEAD_PATTERNS = {
    "codex": _AUTH_DEAD_SHARED,
    "claude": [*_AUTH_DEAD_SHARED, r"oauth token (?:has )?expired"],
}

# Compile once at import — detect_* runs once per PTY output chunk (a hot interactive path).
_LIMIT_RE = {t: [re.compile(p, re.IGNORECASE) for p in pats] for t, pats in LIMIT_PATTERNS.items()}
_AUTH_RE = {t: [re.compile(p, re.IGNORECASE) for p in pats] for t, pats in AUTH_DEAD_PATTERNS.items()}

# Codex can emit this hard billing banner when the ChatGPT workspace has no credits left. It is not
# always reflected as a normal usage-window reset, so waiting for usage API corroboration can trap the
# launcher in a same-seat resume loop. Keep this deliberately narrower than generic "out of credits"
# prose, which is still guarded by the usage probe.
HARD_LIMIT_PATTERNS = {
    "codex": [
        r"(?m)^[^\w\n]{0,8}\s*your\s+workspace\s+is\s+out\s+of\s+credits?\.?"
        r"\s+add\s+credits\s+to\s+continue\.?\s*$",
    ],
    "claude": [],
}
_HARD_LIMIT_RE = {
    t: [re.compile(p, re.IGNORECASE) for p in pats]
    for t, pats in HARD_LIMIT_PATTERNS.items()
}


# Classification on ALREADY-ANSI-stripped text — the single source of truth for the limit veto and
# the pattern scans, so detect_limit/detect_auth_dead (the public probes) and detect_event (the hot
# per-chunk path) can never drift apart. Callers own the one _ANSI.sub so no strip is done twice.
def _is_limit(tool: str, clean: str) -> bool:
    if _NOT_A_LIMIT.search(clean):
        return False  # server throttle explicitly disclaims the usage limit → not a limit banner
    for rx in _LIMIT_RE[tool]:
        for m in rx.finditer(clean):
            if tool == "claude":
                # Veto ONLY if THIS match sits on a benign "approaching … limit" line (a heads-up).
                # Scoping to the match's own line — not the whole buffer — means a real hit elsewhere
                # ("5-hour limit reached") still fires and stray words ("hit enter") can't defeat it.
                ls = clean.rfind("\n", 0, m.start()) + 1
                le = clean.find("\n", m.end())
                line = clean[ls:le if le != -1 else len(clean)]
                if _CLAUDE_APPROACHING.search(line):
                    continue
            return True
    return False


def _is_auth_dead(tool: str, clean: str) -> bool:
    return any(rx.search(clean) for rx in _AUTH_RE[tool])


def detect_limit(tool: str, text: str) -> bool:
    """True if ``text`` (a rolling buffer of recent output) looks like a usage-limit message.

    ANSI escape codes are stripped first so a TUI's color codes can't split a phrase.
    """
    return _is_limit(tool, _ANSI.sub("", text))


def detect_auth_dead(tool: str, text: str) -> bool:
    """True if recent output says the seat's credentials are dead (revoked / signed out)."""
    return _is_auth_dead(tool, _ANSI.sub("", text))


def detect_hard_limit(tool: str, text: str) -> bool:
    """True for a trusted tool-side limit banner that does not need usage API corroboration."""
    clean = _ANSI.sub("", text)
    return any(rx.search(clean) for rx in _HARD_LIMIT_RE[tool])


def detect_event(tool: str, text: str) -> str | None:
    """Classify a PTY output chunk in ONE ANSI strip (the hot path runs this per chunk): returns
    "auth" (creds dead), "limit" (usage limit), or None. Auth is checked first — it's not a usage
    limit and needs a DIFFERENT seat, not a resume on the same one."""
    clean = _ANSI.sub("", text)
    if _is_auth_dead(tool, clean):
        return "auth"
    if _is_limit(tool, clean):
        return "limit"
    return None


class NoSeats(AcctswError):
    """No seats configured for a tool."""


def _wait_on_all_resting_enabled(environ: dict[str, str] | None = None) -> bool:
    environ = os.environ if environ is None else environ
    return environ.get(WAIT_ON_ALL_RESTING_ENV, "1").strip().lower() not in _ENV_FALSE


POLL_INTERVAL_S = 300.0  # while waiting on resting seats, wake at least this often to re-check
POLL_MIN_FETCH_S = 240   # unforced in-wait polls: per-seat floor between usage fetches


def _verify_capacity(ctx: Context, tool: str, get, *, at, force: bool,
                     exclude: set | frozenset = frozenset(), ua: str | None = None,
                     trust_reactive_lag: bool = True) -> Selection:
    """Fresh ground truth before blocking or giving up: fetch usage for the tool's seats, persist
    the results (store_fetch → _apply_limit, which clears flags a confirmed-healthy fetch disproves
    and re-stamps ones it confirms), drop rest markers that have expired by ``at``, and return a
    fresh choose(). Local ``limited_until`` flags alone are NOT trusted — a stale reactive flag from
    a false positive is exactly what wrongly announced "all seats resting" when capacity existed.

    Lock discipline (same as _probe / store_fetch's contract): the state flock is held only for the
    quick reads/writes on either side — NEVER across the network fetches. ``force=False`` polls are
    gated per-seat by usage._due with a POLL_MIN_FETCH_S floor, which includes the endpoint's
    exponential error backoff — sustained 429s stretch a seat's polls instead of hammering it."""
    with ctx.locked():
        state = ctx.load_state()
        pending = []
        for email in list(state.accounts(tool)):
            if email in exclude:
                continue   # auth-dead this run: choose() skips it, so its fetch is a wasted 401
            prev = (state.get_seat(tool, email) or {}).get("usage") or {}
            if not force and not usage_mod._due(prev, at, POLL_MIN_FETCH_S):
                continue
            blob = usage_mod._seat_blob(ctx, state, tool, email)
            if blob:
                pending.append((email, blob))
    results = []
    for email, blob in pending:   # network — no lock held
        try:
            results.append((email, usage_mod._fetch_for(tool, blob, get, ua)))
        except Exception:
            pass  # a broken blob/transport must not kill the wait — the seat just isn't refreshed
    with ctx.locked():
        state = ctx.load_state()
        changed = False
        for email, u in results:
            if state.get_seat(tool, email) is not None:   # seat may have been removed mid-wait
                usage_mod.store_fetch(state, tool, email, u, at=at,
                                      trust_reactive_lag=trust_reactive_lag)
                changed = True
        # Trust the clock for markers the fetches did not re-stamp: clear EVERY seat whose rest has
        # expired by ``at`` (the old wait cleared only the one chosen seat, leaving stale siblings).
        for email, seat in state.accounts(tool).items():
            until = parse_iso(seat.get("limited_until"))
            if until is not None and until <= at:
                state.set_limited_until(tool, email, None)
                changed = True
        if changed:
            state.save()   # skip the fsync'd rewrite on idle polls (nothing fetched, nothing expired)
        return choose(state, tool, at=at, exclude=exclude)


def _wait_for_unlock(ctx: Context, tool: str, notify: Notifier,
                     sleep: Callable[[float], None], get=usage_mod._default_get,
                     exclude: set | frozenset = frozenset(), cold_start: bool = False) -> str | None:
    """Verify-then-poll until a seat is actually usable. Returns the seat's email, or None to give up.

    Never sleeps on stored flags alone: a FORCED verify sweep runs first, so a launch against stale
    rest markers starts immediately instead of announcing "all seats resting". While waiting it
    wakes every POLL_INTERVAL_S to re-check (unforced, backoff-gated) and can resume EARLY the
    moment a seat frees; at the advertised unlock time one more forced sweep is the moment of truth
    — after its expired-flag sweep either a seat is free or a NEW future target was stamped.
    A give_up that carries no unlock time polls too, bounded by DEFAULT_COOLDOWN, instead of
    instantly killing the session. Ctrl-C propagates out of sleep (cli prints a clean message).

    Even with waiting DISABLED we still run ONE forced verify sweep first: a stale reactive flag left
    over from a prior false positive is exactly what wrongly reports "all seats resting" at startup,
    and at COLD START that sweep (trust_reactive_lag=False) also clears a near-max reactive guess — so
    if capacity really exists now the session starts immediately instead of an instant give-up. Only
    if that authoritative sweep still finds no free seat do we give up (without waiting).

    ``cold_start`` gates the near-max-reactive relaxation to the INITIAL launch only. A MID-SESSION
    give-up (a limit caught during the run, then this wait) must NOT clear its own just-stamped
    reactive rest on an endpoint that merely lags below 100% — that would resume the maxed seat at
    once and busy-loop. Mid-session entries keep the conservative lag guard (trust_reactive_lag=True);
    only the cold start, where the flag is old and untrusted, relaxes it."""
    start = now()
    virtual = start

    def vnow():
        # Virtual clock: real sleeps track wall time via now(); the injected test sleep returns
        # instantly, and max() with the slept-forward ``virtual`` keeps the loop terminating for both.
        return max(now(), virtual)

    # The Claude UA shells out to `claude --version` — compute it ONCE per wait, not per poll.
    ua = usage_mod.claude_user_agent(getattr(ctx, "claude_bin", None)) if tool == "claude" else None
    hard_cap = start + DEFAULT_COOLDOWN   # bound for a give_up that advertised NO unlock time
    # Forced verify sweep before any give-up (Fix A, all entries). At COLD START only,
    # trust_reactive_lag=False also lets a fresh "still has credit" reading clear a stale near-max
    # reactive guess (see usage._apply_limit); mid-session keeps the conservative lag guard so a
    # just-stamped reactive rest isn't cleared into a same-seat resume busy-loop.
    sel = _verify_capacity(ctx, tool, get, at=vnow(), force=True, exclude=exclude, ua=ua,
                           trust_reactive_lag=not cold_start)
    if not _wait_on_all_resting_enabled():
        # Waiting disabled: don't poll, but the forced verify above still self-heals a stale reactive
        # flag — start immediately if capacity actually exists now, else give up as before.
        return sel.email if (sel.available and sel.email) else None
    announced = None
    while True:
        if sel.available and sel.email:
            return sel.email                    # capacity actually exists — use it now
        if sel.email is None:
            return None                         # no seats left to wait for (all excluded/removed)
        target = sel.unlocks_at or hard_cap
        if vnow() >= target:
            if sel.unlocks_at is None:
                return None                     # hard_cap exhausted
            # An advertised unlock slipped past while a sweep's network fetches were in flight
            # (its ``at`` is captured before the calls): re-verify with a FRESH clock instead of
            # giving up — the expired sweep then frees the seat or stamps a new future target.
            sel = _verify_capacity(ctx, tool, get, at=vnow(), force=True, exclude=exclude, ua=ua)
            continue
        if announced != target:
            notify(f"all {tool} seats are resting; waiting until {target.isoformat()} to resume "
                   f"on {sel.email} (re-checking every {POLL_INTERVAL_S / 60:.0f} min)")
            announced = target
        # +1ms pads float/µs truncation so a final chunk lands PAST the target, not 1µs short of it
        # (which would cost a pointless extra 1s sleep before the forced moment-of-truth sweep).
        base = vnow()
        remaining = (target - base).total_seconds() + 0.001
        chunk = max(1.0, min(POLL_INTERVAL_S, remaining))
        sleep(chunk)
        virtual = base + timedelta(seconds=chunk)   # anchor to the base ``remaining`` was cut from
        reached = vnow() >= target
        sel = _verify_capacity(ctx, tool, get, at=vnow(), force=reached, exclude=exclude, ua=ua)


# --- commands ---------------------------------------------------------------------------------

def build_cmd(ctx: Context, tool: str, args: list) -> list:
    exe = (ctx.codex_bin if tool == "codex" else ctx.claude_bin) or tool
    return [exe, *args]


def exec_stock(ctx: Context, tool: str, args: list) -> int:
    """Replace this process with the STOCK tool — no supervision, no auto-switch.

    Used when the menubar app is closed: terminal ``codex``/``claude`` must then behave exactly
    like the real tool (against whatever account is currently in ~/.codex / the keychain). We
    ``execvp`` rather than spawn so the tool fully owns the terminal (TTY, signals, exit code).
    Returns 127 only if exec fails (binary missing); on success it never returns.
    """
    argv = build_cmd(ctx, tool, args)
    try:
        os.execvpe(argv[0], argv, harden_env())
    except OSError:
        return 127
    return 127


def resume_cmd(ctx: Context, tool: str) -> list:
    """Resume the most recent session so the work continues after a swap."""
    exe = (ctx.codex_bin if tool == "codex" else ctx.claude_bin) or tool
    if tool == "codex":
        return [exe, "resume", "--last"]
    return [exe, "--continue"]


# --- decision logic (pure-ish; persists state) ------------------------------------------------

@dataclass
class Decision:
    action: str          # "switch" | "resume" | "give_up"
    email: str | None
    unlocks_at: str | None = None


def _seat_confirmed_healthy(state, tool: str, email: str, summary: dict) -> bool:
    """True only when a FRESH usage fetch confirmed this seat has clear headroom — so a stdout
    limit-signal can be safely dismissed as a false positive (the model narrating about limits rather
    than a real banner). Unknown/errored/absent usage → False: we can't confirm, so we fall back to
    trusting the stdout signal exactly as before."""
    if (summary.get(tool) or {}).get(email) != "ok":
        return False  # cached / unauthorized / rate_limited / network / no_creds → can't tell
    u = (state.get_seat(tool, email) or {}).get("usage") or {}
    if u.get("limit_reached"):
        return False  # authoritative API flag says the seat really is out
    pcts = [w.get("used_pct") for w in (u.get("windows") or {}).values()
            if isinstance(w, dict) and w.get("used_pct") is not None]
    return bool(pcts) and max(pcts) < FALSE_ALARM_MAX_PCT


def _hop_or_give_up(state, tool: str, *, exclude, active) -> Decision:
    """Shared decision tail for the limit/auth/exhausted handlers: hop to a free OTHER seat, else
    give_up carrying the soonest unlock time. (``exclude`` may already contain ``active``; the
    ``!= active`` guard is a belt-and-braces so we never 'switch' to the seat we're leaving.)"""
    sel = choose(state, tool, exclude=exclude)
    if sel.email and sel.available and sel.email != active:
        return Decision("switch", sel.email)
    return Decision("give_up", sel.email,
                    sel.unlocks_at.isoformat() if sel.unlocks_at else None)


def handle_limit(ctx: Context, state, tool: str, *, get=usage_mod._default_get,
                 exclude: set | frozenset = frozenset(), corroborated: bool = False,
                 hard: bool = False) -> Decision:
    """A limit was caught for the active seat. Flag it, then choose the next seat. ``exclude`` carries
    seats that already failed auth this run, so a limit never re-selects a known-dead-token seat.
    ``corroborated``: the verify-before-kill probe force-refreshed usage moments ago and it confirmed
    the limit — don't refetch (state already carries the fresh snapshot) or second-guess it here.
    ``hard``: the signal was a trusted tool-side BILLING banner (e.g. codex "workspace out of
    credits") — the usage windows can look healthy while the seat is unusable, so the rest is
    stamped ``source="hard"`` and usage polls must not clear it before it expires."""
    active = state.active(tool)
    # Authoritative reset from the usage endpoint for the seat that just hit the limit (only the
    # active seat — others keep their known state; their stale snapshot tokens would 401 anyway).
    if active and not corroborated:
        summary = usage_mod.refresh(ctx, state, tool, only=active, force=True, get=get)
        # Corroboration guard: the stdout match can be a false positive (the model discussing limits —
        # routine when THIS repo is under development). If the endpoint FRESHLY confirms the active
        # seat still has clear headroom, don't rest a healthy seat — resume the same work instead.
        if _seat_confirmed_healthy(state, tool, active, summary):
            return Decision("resume", active)
        # Only rest on POSITIVE evidence that the seat is really out: the endpoint answered "ok" and
        # its windows show the seat maxed (else _seat_confirmed_healthy would have resumed above).
        # Anything else is inconclusive and must NOT burn a 5h rest — that false positive, cascaded
        # across every seat, is exactly what wrongly killed sessions with "all seats resting":
        #   • network / unauthorized / cached / no_creds → we simply couldn't reach or read the endpoint
        #     (network down, the Headroom proxy in front of it flapping, a stale snapshot token).
        #   • rate_limited (429) is the USAGE ENDPOINT throttling us (it "rate-limits hard" — see
        #     usage._backoff_seconds), NOT the account's quota; a transient server 429 is not an out-
        #     of-quota banner (Claude Code even says "temporarily limiting requests (not your usage
        #     limit)").
        # So on anything but "ok" we keep working on the same seat — bounded by the false-alarm
        # counter, which turns scanning off after MAX_FALSE_ALARMS so a genuine limit still stops.
        # (Only when the active seat is still a real, selectable seat; if the active pointer is stale
        # — the account was removed mid-run — fall through to choose() a valid seat instead of
        # resuming a phantom, exactly as the pre-guard reactive path did.)
        status = (summary.get(tool) or {}).get(active)
        if status != "ok" and state.get_seat(tool, active) is not None:
            return Decision("resume", active)
    seat = state.get_seat(tool, active) if active else None
    # ...else a reactive fallback so we don't immediately re-pick the maxed seat.
    if seat is not None:
        existing = parse_iso(seat.get("limited_until"))
        if hard:
            # A billing banner must ALWAYS land as source="hard" — even over an existing softer
            # flag (e.g. a menubar poll's short "usage" stamp), which a later healthy-looking
            # fetch would clear, re-picking the creditless seat. Keep the later unlock time.
            until = now() + DEFAULT_COOLDOWN
            if existing is not None and existing > until:
                until = existing
            state.set_limited_until(tool, active, iso(until), source="hard")
        elif existing is None:
            state.set_limited_until(tool, active, iso(now() + DEFAULT_COOLDOWN), source="reactive")
    state.save()
    return _hop_or_give_up(state, tool, exclude=exclude, active=active)


def handle_auth_dead(ctx: Context, state, tool: str, *, exclude: set | frozenset = frozenset()) -> Decision:
    """The active seat's credentials are dead (revoked/signed out) for THIS run. Choose a DIFFERENT
    seat, skipping the active one plus any that already failed auth this session (``exclude``).

    We deliberately do NOT persist a "dead" flag on the seat: a usage-poll ``unauthorized`` is not a
    reliable health signal (a non-active seat shows it from a stale cached access token), and a benign
    output match shouldn't disable a seat beyond the current run. Re-login is detected fresh next time.
    """
    active = state.active(tool)
    skip = set(exclude) | ({active} if active else set())
    return _hop_or_give_up(state, tool, exclude=skip, active=active)


def handle_exhausted(ctx: Context, state, tool: str, *, get=usage_mod._default_get,
                     exclude: set | frozenset = frozenset()) -> Decision:
    """Post-exit safety net: the child exited on its own WITHOUT a stdout limit banner we could catch
    mid-session (codex often just errors/exits on a real limit, and this repo's own limit-prose can
    have turned scanning off earlier in the run). FORCE-refresh the active seat; only if the endpoint
    now POSITIVELY confirms it is out AND another seat is free do we hop. No confirmation → give_up,
    so an ordinary non-zero exit (build failure, crash) is surfaced untouched, never a spurious
    switch."""
    active = state.active(tool)
    if not active or state.get_seat(tool, active) is None:
        return Decision("give_up", active)
    summary = usage_mod.refresh(ctx, state, tool, only=active, force=True, get=get)
    until = parse_iso((state.get_seat(tool, active) or {}).get("limited_until"))
    out = until is not None and until > now()
    # A seat can be authoritatively out (limit_reached / a window at 100%) yet carry NO reset
    # timestamp, so usage stamps no ``limited_until``. Treat that "ok but not healthy" reading — the
    # same positive evidence handle_limit rests on — as out, and reactively rest it so choose() won't
    # re-pick it. Anything other than a clean "ok" fetch stays inconclusive → give_up.
    if not out and (summary.get(tool) or {}).get(active) == "ok" \
            and not _seat_confirmed_healthy(state, tool, active, summary):
        state.set_limited_until(tool, active, iso(now() + DEFAULT_COOLDOWN), source="reactive")
        state.save()
        out = True
    if not out:
        return Decision("give_up", active)
    return _hop_or_give_up(state, tool, exclude=exclude, active=active)


# --- real PTY supervisor ----------------------------------------------------------------------

def _real_fd(stream) -> int | None:
    """Return a stream's OS fd, or None if it has none (e.g. captured/replaced under tests)."""
    try:
        fd = stream.fileno()
    except (AttributeError, ValueError, OSError):
        return None
    return fd if isinstance(fd, int) and fd >= 0 else None


def _set_winsize(master_fd: int, out_fd: int) -> None:
    try:
        sz = fcntl.ioctl(out_fd, termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, sz)
    except Exception:
        pass


# Terminal private modes a TUI child (claude/codex) turns on but cannot reset when we KILL it:
# on a limit/auth hop the child gets SIGTERM→SIGKILL and never runs its own cleanup, so the shell
# that inherits the terminal is left in mouse-reporting mode and spews coordinates (e.g. the
# "\e[<35;86;2M" garbage seen at the prompt after a session ends). We disable every mouse-tracking
# variant (including SGR-pixel 1016, which modern Ink-based TUIs set), focus reporting and
# bracketed paste, and re-show the cursor. Alt-screen is deliberately left alone so an inline
# session's visible output stays in the scrollback.
_TERM_RESET = (
    b"\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1015l\x1b[?1016l"  # all mouse-tracking modes off
    b"\x1b[?1004l"  # focus reporting off
    b"\x1b[?2004l"  # bracketed paste off
    b"\x1b[?25h"    # cursor visible
)


def _reset_terminal(out_fd: int) -> None:
    """Undo the terminal private modes a TUI leaves set. No-op unless out_fd is a real terminal."""
    try:
        if os.isatty(out_fd):
            os.write(out_fd, _TERM_RESET)
    except OSError:
        pass


def _exitcode(raw_status: int) -> int:
    return (os.waitstatus_to_exitcode(raw_status)
            if hasattr(os, "waitstatus_to_exitcode") else raw_status)


def pty_spawn(argv: list, on_output: Callable[[bytes], bool]) -> int:
    """Run ``argv`` in a PTY, copying I/O to the real terminal and teeing output to ``on_output``.

    If ``on_output`` returns True, the child is terminated (SIGTERM→SIGKILL) so the caller can
    relaunch. Returns the child's exit status. The child is reaped exactly once.
    """
    # Resolve real fds up front; under test capture / non-tty these may be missing — guard them
    # so we never pass an object with a raising fileno() into select() (which would busy-loop).
    stdin_fd = _real_fd(sys.stdin)
    out_fd = _real_fd(sys.stdout)
    if out_fd is None:
        out_fd = 1
    stdin_is_tty = stdin_fd is not None and os.isatty(stdin_fd)

    pid, master_fd = pty.fork()
    if pid == 0:
        os.execvpe(argv[0], argv, harden_env())
        os._exit(127)

    stop_requested = False
    old_attrs = None
    prev_winch = None
    prev_term = None
    prev_hup = None
    watch = [master_fd] + ([stdin_fd] if stdin_fd is not None else [])

    def _restore_terminal() -> None:
        # Undo mouse-tracking et al. and put the tty back into cooked mode. Idempotent, so it's
        # safe to call from both the signal handler and the finally block.
        _reset_terminal(out_fd)
        if old_attrs is not None:
            try:
                termios.tcsetattr(stdin_fd, termios.TCSAFLUSH, old_attrs)
            except (termios.error, OSError):
                pass

    try:
        if stdin_is_tty:
            old_attrs = termios.tcgetattr(stdin_fd)
            tty.setraw(stdin_fd)
        _set_winsize(master_fd, out_fd)

        def _winch(_sig, _frm):
            _set_winsize(master_fd, out_fd)
        try:
            prev_winch = signal.signal(signal.SIGWINCH, _winch)
        except (ValueError, OSError):
            prev_winch = None

        # If the supervisor itself is killed (tab closed → SIGHUP, `kill` → SIGTERM) the finally
        # below never runs, so the child's terminal modes would leak. Restore the terminal and
        # kill the child from a handler, then die with the signal's default disposition.
        def _on_term(sig, _frm):
            _restore_terminal()
            try:
                _terminate(pid)
            except Exception:
                pass
            signal.signal(sig, signal.SIG_DFL)
            os.kill(os.getpid(), sig)
        try:
            prev_term = signal.signal(signal.SIGTERM, _on_term)
            prev_hup = signal.signal(signal.SIGHUP, _on_term)
        except (ValueError, OSError):
            pass

        while True:
            try:
                rlist, _, _ = select.select(watch, [], [])
            except InterruptedError:
                continue
            except OSError:
                break  # an fd went bad — stop the copy loop and reap
            if master_fd in rlist:
                try:
                    data = os.read(master_fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    break  # child closed the pty → exited
                os.write(out_fd, data)
                if on_output(data):
                    stop_requested = True
                    break
            if stdin_fd is not None and stdin_fd in rlist:
                try:
                    inp = os.read(stdin_fd, 65536)
                except OSError:
                    inp = b""
                if inp:
                    os.write(master_fd, inp)
                else:
                    watch.remove(stdin_fd)  # stdin EOF → stop watching (avoid busy-loop)
    finally:
        # Re-assert the terminal's default private modes the child TUI may have left set (mouse
        # tracking especially) — on the kill path the child never got to do this itself.
        _restore_terminal()
        for sig, prev in ((signal.SIGWINCH, prev_winch),
                          (signal.SIGTERM, prev_term),
                          (signal.SIGHUP, prev_hup)):
            if prev is not None:
                try:
                    signal.signal(sig, prev)
                except (ValueError, OSError):
                    pass
        try:
            os.close(master_fd)
        except OSError:
            pass

    # Reap exactly once. On the stop path, _terminate kills AND reaps and returns the status.
    if stop_requested:
        return _terminate(pid)
    try:
        _, status = os.waitpid(pid, 0)
        return _exitcode(status)
    except ChildProcessError:
        return 0


def _terminate(pid: int) -> int:
    """Stop the child (SIGTERM→SIGKILL) and reap it. Returns its exit/signal status.

    We signal the child's whole PROCESS GROUP: ``pty.fork`` makes the child a session leader, so
    its children (e.g. a shell's subprocesses) share its pgid and must be killed too — otherwise
    an orphan keeps the pty open and we'd hang.
    """
    def _signal(sig):
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, OSError):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass

    for sig in (signal.SIGTERM, signal.SIGKILL):
        _signal(sig)
        for _ in range(20):
            try:
                wpid, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return -signal.SIGKILL  # already reaped elsewhere
            if wpid == pid:
                return _exitcode(status)
            time.sleep(0.05)
    try:
        _, status = os.waitpid(pid, 0)
        return _exitcode(status)
    except ChildProcessError:
        return -signal.SIGKILL


# --- orchestration ----------------------------------------------------------------------------

def _noop(_msg: str) -> None:
    pass


# Sub-commands that establish/replace credentials rather than run an agent session: codex login,
# claude auth login / auth status / setup-token, logout. They must NOT be supervised — there is no
# seat to pick, switch, or auto-switch, and the interactive OAuth flow needs the tool to fully own the
# real TTY. Under our PTY the sign-in never completes: the browser redirects to
# http://localhost:PORT/callback but the callback server (running in the supervised child) is torn
# down / never handed the request, so the seat is never actually added. Run these as the stock tool.
_PASSTHROUGH_CMDS = frozenset({"login", "logout", "auth", "setup-token"})


def run(ctx: Context, tool: str, args: list, *, spawn: SpawnFn = pty_spawn,
        notify: Notifier = _noop, get=usage_mod._default_get,
        max_switches: int = MAX_SWITCHES,
        sleep: Callable[[float], None] = time.sleep) -> int:
    """Launch ``tool`` with the best seat, auto-switching + resuming on limits. Returns exit code."""
    if args and args[0] in _PASSTHROUGH_CMDS:
        # Credential flow (e.g. `claude auth login`): run stock, unsupervised, so the OAuth
        # localhost-callback + interactive prompts work exactly as they do for a plain invocation.
        return exec_stock(ctx, tool, args)
    state = ctx.load_state()
    if not state.accounts(tool):
        raise NoSeats(f"no {tool} seats yet — add one first")

    # NB: legacy-Headroom cleanup deliberately lives in `cli._cmd_run`, BEFORE the app-running split
    # — the passthrough and NoSeats branches above return without ever reaching this far, so cleaning
    # here would miss exactly the fresh-install / `cx login` cases that most need it.

    def _activate_codex_home(email):
        """Point codex at the account's own home so it maintains that account's tokens in place."""
        if tool == "codex" and email:
            from . import codexhome
            codexhome.ensure_home(email, codex_home=ctx._codex_real, root=ctx._homes_root)
            os.environ["CODEX_HOME"] = str(ctx.codex_home(email))

    def _commit_switch(state, email):
        """Persist a seat hop: switch creds, repoint codex's home, stamp the switch time. Caller holds
        ctx.locked() and owns the surrounding notify/budget bookkeeping."""
        switch(ctx, state, tool, email, sync=(tool != "codex"))
        _activate_codex_home(email)
        state.data["last_switch_at"] = iso(now())
        state.save()

    auth_failed: set = set()   # seats whose token died THIS run — skip them for the rest of it

    def _wait_and_activate(cold_start: bool = False) -> bool:
        email = _wait_for_unlock(ctx, tool, notify, sleep, get, exclude=auth_failed,
                                 cold_start=cold_start)
        if email is None:
            return False
        with ctx.locked():
            state = ctx.load_state()
            if email != state.active(tool):
                _commit_switch(state, email)
        return True

    try:
        initial_resting: Decision | None = None
        # Initial selection + switch, under the state lock (brief; never held across a spawn).
        with ctx.locked():
            state = ctx.load_state()
            if tool == "codex":
                from . import accounts as _acct
                _acct.reconcile_codex(ctx, state)   # freshen home(s) from ~/.codex before using them
            sel = choose(state, tool)
            if sel.email and sel.email != state.active(tool):
                switch(ctx, state, tool, sel.email, sync=(tool != "codex"))
            _activate_codex_home(state.active(tool))
        if sel.all_limited:
            # The wait verifies against the live endpoint and recomputes targets from state, so it
            # does not need a pre-known unlock time (unlocks_at may be None for reactive marks).
            initial_resting = Decision("give_up", sel.email,
                                       sel.unlocks_at.isoformat() if sel.unlocks_at else None)

        switches = 0
        resuming = False
        if initial_resting is not None:
            if _wait_and_activate(cold_start=True):   # initial launch: relax a stale reactive guess
                resuming = True
            else:
                notify(f"all {tool} seats are resting; soonest unlocks at "
                       f"{initial_resting.unlocks_at}")
                return EXIT_GAVE_UP
        buf = bytearray()
        # Verify-before-kill scanning state. A stdout match is corroborated against the usage
        # endpoint while the child is STILL RUNNING; a dismissed match costs a brief stall of the
        # output copy loop, never the session. Past MAX_FALSE_ALARMS dismissals the generic output
        # scan turns OFF while supervision continues. Trusted hard-limit banners still bypass that
        # off switch, because they are tool-side stop messages rather than model prose.
        scan = {"on": True, "next_probe": 0.0, "dismissed": 0}

        def _dismissed() -> None:
            scan["dismissed"] += 1
            scan["next_probe"] = time.monotonic() + PROBE_COOLDOWN_S
            if scan["on"] and scan["dismissed"] > MAX_FALSE_ALARMS:
                scan["on"] = False
                notify(f"{tool}'s output keeps mentioning limits while usage says the seat is fine "
                       f"— ignoring limit/auth text for the rest of this session")

        def _probe(reason: str) -> str:
            """Fresh usage check for the active seat while the child is still running. Returns
            "dismiss" (provably prose), "confirmed" (endpoint agrees the seat is out), or "unknown"
            (couldn't tell — the stdout signal is then trusted, exactly as before this guard).

            The state flock is held only for the quick reads/writes on either side of the fetch —
            NEVER across the network call (locked()'s contract; a slow endpoint must stall neither
            other lock takers like the menubar poll nor this probe's caller longer than needed)."""
            try:
                with ctx.locked():
                    st = ctx.load_state()
                    seat = st.active(tool)
                    blob = usage_mod._seat_blob(ctx, st, tool, seat) if seat else None
                if not seat or not blob:
                    return "unknown"
                ua = (usage_mod.claude_user_agent(getattr(ctx, "claude_bin", None))
                      if tool == "claude" else None)
                u = usage_mod._fetch_for(tool, blob, get, ua)  # network — no lock held
                with ctx.locked():
                    st = ctx.load_state()
                    status = usage_mod.store_fetch(st, tool, seat, u)
                    st.save()
                    if reason == "auth":
                        # the creds just authenticated a usage fetch → they aren't dead
                        return "dismiss" if status == "ok" else "unknown"
                    if _seat_confirmed_healthy(st, tool, seat, {tool: {seat: status}}):
                        return "dismiss"
                    return "confirmed" if status == "ok" else "unknown"
            except Exception:
                return "unknown"

        while True:
            argv = resume_cmd(ctx, tool) if resuming else build_cmd(ctx, tool, args)
            hit = {"reason": None, "corroborated": False, "hard": False}  # reason: None|"limit"|"auth"
            buf.clear()

            def on_output(chunk: bytes) -> bool:
                buf.extend(chunk)
                del buf[:-4096]  # keep a rolling tail
                text = buf.decode("utf-8", "replace")
                if detect_hard_limit(tool, text):
                    hit["reason"] = "limit"
                    hit["corroborated"] = True
                    hit["hard"] = True
                    return True
                if not scan["on"]:
                    return False
                reason = detect_event(tool, text)  # one ANSI strip per chunk
                if reason is None:
                    return False
                if time.monotonic() < scan["next_probe"]:
                    buf.clear()  # same prose redrawn within a dismissal's cooldown — skip re-probing
                    return False
                verdict = _probe(reason)
                if verdict == "dismiss":
                    buf.clear()  # don't re-trip on the text still sitting in the rolling tail
                    _dismissed()
                    return False
                hit["reason"] = reason
                hit["corroborated"] = verdict == "confirmed"
                return True

            status = spawn(argv, on_output)  # NO lock held during the session

            if hit["reason"] is None:
                # No stdout limit banner was caught — but a NON-ZERO exit can be a real limit codex
                # surfaced as a plain error (or one that slipped past after scanning turned off). Last
                # resort: confirm via the usage endpoint; if the active seat is genuinely out and a
                # healthy seat is free (and the switch budget allows), hop + resume so the work
                # continues instead of dying on a maxed seat. A clean (0) exit is a real completion —
                # never second-guess it, and a user abort (Ctrl-C/kill) is not a limit — only a
                # POSITIVE, non-abort failure code is worth a usage check. See handle_exhausted.
                if status > 0 and status not in _ABORT_EXITS and switches < max_switches:
                    with ctx.locked():
                        state = ctx.load_state()
                        active = state.active(tool)
                        dec = handle_exhausted(ctx, state, tool, get=get, exclude=auth_failed)
                        if dec.action == "switch":
                            _commit_switch(state, dec.email)
                    if dec.action == "switch":
                        notify(f"{active} hit its usage limit — hopping to {dec.email}, "
                               f"resuming your work ✨")
                        switches += 1
                        resuming = True
                        continue
                    if dec.action == "give_up" and dec.unlocks_at:
                        # (``and dec.unlocks_at`` stays: a timestamp-less give_up here means the
                        # exit was NOT a limit — surface the child's own exit code, never wait.)
                        if _wait_and_activate():
                            resuming = True
                            continue
                        notify(f"all {tool} seats are resting"
                               + (f"; soonest unlocks at {dec.unlocks_at}" if dec.unlocks_at else ""))
                        return EXIT_GAVE_UP
                return status  # clean exit, or a plain failure — child's real exit code

            with ctx.locked():
                state = ctx.load_state()
                active = state.active(tool)
                if hit["reason"] == "auth":
                    if active:
                        auth_failed.add(active)
                    dec = handle_auth_dead(ctx, state, tool, exclude=auth_failed)
                else:
                    dec = handle_limit(ctx, state, tool, get=get, exclude=auth_failed,
                                       corroborated=hit["corroborated"], hard=hit["hard"])
                # The switch cap gates only an actual seat hop. Classify FIRST: a false-alarm
                # "resume" (usage says the active seat is healthy) must not be terminated just
                # because earlier genuine switches used up the budget — that would kill a healthy
                # session, the very bug this supervisor is meant to avoid.
                hop_capped = dec.action == "switch" and switches >= max_switches
                if dec.action == "switch" and not hop_capped:
                    _commit_switch(state, dec.email)
            if hop_capped:
                notify(f"hit the switch limit ({max_switches}); stopping")
                return EXIT_GAVE_UP
            if dec.action == "switch":
                reason_msg = ("needs you to sign in again 🔑" if hit["reason"] == "auth"
                              else "needs a rest 💤")
                notify(f"{active} {reason_msg} — hopping to {dec.email}, "
                       f"your work's coming with you ✨")
                switches += 1
                resuming = True
                continue
            if dec.action == "resume":
                # Kill-path false alarm: the probe couldn't tell (endpoint error mid-session) so the
                # child was stopped on the stdout signal alone — but handle_limit's own fresh fetch
                # then confirmed the seat healthy. Resume the SAME seat and carry the work on. It
                # counts toward the SAME false-alarm bound as an in-flight dismissal (one counter,
                # one message, and the probe cooldown), past which scanning turns off — the text is
                # provably untrustworthy — while supervision continues.
                _dismissed()
                resuming = True
                continue
            if hit["reason"] == "auth":
                if dec.unlocks_at:
                    notify(f"{active} needs you to sign in again 🔑 — the only other {tool} seat is "
                           f"resting until {dec.unlocks_at}")
                else:
                    notify(f"{active} needs you to sign in again (token revoked) and no other "
                           f"{tool} seat is ready — re-add it via the app or `acctsw add {tool}`")
                return EXIT_GAVE_UP
            if _wait_and_activate():
                resuming = True
                continue
            notify(f"all {tool} seats are resting"
                   + (f"; soonest unlocks at {dec.unlocks_at}" if dec.unlocks_at else ""))
            return EXIT_GAVE_UP
    finally:
        # On exit, reconcile the active account's creds (the just-run seat may carry a rotated token).
        #  - codex: it maintained its own home via CODEX_HOME → mirror the home into ~/.codex so
        #    plain codex / the GUI follow the active account.
        #  - claude: sync the live keychain item back into the account's snapshot.
        try:
            with ctx.locked():
                st = ctx.load_state()
                active = st.active(tool)
                if tool == "codex" and active:
                    blob = ctx.snapshot_get("codex", active)   # home = source of truth
                    if blob:
                        ctx.cred["codex"].set_live(blob)        # mirror → ~/.codex
                elif sync_back(ctx, st, tool):
                    st.save()
        except Exception:
            pass
