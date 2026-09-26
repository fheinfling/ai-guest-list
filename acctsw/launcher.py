"""Supervised launcher: run an agent, auto-switch on usage limit, resume the same work.

Design (testability): the *decisions* (limit detection, what to do on a limit) are pure functions;
the messy PTY I/O is isolated behind an injectable ``spawn`` callable so ``run()`` is unit-tested
with a scripted fake child (no real PTY, no network).

Flow:
  1. pick a seat (prefer active; else available; else soonest-unlock + report) and switch to it
  2. spawn the agent under a PTY, teeing output while a TICK_INTERVAL_S heartbeat polls the
     STRUCTURED signals beside it — the child's own rollout JSONL (codex writes its rate-limit
     windows and the server's error code there) and the engine state file (another process, e.g.
     the menubar's usage poll, can rest the seat we are running on while we hold no lock)
  3. on any confirmed signal: DECIDE BEFORE KILLING — while the child is still running, stamp the
     seat, choose a different seat, and check the switch budget. Only when all three pass do we
     stop, commit that exact hop, and relaunch with RESUME; unverifiable signals and confirmed
     limits with nowhere to land leave the live session alone
  4. on normal exit: sync-back the (refreshed) creds and return the child's exit status

Authority order for "this seat is out" (highest first) — banner strings are the WEAKEST evidence
and never decide anything on their own when something better is available:
  1. the child's rollout JSONL (``rollout.RolloutWatcher``): the tool's own structured record,
     carrying the server's error code and the real reset time
  2. the usage endpoint's authoritative flags (``usage.snapshot_says_out`` / ``_is_limited``):
     ``allowed:false``, ``rate_limit_reached_type``, spend control, a window at 100%
  3. engine state: a seat rested by ANOTHER process with ``limit_source`` "usage"/"hard" (never our
     own weakest "reactive" guess) means hop now, mid-session
  4. stdout regexes: a HINT that triggers a probe, and a fallback only when nothing better exists
     (no rollout attached). A fresh structured "healthy" reading dismisses limit prose outright.
"""
from __future__ import annotations

import fcntl
import logging
import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import threading
import time
import tty
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from . import identity as identity_mod
from . import rollout
from . import usage as usage_mod
from .context import Context
from .errors import AcctswError
from .procenv import harden_env
from .selection import Selection, choose
from .session import active_session, clear_session, mark_session
from .switch import switch, sync_back
from .util import iso, now, parse_iso

# A spawn function: (argv, on_output, on_tick=None) -> exit_status.
#   on_output(chunk: bytes) -> bool ; returning True asks the supervisor to stop the child.
#   on_tick() -> bool           ; called about every TICK_INTERVAL_S, INDEPENDENTLY of output (a
#                                 silent child still has to be supervised); True also stops it.
SpawnFn = Callable[..., int]
Notifier = Callable[[str], None]

# Default cooldown when a limit is caught but no authoritative reset is known (owned by usage so
# its limit-flagging can share it; re-exported here for the handlers and existing callers/tests).
DEFAULT_COOLDOWN = usage_mod.DEFAULT_COOLDOWN
MAX_SWITCHES = 6      # safety bound on auto-relaunches within one `run`
TICK_INTERVAL_S = 2.0   # heartbeat for the structured signals (rollout JSONL + state file). Cheap
                        # by construction: one incremental read of an already-open path and one
                        # os.stat, so a silent child is still supervised without polling the network.
TICK_BLOCK_S = 60.0     # after a tick decision that could NOT stop the child (no landing seat), wait
                        # this long before re-deciding — a permanently rested seat must not re-run
                        # the whole decision (and its notifications) every TICK_INTERVAL_S.
MAX_FALSE_ALARMS = 3  # dismissed stdout matches per run before stdout scanning is switched OFF
                      # (supervision continues — only the untrustworthy signal is dropped)
PROBE_COOLDOWN_S = 30.0  # after a dismissed or inconclusive match, skip re-probing usage for this
                         # long: a TUI redraws the same prose every frame, and without a cooldown
                         # each redraw would force-hit the usage endpoint
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

# NOTE (deferred): Claude Code also prints a benign pre-limit WARNING ("Approaching your 5-hour limit")
# that trips the loose window patterns and, in the 90-100% band, the usage-probe corroboration cannot
# dismiss it (healthy requires < FALSE_ALARM_MAX_PCT). A text veto for it was attempted and removed:
# distinguishing the warning from a real hit in the ANSI-mangled rolling byte tail is not reliably
# doable by proximity heuristics (a fixed window either crosses a newline and masks a real reset-paired
# banner, or admits benign trailing text and misfires). This matches shipped behaviour and is best
# fixed with real Claude output samples in a focused change, not guessed at here.

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

# Exact Codex error, including terminal line wrapping. A still-valid access token can read usage
# even after the refresh token has been revoked, so a usage 200 cannot disprove this banner.
# Require the TUI error glyph and full wording; ordinary prose still goes through the probe.
_CODEX_REFRESH_REVOKED = re.compile(
    r"(?m)^\s*■\s+Your access token could not be refreshed because your refresh token was"
    r"\s+revoked\.\s+Please log out and sign in again\.", re.IGNORECASE)

# Codex's hard billing banner (the ChatGPT workspace has no credits left). This is a HINT and a
# FALLBACK, never the primary evidence: the decision belongs to the structured signals (the rollout
# JSONL's own error code / reached-type, and the usage endpoint's flags), and when we are tailing
# this child's rollout we WAIT for that fact instead of acting on prose. The old pattern also pinned
# the trailing sentence ("Add credits to continue"), and codex 0.153.4 shipped different wording
# ("Ask your workspace owner to refill in order to continue") — so the whole hop silently stopped
# working. Only the line anchor and the leading-glyph allowance remain, which is what keeps the
# model's own narration about credits (mid-sentence, never at a line start) out.
HARD_LIMIT_PATTERNS = {
    "codex": [
        r"(?m)^[^\w\n]{0,8}\s*your\s+workspace\s+is\s+out\s+of\s+credits?\b",
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
    return any(rx.search(clean) for rx in _LIMIT_RE[tool])


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


def detect_refresh_revoked(tool: str, text: str) -> bool:
    clean = _ANSI.sub("", text)
    # Wrapping can occur between any words, not just before "revoked".
    lines = re.sub(r"(?<=\S)[ \t]*\r?\n[ \t]*(?=\w)", " ", clean)
    return tool == "codex" and bool(_CODEX_REFRESH_REVOKED.search(lines))


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
            # The ACTIVE seat's error backoff is capped short (usage.ACTIVE_MAX_BACKOFF_SECONDS):
            # the seat we're actually running on must re-validate promptly, not sit out the 1h cap.
            if not force and not usage_mod._due(prev, at, POLL_MIN_FETCH_S,
                                                active=(state.active(tool) == email)):
                continue
            blob = usage_mod._seat_blob(ctx, state, tool, email)
            if blob:
                pending.append((email, blob))
    results = []
    for email, blob in pending:   # network — no lock held
        try:
            # Carry the EXACT blob the fetch used into store_fetch: it re-derives plan/account_id
            # from it, so a re-subscription is caught here too — not only on the menubar's poll.
            results.append((email, blob, usage_mod._fetch_for(tool, blob, get, ua)))
        except Exception:
            pass  # a broken blob/transport must not kill the wait — the seat just isn't refreshed
    with ctx.locked():
        state = ctx.load_state()
        changed = False
        for email, blob, u in results:
            seat = state.get_seat(tool, email)
            if seat is None or usage_mod._seat_blob(ctx, state, tool, email) != blob:
                continue  # removed, re-authenticated, or changed identity while the request ran
            previous = seat.get("usage") or {}
            previous_attempt = parse_iso(previous.get("last_attempted_at") or previous.get("fetched_at"))
            fetched_at = parse_iso(u.fetched_at)
            if previous_attempt and fetched_at and previous_attempt > fetched_at:
                continue  # a newer GUI/launcher observation already superseded this result
            usage_mod.store_fetch(state, tool, email, u, at=at,
                                  trust_reactive_lag=trust_reactive_lag, blob=blob)
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
    if usage_mod.snapshot_says_out(u):
        # Authoritative API flags say the seat really is out. NOT just ``limit_reached``: the
        # credits-depleted payload reports NULL windows and says so only via allowed/reached_type,
        # so a percentage-only reading would call a creditless seat "healthy" and dismiss a real hit.
        return False
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
                 hard: bool = False, reset_at: str | None = None, source: str | None = None,
                 detail: str | None = None) -> Decision:
    """A limit was caught for the active seat. Flag it, then choose the next seat. ``exclude`` carries
    seats that already failed auth this run, so a limit never re-selects a known-dead-token seat.
    ``corroborated``: the verify-before-kill probe force-refreshed usage moments ago and it confirmed
    the limit — don't refetch (state already carries the fresh snapshot) or second-guess it here.
    ``hard``: the signal was a trusted tool-side BILLING/entitlement stop (the rollout's own error
    code or reached-type, or the banner as a fallback) — the usage windows can look healthy while
    the seat is unusable, so the rest is stamped ``source="hard"`` and usage polls must not clear it
    before it expires.
    ``reset_at``/``source``/``detail``: what a STRUCTURED signal knew and a stdout match never
    could — the server's own unlock time, which evidence class stamped it, and the human phrase to
    keep on the seat. Omitted by every pre-existing caller, which therefore behaves exactly as
    before (reactive guess, blind DEFAULT_COOLDOWN)."""
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
        #   • network / unauthorized (401) / cached / no_creds → we simply couldn't reach or read the
        #     endpoint (network down, the Headroom proxy in front of it flapping, a stale snapshot
        #     token). A 401 is routine and inconclusive — it must never rest a seat.
        #   • rate_limited (429) is the USAGE ENDPOINT throttling us (it "rate-limits hard" — see
        #     usage._backoff_seconds), NOT the account's quota; a transient server 429 is not an out-
        #     of-quota banner (Claude Code even says "temporarily limiting requests (not your usage
        #     limit)").
        # So on anything but "ok" we keep working on the same seat. In the live supervisor `_probe`
        # tracks these unknowns separately from PROVEN false alarms, keeping scanning enabled so a
        # later genuine limit is still caught after the endpoint recovers.
        # (Only when the active seat is still a real, selectable seat; if the active pointer is stale
        # — the account was removed mid-run — fall through to choose() a valid seat instead of
        # resuming a phantom, exactly as the pre-guard reactive path did.)
        status = (summary.get(tool) or {}).get(active)
        # 403 is the one non-"ok" status that IS positive evidence: the endpoint answered, and it
        # answered "this account is not entitled" — a cancelled/terminated subscription, not a
        # quota. Resting it would advertise a reset that will never come, so treat it exactly like
        # a dead token: leave this seat for an entitled one (or, with none, keep running and say so).
        # The production live path normally catches this earlier as `_probe`'s "revoked" verdict;
        # keep this branch as the defensive fallback for direct/non-corroborated callers.
        if status == "forbidden":
            return handle_auth_dead(ctx, state, tool, exclude=exclude)
        if status != "ok" and state.get_seat(tool, active) is not None:
            return Decision("resume", active)
    seat = state.get_seat(tool, active) if active else None
    # ...else a reactive fallback so we don't immediately re-pick the maxed seat.
    if seat is not None:
        existing = parse_iso(seat.get("limited_until"))
        src = source or ("hard" if hard else "reactive")
        # Re-stamp when the signal is authoritative (hard), when it brought a real reset time, or
        # when nothing is stamped yet. A soft signal that knows no more than an existing flag does
        # must NOT re-anchor it to now() — that would make the wait target recede on every poll.
        if hard or reset_at or existing is None:
            until = parse_iso(reset_at) or (now() + DEFAULT_COOLDOWN)
            if existing is not None and existing > until:
                until = existing   # never shorten a rest we already believed in
            state.set_limited_until(tool, active, iso(until), source=src)
            if detail:
                seat["limit_detail"] = detail   # the server's own phrase, for the UI/notifications
    state.save()
    return _hop_or_give_up(state, tool, exclude=exclude, active=active)


def handle_auth_dead(ctx: Context, state, tool: str, *, exclude: set | frozenset = frozenset()) -> Decision:
    """The active seat's credentials are dead (revoked/signed out) for THIS run. Choose a DIFFERENT
    seat, skipping the active one plus any that already failed auth this session (``exclude``).

    This helper does NOT persist a "dead" flag on the seat: a usage-poll ``unauthorized`` is not a
    reliable health signal (a non-active seat shows it from a stale cached access token), and a benign
    output match shouldn't disable a seat beyond the current run. Only the caller handling Codex's
    exact refresh-token-revoked error persists a re-login requirement.
    """
    active = state.active(tool)
    skip = set(exclude) | ({active} if active else set())
    return _hop_or_give_up(state, tool, exclude=skip, active=active)


def handle_exhausted(ctx: Context, state, tool: str, *, get=usage_mod._default_get,
                     exclude: set | frozenset = frozenset(),
                     user_agent: str | None = None) -> Decision:
    """Post-exit safety net: the child exited on its own WITHOUT a stdout limit banner we could catch
    mid-session (codex often just errors/exits on a real limit, and this repo's own limit-prose can
    have turned scanning off earlier in the run). FORCE-refresh the active seat; only if the endpoint
    now POSITIVELY confirms it is out AND another seat is free do we hop. No confirmation → give_up,
    so an ordinary non-zero exit (build failure, crash) is surfaced untouched, never a spurious
    switch."""
    active = state.active(tool)
    if not active or state.get_seat(tool, active) is None:
        return Decision("give_up", active)
    summary = usage_mod.refresh(
        ctx, state, tool, only=active, force=True, get=get, user_agent=user_agent
    )
    status = (summary.get(tool) or {}).get(active)
    # A 403 is positive evidence that this seat is no longer entitled, not an inconclusive fetch
    # and not a quota rest. Leave it through the dead-token path without inventing a reset time.
    if status == "forbidden":
        return handle_auth_dead(ctx, state, tool, exclude=exclude)
    until = parse_iso((state.get_seat(tool, active) or {}).get("limited_until"))
    out = until is not None and until > now()
    # A seat can be authoritatively out (limit_reached / a window at 100%) yet carry NO reset
    # timestamp, so usage stamps no ``limited_until``. Treat that "ok but not healthy" reading — the
    # same positive evidence handle_limit rests on — as out, and reactively rest it so choose() won't
    # re-pick it. Anything other than a clean "ok" fetch stays inconclusive → give_up.
    if not out and status == "ok" \
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


def pty_spawn(argv: list, on_output: Callable[[bytes], bool],
              on_tick: Callable[[], bool] | None = None,
              tick_interval: float = TICK_INTERVAL_S) -> int:
    """Run ``argv`` in a PTY, copying I/O to the real terminal and teeing output to ``on_output``.

    If ``on_output`` returns True, the child is terminated (SIGTERM→SIGKILL) so the caller can
    relaunch. Returns the child's exit status. The child is reaped exactly once.

    ``on_tick`` is the supervisor's heartbeat, called about every ``tick_interval`` seconds and
    independently of output: the structured signals (the child's rollout log, the engine state) are
    the ones that actually decide, and a child can sit silent for minutes — or flood the terminal so
    fast that select() never times out. It is therefore driven from BOTH ends of the loop: a timeout
    when there is nothing to copy, and an elapsed-time check after each copy. True stops the child
    exactly like ``on_output`` does.
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
    terminating = False
    pending_signal = None
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
            if master_fd is not None:
                _set_winsize(master_fd, out_fd)
        try:
            prev_winch = signal.signal(signal.SIGWINCH, _winch)
        except (ValueError, OSError):
            prev_winch = None

        # If the supervisor itself is killed (tab closed → SIGHUP, `kill` → SIGTERM) the finally
        # below never runs, so the child's terminal modes would leak. Restore the terminal and
        # kill the child from a handler, then die with the signal's default disposition.
        def _stop_child():
            nonlocal terminating, master_fd
            terminating = True
            # Transfer ownership: _terminate closes it exactly once, even after a bounded timeout.
            fd, master_fd = master_fd, None
            return _terminate(pid, fd)

        def _on_term(sig, _frm):
            nonlocal pending_signal
            pending_signal = sig
            if terminating:
                return  # Includes signals interrupting an auto-switch's shutdown/drain.
            try:
                _stop_child()
            except Exception:
                pass
            _restore_terminal()
            signal.signal(sig, signal.SIG_DFL)
            os.kill(os.getpid(), sig)
        try:
            prev_term = signal.signal(signal.SIGTERM, _on_term)
            prev_hup = signal.signal(signal.SIGHUP, _on_term)
        except (ValueError, OSError):
            pass

        next_tick = time.monotonic() + tick_interval
        while True:
            timeout = None if on_tick is None else max(0.0, next_tick - time.monotonic())
            try:
                rlist, _, _ = select.select(watch, [], [], timeout)
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
            # Checked on EVERY iteration, not only on a select timeout: a chatty TUI keeps the
            # master fd readable forever, so a timeout-only tick would never fire on the one child
            # that most needs supervising.
            if on_tick is not None and time.monotonic() >= next_tick:
                next_tick = time.monotonic() + tick_interval
                if on_tick():
                    stop_requested = True
                    break
        if stop_requested:
            # Keep the PTY open while the child handles SIGTERM and saves its conversation.
            # Keep READING it too: tty close can wait for queued output to drain on macOS.
            result = _stop_child()
            if pending_signal is not None:
                _restore_terminal()
                signal.signal(pending_signal, signal.SIG_DFL)
                os.kill(os.getpid(), pending_signal)
            return result
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
            if master_fd is not None:
                os.close(master_fd)
        except OSError:
            pass

    # Reap exactly once. The stop path above already killed and reaped before closing the PTY.
    try:
        _, status = os.waitpid(pid, 0)
        return _exitcode(status)
    except ChildProcessError:
        return 0


def _terminate(pid: int, master_fd: int | None = None) -> int:
    """Stop the child (SIGTERM→SIGKILL) and reap it. Returns its exit/signal status.

    We signal the child's whole PROCESS GROUP: ``pty.fork`` makes the child a session leader, so
    its children (e.g. a shell's subprocesses) share its pgid and must be killed too — otherwise
    an orphan keeps the pty open and we'd hang. Takes ownership of master_fd, if supplied.
    Shutdown output is discarded: forwarding to a closed/stalled terminal can itself block.
    """
    def _signal(sig):
        try:
            pgid = os.getpgid(pid)
            if pgid == pid:
                os.killpg(pgid, sig)
            else:
                os.kill(pid, sig)  # non-PTY callers may share our own process group
        except (ProcessLookupError, OSError):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass

    def _close_master():
        nonlocal master_fd
        if master_fd is not None:
            fd, master_fd = master_fd, None
            try:
                os.close(fd)
            except OSError:
                pass

    def _wait(seconds):
        deadline = time.monotonic() + seconds
        while True:
            try:
                wpid, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return -signal.SIGKILL
            if wpid == pid:
                return _exitcode(status)
            delay = min(0.05, max(0, deadline - time.monotonic()))
            if not delay:
                return None
            if master_fd is None:
                time.sleep(delay)
                continue
            try:
                readable, _, _ = select.select([master_fd], [], [], delay)
                if readable and not os.read(master_fd, 65536):
                    # EOF; keep the fd until the grace ends but avoid spinning.
                    time.sleep(delay)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                time.sleep(delay)

    try:
        if master_fd is not None:
            os.set_blocking(master_fd, False)
        # About five seconds for the TUI to save its session, without an early SIGHUP.
        for sig, grace in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 1.0)):
            _signal(sig)
            result = _wait(grace)
            if result is not None:
                return result
        # tty drain can wedge even an exiting SIGKILLed child. Closing releases that wait.
        _close_master()
        result = _wait(1.0)
        if result is not None:
            return result
        logging.getLogger(__name__).warning(
            'Child %s did not reap after SIGKILL and PTY close; leaving it for init', pid)
        return -signal.SIGKILL
    finally:
        _close_master()


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
    seen_manual_switch = state.data["tools"][tool].get("manual_switch")

    # NB: legacy-Headroom cleanup deliberately lives in `cli._cmd_run`, BEFORE the app-running split
    # — the passthrough and NoSeats branches above return without ever reaching this far, so cleaning
    # here would miss exactly the fresh-install / `cx login` cases that most need it.

    def _activate_codex_home(email):
        """Point codex at the account's own home so it maintains that account's tokens in place."""
        if tool == "codex" and email:
            from . import codexhome
            # Promotion MOVES shared files (incl. live SQLite databases) out of the home into
            # ~/.codex, so ask for it only when no supervised codex session is alive to have them
            # open — ours included, once this run has marked itself. Skipping costs nothing: the
            # heal happens at the next launch that finds the coast clear.
            promote = active_session(ctx.data_dir, "codex") is None
            home = codexhome.ensure_home(email, codex_home=ctx._codex_real, root=ctx._homes_root,
                                         promote=promote)
            os.environ["CODEX_HOME"] = str(home)

    # Claude's official identity command can stall for 30 seconds. Resolve it with NO state flock,
    # and memoise the answer only for the exact live Keychain blob: each distinct credential value
    # gets its own answer, while later hops cannot serially respawn the CLI for unchanged bytes.
    # The reconcile write rechecks that the same blob is still live.
    claude_identities: dict[str | None, identity_mod.ClaudeLiveIdentity] = {}

    def _claude_live_identity() -> identity_mod.ClaudeLiveIdentity | None:
        if tool != "claude":
            return None
        live = ctx.cred["claude"].get_live()
        if live in claude_identities:
            return claude_identities[live]
        resolved = identity_mod.claude_live_identity(ctx)  # subprocess — caller holds no state lock
        if resolved.blob == live:
            claude_identities[live] = resolved
        return resolved

    def _commit_switch(state, email, live_identity=None):
        """Persist a seat hop: switch creds, repoint codex's home, stamp the switch time. Caller holds
        ctx.locked(), having resolved any Claude identity before the lock, and owns the surrounding
        notify/budget bookkeeping."""
        switch(ctx, state, tool, email, sync=(tool != "codex"), live_identity=live_identity)
        _activate_codex_home(email)
        state.data["last_switch_at"] = iso(now())
        state.save()

    auth_failed: set = set()   # seats whose token died THIS run — skip them for the rest of it

    def _auto_switch_is_on() -> bool:
        """Read the live preference at an unlocked decision boundary.

        A hard-limit landing pre-flight performs network I/O, so the toggle may change while it is
        in flight.  Every path that can stop a child rechecks at such a boundary rather than treating
        the setting sampled before the fetch as a lease to swap credentials later.
        """
        with ctx.locked():
            return bool(ctx.load_state().settings().get("auto_switch", True))

    def _wait_and_activate(cold_start: bool = False) -> bool:
        if not cold_start and not _auto_switch_is_on():
            return False
        email = _wait_for_unlock(ctx, tool, notify, sleep, get, exclude=auth_failed,
                                 cold_start=cold_start)
        if email is None or (not cold_start and not _auto_switch_is_on()):
            return False
        hopped = False
        live_identity = None
        if tool == "claude":
            # The wait may return the seat that is already active. Check that cheaply first so the
            # common no-switch path never pays for `claude auth status`; resolve only between locks.
            with ctx.locked():
                if email == ctx.load_state().active(tool):
                    return True
            live_identity = _claude_live_identity()  # slow Claude status happens before the flock
        with ctx.locked():
            state = ctx.load_state()
            if not cold_start and not state.settings().get("auto_switch", True):
                return False
            if email != state.active(tool):
                _commit_switch(state, email, live_identity)
                hopped = True
        if hopped:
            mark_session(ctx.data_dir, tool, email)
        return True

    # A confirmed limit with no landing seat must not kill the live child. Keep one lightweight
    # watcher beside it so the user gets a single heads-up when another seat later becomes usable;
    # the watcher is advisory only — it never signals the child or commits a switch.
    watcher_stop = threading.Event()
    watcher_thread: threading.Thread | None = None
    watcher_started = False

    def _start_capacity_watcher(blocked_email: str | None) -> None:
        nonlocal watcher_thread, watcher_started
        if watcher_started:
            return
        watcher_started = True
        excluded = set(auth_failed)
        if blocked_email:
            excluded.add(blocked_email)  # a landing seat must differ from the still-running seat
        ua = (usage_mod.claude_user_agent(getattr(ctx, "claude_bin", None))
              if tool == "claude" else None)

        def _watch() -> None:
            while not watcher_stop.is_set():
                # Default real sleeps are made interruptible so run()'s finally can always join
                # promptly. Injected sleeps mirror _wait_for_unlock and make watcher tests instant.
                if sleep is time.sleep:
                    if watcher_stop.wait(POLL_INTERVAL_S):
                        return
                else:
                    sleep(POLL_INTERVAL_S)
                    if watcher_stop.is_set():
                        return
                try:
                    sel = _verify_capacity(ctx, tool, get, at=now(), force=False,
                                           exclude=excluded, ua=ua)
                except Exception:
                    continue
                if sel.email and sel.available:
                    notify(f"{sel.email} is available for {tool} now")
                    return

        watcher_thread = threading.Thread(
            target=_watch, name=f"acctsw-{tool}-seat-watcher", daemon=True
        )
        watcher_thread.start()

    try:
        initial_resting: Decision | None = None
        # Initial selection is cheap. If Claude actually needs a switch, resolve its slow identity
        # between two lock acquisitions and re-run selection before committing.
        claude_switch_needed = False
        with ctx.locked():
            state = ctx.load_state()
            if tool == "codex":
                from . import accounts as _acct
                if active_session(ctx.data_dir, tool) is None:
                    # A live supervisor owns its private tokens; its shared mirror can be older.
                    _acct.reconcile_codex(ctx, state)
            sel = choose(state, tool)
            if not sel.email and all(s.get("auth_error") for s in state.accounts(tool).values()):
                notify(f"all {tool} seats need you to sign in again — re-add them via the app")
                return EXIT_GAVE_UP
            claude_switch_needed = (
                tool == "claude"
                and sel.available
                and bool(sel.email)
                and sel.email != state.active(tool)
            )
            if (sel.available and sel.email and sel.email != state.active(tool)
                    and not claude_switch_needed):
                switch(ctx, state, tool, sel.email, sync=(tool != "codex"),
                       live_identity=None)
            _activate_codex_home(state.active(tool))
        if claude_switch_needed:
            live_identity = _claude_live_identity()  # subprocess — no state flock held
            with ctx.locked():
                state = ctx.load_state()
                sel = choose(state, tool)  # selection may have changed while identity resolved
                if sel.available and sel.email and sel.email != state.active(tool):
                    switch(ctx, state, tool, sel.email, live_identity=live_identity)
        if sel.all_limited:
            # The wait verifies against the live endpoint and recomputes targets from state, so it
            # does not need a pre-known unlock time (unlocks_at may be None for reactive marks).
            initial_resting = Decision("give_up", sel.email,
                                       sel.unlocks_at.isoformat() if sel.unlocks_at else None)

        switches = 0
        resuming = False
        resume_thread = None
        if initial_resting is not None:
            # No child has run yet. Even after waiting, honor the original invocation (including
            # --version or a fresh prompt); only a handoff during a session should add resume.
            if not _wait_and_activate(cold_start=True):
                notify(f"all {tool} seats are resting; soonest unlocks at "
                       f"{initial_resting.unlocks_at}")
                return EXIT_GAVE_UP
        buf = bytearray()
        # Decide-before-kill scanning state. A stdout match is corroborated and its landing seat is
        # selected while the child is STILL RUNNING. A dismissed or unverifiable match costs only a
        # brief output-copy stall. Only PROVEN dismissals consume MAX_FALSE_ALARMS and can turn the
        # generic scan off; endpoint noise is tracked separately so recovery can reveal a later real
        # limit. Trusted hard-limit banners bypass that off switch but still need a free landing seat
        # and switch budget before they may stop the child.
        scan = {
            "on": True,
            "next_probe": 0.0,
            "dismissed": 0,
            "unknown": 0,
            "unknown_notified": False,
            "limit_stay_notified": False,
            "auth_stay_notified": False,
            "revoked_stay_notified": False,
            "budget_notified": False,
            "auto_switch_off_notified": False,
        }

        def _dismissed() -> None:
            scan["dismissed"] += 1
            scan["next_probe"] = time.monotonic() + PROBE_COOLDOWN_S
            if scan["on"] and scan["dismissed"] > MAX_FALSE_ALARMS:
                scan["on"] = False
                notify(f"{tool}'s output keeps mentioning limits while usage says the seat is fine "
                       f"— ignoring limit/auth text for the rest of this session")

        def _unknown() -> None:
            # A failed/throttled endpoint says nothing about whether the text is trustworthy. Keep a
            # separate count for diagnosis and only rate-limit retries; unlike _dismissed this may
            # never disable scanning during the Headroom-proxy-flapping field failure.
            scan["unknown"] += 1
            scan["next_probe"] = time.monotonic() + PROBE_COOLDOWN_S

        def _probe(reason: str) -> str:
            """Fresh usage check for the active seat while the child is still running. Returns
            "dismiss" when fresh usage disproves the signal; "confirmed" when a LIMIT signal agrees
            the seat is out; "revoked" when a 403 positively proves the seat lost entitlement; or
            "unknown" when the endpoint could not decide. For a limit, unknown always leaves the
            child alive. For a tool-specific auth-death banner, unknown does not override that
            trusted signal, so the supervisor may still hop after it preflights a healthy landing.

            The state flock is held only for the quick reads/writes on either side of the fetch —
            NEVER across the network call (locked()'s contract; a slow endpoint must stall neither
            other lock takers like the menubar poll nor this probe's caller longer than needed)."""
            try:
                with ctx.locked():
                    st = ctx.load_state()
                    # ALWAYS the seat this child was SPAWNED with — CODEX_HOME is process-global, so
                    # the credentials behind this fetch are that seat's no matter where ``active``
                    # points now. Judging st.active() instead would fetch with the running seat's
                    # creds and file the answer (windows, limit flags, account_id) under a sibling's
                    # name — resting a healthy seat and tripping the re-subscription branch.
                    seat = (launch_email
                            if launch_email and st.get_seat(tool, launch_email) is not None
                            else st.active(tool))
                    blob = usage_mod._seat_blob(ctx, st, tool, seat) if seat else None
                if not seat or not blob:
                    return "unknown"
                ua = (usage_mod.claude_user_agent(getattr(ctx, "claude_bin", None))
                      if tool == "claude" else None)
                u = usage_mod._fetch_for(tool, blob, get, ua)  # network — no lock held
                with ctx.locked():
                    st = ctx.load_state()
                    status = usage_mod.store_fetch(st, tool, seat, u, blob=blob)
                    st.save()
                    if status == "forbidden":
                        return "revoked"  # positive lost-entitlement evidence, never a quota rest
                    if reason == "auth":
                        # Dismiss loose auth prose when access still works. The exact refresh-token
                        # revocation banner bypasses this probe: usage cannot test a refresh token.
                        return "dismiss" if status == "ok" else "unknown"
                    if _seat_confirmed_healthy(st, tool, seat, {tool: {seat: status}}):
                        return "dismiss"
                    return "confirmed" if status == "ok" else "unknown"
            except Exception:
                return "unknown"

        def _decide_and_maybe_stop(*, reason: str, hard: bool = False,
                                   refresh_revoked: bool = False,
                                   reset_at: str | None = None, source: str | None = None,
                                   detail: str | None = None,
                                   clear: Callable[[], None] = lambda: None) -> bool:
            """The ONE decision tail every confirmed signal goes through — stdout, the rollout
            JSONL, or a seat another process rested. Stamp the seat, choose the exact landing seat,
            check the switch budget; return True (with ``hit`` filled in) only when all of them
            pass, else leave the live child alone and say why exactly once.

            DECIDE BEFORE KILLING: nothing here kills anything. It reports whether a stop is
            justified while the child is still running, so an unverifiable signal or a confirmed
            limit with nowhere to land costs nothing but a brief stall.
            """
            # ``hit`` is rebound per loop iteration; the closure reads the current one.
            hit["handled"] = True   # recognized — the exit-time net must not re-derive this signal
            with ctx.locked():
                state = ctx.load_state()
                active = state.active(tool)
                prev_active = active
                # Another process (the menubar's usage poll) may already have moved ``active`` off
                # the seat this child is running on — that is exactly the field failure this whole
                # tick exists for. CODEX_HOME was pointed at ``launch_email`` before the spawn and
                # is process-global, so the seat to rest and leave is ALWAYS launch_email.
                realigned = (bool(launch_email) and active != launch_email
                             and state.get_seat(tool, launch_email) is not None)
                if realigned:
                    state.set_active(tool, launch_email)
                    active = launch_email
                if reason in ("auth", "revoked"):
                    if active:
                        auth_failed.add(active)
                        seat = state.get_seat(tool, active)
                        if refresh_revoked and seat is not None:
                            seat["auth_error"] = "refresh_token_revoked"
                            state.save()
                    dec = handle_auth_dead(ctx, state, tool, exclude=auth_failed)
                else:
                    dec = handle_limit(ctx, state, tool, get=get, exclude=auth_failed,
                                       corroborated=True, hard=hard, reset_at=reset_at,
                                       source=source, detail=detail)
                # The app can continue to supervise a session and record a confirmed rest while
                # automatic hopping is disabled.  In that mode the running child keeps ownership
                # of its terminal and exit code; a later cold start can still safely select a
                # non-resting seat.  This check sits in the shared decision tail so rollout,
                # stdout, the menubar state signal and auth/revocation paths all obey it.
                auto_switch = bool(state.settings().get("auto_switch", True))
                approved = auto_switch and dec.action == "switch" and switches < max_switches
                if realigned and not approved:
                    # Not hopping after all: put the pointer back so a seat the user picked in the
                    # GUI still applies to their next session.
                    state.set_active(tool, prev_active)
                    state.save()

            def _undo_realign() -> None:
                """Same promise as above for the paths that give up AFTER the lock was released:
                we only borrowed ``active`` to name the seat this child runs on, so a decision that
                ends in no hop must not leave the user's own choice overwritten. Re-read under a
                fresh lock and only undo what is still ours — another process may have moved on."""
                if not realigned:
                    return
                with ctx.locked():
                    st = ctx.load_state()
                    if st.active(tool) == launch_email:
                        st.set_active(tool, prev_active)
                        st.save()

            if approved:
                landing = dec.email
                if hard:
                    # HARD means billing/entitlement, and two codex seats routinely share ONE
                    # workspace — a depleted workspace would ping-pong the session between siblings
                    # until MAX_SWITCHES is gone. So prove the landing seat against the live
                    # endpoint first (no lock held — locked()'s contract). The fetch also RESTS a
                    # sibling that is out, so the next signal won't re-pick it either. Shared
                    # account_id is deliberately NOT a veto: the field report had both seats on one
                    # workspace and the sibling member was still usable.
                    skip = set(auth_failed) | ({launch_email} if launch_email else set())
                    sel = _verify_capacity(ctx, tool, get, at=now(), force=True, exclude=skip,
                                           ua=None)
                    if sel.email and sel.available and sel.email != launch_email:
                        landing = sel.email   # the fresh sweep may name a different seat than choose
                    else:
                        _undo_realign()   # the pre-flight refused: this is a no-hop after all
                        if not scan["limit_stay_notified"]:
                            notify(f"{active} hit a hard billing limit and no other {tool} seat is "
                                   f"usable right now (the whole workspace may be out of credits) "
                                   f"— staying on this seat")
                            scan["limit_stay_notified"] = True
                        _start_capacity_watcher(active)
                        clear()
                        scan["next_probe"] = time.monotonic() + PROBE_COOLDOWN_S
                        return False
                # A settings change while the hard-seat pre-flight was in flight wins.  Do this
                # immediately before filling `hit`: the spawn callback stops the child only after
                # this function returns True.
                if not _auto_switch_is_on():
                    _undo_realign()
                    clear()
                    if not scan["auto_switch_off_notified"]:
                        notify(f"{tool} hit a confirmed limit, but auto-switch is off — staying on this seat")
                        scan["auto_switch_off_notified"] = True
                    return False
                hit.update({
                    "reason": reason,
                    "email": landing,
                    "active": active,
                    "hard": hard,
                })
                return True

            # No approved landing means no stop. Clear the rolling match so subsequent ordinary
            # output is not mistaken for a fresh banner; the child keeps its terminal and argv.
            clear()
            scan["next_probe"] = time.monotonic() + PROBE_COOLDOWN_S
            if not auto_switch:
                if not scan["auto_switch_off_notified"]:
                    notify(f"{tool} hit a confirmed limit, but auto-switch is off — staying on this seat")
                    scan["auto_switch_off_notified"] = True
                return False
            if dec.action == "switch":  # a seat exists, but the budget is already spent
                if not scan["budget_notified"]:
                    suffix = (
                        f"; {active} needs you to sign in again" if reason == "auth"
                        else (f"; {active} is no longer entitled"
                              if reason == "revoked" else "")
                    )
                    notify(f"hit the switch limit ({max_switches}){suffix} — staying on this seat")
                    scan["budget_notified"] = True
                return False
            if reason == "auth":
                if not scan["auth_stay_notified"]:
                    if dec.unlocks_at:
                        notify(f"{active} needs you to sign in again 🔑 — the only other {tool} "
                               f"seat is resting until {dec.unlocks_at}; staying on this seat")
                    else:
                        notify(f"{active} needs you to sign in again (token revoked) and no "
                               f"other {tool} seat is ready — re-add it via the app or "
                               f"`acctsw add {tool}`; staying on this seat")
                    scan["auth_stay_notified"] = True
                return False
            if reason == "revoked":
                if not scan["revoked_stay_notified"]:
                    notify(f"{active} is no longer entitled and no other {tool} seat is ready "
                           f"— staying on this seat")
                    scan["revoked_stay_notified"] = True
                return False
            if not scan["limit_stay_notified"]:
                label = "hit a hard billing limit" if hard else "hit its usage limit"
                notify(f"{active} {label}, but no other {tool} seat is ready — staying on this "
                       f"seat")
                scan["limit_stay_notified"] = True
            _start_capacity_watcher(active)
            return False

        while True:
            argv = resume_cmd(ctx, tool) if resuming else build_cmd(ctx, tool, args)
            if resuming and tool == "codex" and resume_thread:
                argv = [argv[0], "resume", resume_thread]
            with ctx.locked():
                launch_email = ctx.load_state().active(tool)
                # A click between the previous handoff and this launch may have moved active.
                # Keep the child's actual credentials and its recorded launch seat together.
                _activate_codex_home(launch_email)
            if launch_email:
                mark_session(ctx.data_dir, tool, launch_email)
            # ``reason`` is set ONLY for a fully approved stop. ``handled`` also covers matches that
            # deliberately leave this child alive, preventing the post-exit safety net from turning
            # that same banner into a re-derived hop after the child later exits on its own.
            hit = {
                "reason": None,       # None | "manual" | "limit" | "auth" | "revoked"
                "email": None,        # exact pre-flight landing seat
                "active": None,       # seat the still-live child was using
                "hard": False,
                "handled": False,
            }
            buf.clear()

            # --- structured signals beside this child -------------------------------------------
            # Snapshot the sessions tree BEFORE the spawn so the child's own rollout file is
            # identified by what moves afterwards (a RESUMED thread keeps appending to its original
            # dated file, so the date in the path proves nothing). Aware datetimes only — the
            # watcher gates every replayed line on its timestamp.
            launch_cwd = os.getcwd()
            spawn_at = now()
            rollout_source = None
            if tool == "codex":
                roots = rollout.sessions_roots(ctx, launch_email)
                rollout_source = rollout.RolloutWatcher(roots, cwd=launch_cwd,
                                                        started_at=spawn_at,
                                                        before=rollout.scan_rollouts(roots))
            tick = {
                "state_mtime": 0,             # last seen st_mtime_ns of state.json (cheap change gate)
                "seen_rev": None,             # last seen state revision (mtime can move without data)
                "blocked": False,             # a confirmed limit could not be acted on (yet)
                "blocked_until": 0.0,         # monotonic: suppress re-deciding an unlandable limit
                "gui_switch_notified": False,
                "healthy_at": None,           # monotonic of the last structured "still has headroom"
                "last_structured": None,      # last structured limit signal (post-exit evidence)
            }

            def _tick_state() -> bool:
                """Engine state as a signal: another process may have rested the seat we run on.

                This is the field regression — the menubar's usage poll rested the exhausted seat
                and made the sibling active, but the live child never hopped because nothing fed
                that back in. One os.stat per tick, a lock-free read only when it changed (state
                writes are atomic temp+os.replace, so a reader never sees a torn file).
                """
                nonlocal seen_manual_switch
                try:
                    m = os.stat(ctx.state_file).st_mtime_ns
                except OSError:
                    return False
                if m == tick["state_mtime"]:
                    return False
                tick["state_mtime"] = m
                snap = ctx.load_state()
                if snap.data.get("rev") == tick["seen_rev"]:
                    return False   # touched but unchanged (e.g. an idle poll's rewrite)
                tick["seen_rev"] = snap.data.get("rev")
                request = snap.data["tools"][tool].get("manual_switch")
                if tool == "codex" and request != seen_manual_switch:
                    seen_manual_switch = request
                    target = request.get("email") if isinstance(request, dict) else None
                    if (target and target != launch_email and target == snap.active(tool)
                            and snap.get_seat(tool, target) is not None
                            and ctx.snapshot_get(tool, target)):
                        # The user chose this exact seat; auto-switch preferences and the
                        # automatic hop budget do not veto an explicit manual action.
                        hit.update(reason="manual", email=target, active=launch_email,
                                   handled=True)
                        return True
                # ALWAYS the seat this child was spawned with: CODEX_HOME is process-global and
                # ``active`` may already point somewhere else.
                seat = snap.get_seat(tool, launch_email) or {}
                until = parse_iso(seat.get("limited_until"))
                # "reactive" is our OWN weakest guess (a stdout match); it must never be read back
                # as if it were someone else's evidence, or a false positive becomes a hop.
                rested_by_other = (until is not None and until > now()
                                   and seat.get("limit_source") in ("usage", "hard"))
                if rested_by_other:
                    if time.monotonic() >= tick["blocked_until"]:
                        return _tick_decide(reason="limit",
                                            hard=(seat.get("limit_source") == "hard"),
                                            reset_at=seat.get("limited_until"),
                                            source=seat.get("limit_source"),
                                            detail=seat.get("limit_detail"))
                elif snap.active(tool) != launch_email and not tick["gui_switch_notified"]:
                    # A healthy seat the user simply re-pointed in the GUI. Never kill for that.
                    tick["gui_switch_notified"] = True
                    notify(f"{tool} is still running on {launch_email}; your new seat applies to "
                           f"the next session")
                return False

            def _tick_decide(**kw) -> bool:
                if _decide_and_maybe_stop(**kw):
                    return True
                # Nowhere to land (or no budget): don't re-decide — and re-notify — every 2s.
                tick["blocked"] = True
                tick["blocked_until"] = time.monotonic() + TICK_BLOCK_S
                return False

            def _tick_recover() -> bool:
                """Stuck on a rested seat — has a sibling come back yet?

                A confirmed limit with nowhere to land leaves the child running, and until now the
                only thing that ever changed afterwards was the advisory watcher PRINTING that a
                seat had freed. The session stayed on the dead seat. The missing half is this: a
                rest usually expires by the CLOCK, with nobody writing state.json at all, so the
                mtime gate in _tick_state can never notice it. Hence a time-driven re-check, but
                only once we are actually blocked and only every TICK_BLOCK_S — otherwise a healthy
                session would parse state on every tick for nothing.

                Same contract as every other path: the seat we run on must still be credibly rested
                (never our own "reactive" guess), there must be a real landing seat, and the switch
                budget must allow it — the decision tail re-checks all three anyway.
                """
                if not tick["blocked"] or time.monotonic() < tick["blocked_until"]:
                    return False
                tick["blocked_until"] = time.monotonic() + TICK_BLOCK_S
                snap = ctx.load_state()
                seat = snap.get_seat(tool, launch_email) or {}
                until = parse_iso(seat.get("limited_until"))
                if (until is None or until <= now()
                        or seat.get("limit_source") not in ("usage", "hard")):
                    return False   # the seat we are on is not (credibly) out — nothing to recover
                if switches >= max_switches:
                    return False
                sel = choose(snap, tool,
                             exclude=set(auth_failed) | ({launch_email} if launch_email else set()))
                if not (sel.email and sel.available):
                    return False   # still nowhere to go; try again after the next TICK_BLOCK_S
                return _tick_decide(reason="limit",
                                    hard=(seat.get("limit_source") == "hard"),
                                    reset_at=seat.get("limited_until"),
                                    source=seat.get("limit_source"),
                                    detail=seat.get("limit_detail"))

            def _tick() -> bool:
                for sig in (rollout_source.poll() if rollout_source is not None else ()):
                    if sig.kind == "healthy":
                        tick["healthy_at"] = time.monotonic()
                        continue
                    tick["last_structured"] = sig   # recorded even when we don't act (post-exit)
                    if time.monotonic() < tick["blocked_until"]:
                        # Already decided this and could not act. A user pressing "continue" on a
                        # maxed seat writes a fresh usage_limit_exceeded per turn, and each one
                        # would otherwise cost a forced fetch (the hard landing pre-flight).
                        continue
                    if not rollout_source.unambiguous:
                        # Two sessions share this cwd, so the file may not be ours. A wrong
                        # attachment must never kill a healthy session on its own — require the
                        # endpoint to agree before acting, on the SAME cooldown stdout probes use
                        # so a burst of lines cannot hammer the endpoint.
                        if time.monotonic() < scan["next_probe"]:
                            continue
                        if _probe("limit") != "confirmed":
                            scan["next_probe"] = time.monotonic() + PROBE_COOLDOWN_S
                            continue
                    if _tick_decide(reason="limit", hard=sig.hard, reset_at=sig.reset_at,
                                    source=("hard" if sig.hard else "usage"), detail=sig.detail):
                        return True
                # (c) engine state, then (d) the clock: a seat that frees while we are stuck must
                # actually carry the session over, not merely be announced.
                return _tick_state() or _tick_recover()

            def on_output(chunk: bytes) -> bool:
                buf.extend(chunk)
                del buf[:-4096]  # keep a rolling tail
                text = buf.decode("utf-8", "replace")
                if detect_refresh_revoked(tool, text):
                    return _decide_and_maybe_stop(reason="auth", refresh_revoked=True,
                                                  clear=buf.clear)
                hard = detect_hard_limit(tool, text)
                if hard:
                    reason = "limit"
                else:
                    if not scan["on"]:
                        return False
                    reason = detect_event(tool, text)  # one ANSI strip per chunk
                    if reason is None:
                        return False
                    hit["handled"] = True
                if reason == "limit" and tick["healthy_at"] is not None \
                        and time.monotonic() - tick["healthy_at"] < PROBE_COOLDOWN_S:
                    # The child ITSELF just recorded near-empty windows in its rollout. Structured
                    # evidence outranks prose, so the text is a false positive — and dismissing it
                    # here costs no network call at all.
                    hit["handled"] = True
                    buf.clear()
                    _dismissed()
                    return False
                if hard:
                    if (rollout_source is not None and rollout_source.attached is not None
                            and rollout_source.unambiguous):
                        # We are tailing THIS child's own session log: the banner is a hint, the
                        # JSONL is the fact. Wait for the structured event instead of killing on a
                        # string whose wording changes between releases. Only a file we know is
                        # ours earns that veto — an ambiguous or provisional attachment must not
                        # silence the fallback, or a wrong guess would disable the banner outright.
                        buf.clear()
                        return False
                    verdict = "confirmed"   # nothing better to consult: the banner is the fallback
                else:
                    if time.monotonic() < scan["next_probe"]:
                        # Same banner/prose redrawn inside the cooldown. It has already been handled,
                        # so neither re-probe nor the exit-time fallback gets a second bite at it.
                        buf.clear()
                        return False
                    verdict = _probe(reason)
                hit["handled"] = True
                if verdict == "dismiss":
                    buf.clear()  # don't re-trip on the text still sitting in the rolling tail
                    _dismissed()
                    return False
                if reason == "limit" and verdict == "unknown":
                    # Endpoint down / 429 / stale token is absence of evidence, never authority to
                    # Ctrl-C a live session. Rate-limit repeated probes without spending the proven
                    # false-alarm budget, and tell the user only once.
                    if not scan["unknown_notified"]:
                        notify(f"{tool}: couldn't verify — staying on this seat")
                        scan["unknown_notified"] = True
                    buf.clear()
                    _unknown()
                    return False
                # Confirmation alone is insufficient — the shared tail decides. Auth banners remain a
                # separate trusted signal: a successful usage call dismissed them above; otherwise the
                # token-dead seat is excluded for every later decision in this run. A 403 reached
                # through a limit banner is the same leave-this-seat decision, but never a rest.
                decision_reason = "revoked" if verdict == "revoked" else reason
                return _decide_and_maybe_stop(reason=decision_reason, hard=hard, clear=buf.clear)

            status = spawn(argv, on_output, on_tick=_tick)  # NO lock held here
            if rollout_source is not None:
                if hit["reason"] == "manual":
                    rollout_source.poll(force_attach=True)
                if (hit["reason"] == "manual" and rollout_source.attached is not None
                        and rollout_source.unambiguous):
                    meta = rollout.session_meta(rollout_source.attached) or {}
                    thread = meta.get("id")
                    if isinstance(thread, str) and thread:
                        resume_thread = thread
                rollout_source.close()

            if hit["reason"] == "manual":
                # The child has flushed its session. Re-read the selection so rapid clicks
                # during shutdown cannot restore an older choice (including a switch back).
                with ctx.locked():
                    state = ctx.load_state()
                    target = state.active(tool)
                    if (not target or state.get_seat(tool, target) is None
                            or not ctx.snapshot_get(tool, target)):
                        notify(f"{tool}: selected seat is no longer available; resume after choosing a seat")
                        return status
                    _commit_switch(state, target)
                    seen_manual_switch = state.data["tools"][tool].get("manual_switch")
                mark_session(ctx.data_dir, tool, target)
                notify(f"{tool}: switching to {target}, resuming your work")
                resuming = True
                continue

            # The child exited right after its OWN log recorded a limit (codex often just ends the
            # turn with `usage_limit_exceeded` and quits). That is positive, structured evidence in
            # hand — it does not need the usage endpoint to agree, which is the difference that
            # matters when the endpoint is 401/429/unreachable. It goes through the SAME decision
            # tail as every live signal, so it realigns the active pointer, pre-flights the landing
            # seat and respects the switch budget exactly like a mid-session hop; when it approves,
            # ``hit`` is filled and the shared commit below carries the work over. Same guards as
            # the safety net: a clean exit is a real completion and an abort is not a limit, so
            # neither is ever second-guessed.
            sig = tick["last_structured"]
            if (hit["reason"] is None and sig is not None and sig.kind == "limit"
                    and status > 0 and status not in _ABORT_EXITS):
                _decide_and_maybe_stop(reason="limit", hard=sig.hard, reset_at=sig.reset_at,
                                       source=("hard" if sig.hard else "usage"), detail=sig.detail)

            if hit["reason"] is None:
                if hit["handled"]:
                    return status  # recognized but intentionally left alive: surface its own exit
                # No stdout limit banner was caught — but a NON-ZERO exit can be a real limit codex
                # surfaced as a plain error (or one that slipped past after scanning turned off). Last
                # resort: confirm via the usage endpoint; if the active seat is genuinely out and a
                # healthy seat is free (and the switch budget allows), hop + resume so the work
                # continues instead of dying on a maxed seat. A clean (0) exit is a real completion —
                # never second-guess it, and a user abort (Ctrl-C/kill) is not a limit — only a
                # POSITIVE, non-abort failure code is worth a usage check. See handle_exhausted.
                with ctx.locked():
                    auto_switch = bool(ctx.load_state().settings().get("auto_switch", True))
                if (status > 0 and status not in _ABORT_EXITS and switches < max_switches
                        and auto_switch):
                    ua = (usage_mod.claude_user_agent(getattr(ctx, "claude_bin", None))
                          if tool == "claude" else None)  # subprocess — before the state flock
                    with ctx.locked():
                        state = ctx.load_state()
                        active = state.active(tool)
                        dec = handle_exhausted(
                            ctx, state, tool, get=get, exclude=auth_failed, user_agent=ua
                        )
                        # handle_exhausted can now hop for TWO different reasons: a real usage limit,
                        # or a 403 (entitlement gone). Decision carries no reason, so read the status
                        # the refresh just persisted — telling a user with a cancelled subscription
                        # that they "hit their usage limit" promises a reset that will never arrive.
                        exit_revoked = ((state.get_seat(tool, active) or {}).get("usage") or {}
                                        ).get("error") == "forbidden" if active else False
                    switched = False
                    if dec.action == "switch":
                        live_identity = _claude_live_identity()  # only for an approved seat hop
                        with ctx.locked():
                            state = ctx.load_state()
                            landing = choose(state, tool, exclude=auth_failed)
                            if (state.settings().get("auto_switch", True)
                                    and state.active(tool) == active and landing.available
                                    and landing.email == dec.email):
                                _commit_switch(state, dec.email, live_identity)
                                switched = True
                    if switched:
                        mark_session(ctx.data_dir, tool, dec.email)
                        why = ("is no longer entitled" if exit_revoked else "hit its usage limit")
                        notify(f"{active} {why} — hopping to {dec.email}, "
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
                        # A revoked active seat is not "resting" — saying so implies waiting will fix
                        # it. Name the real problem so the user knows to sign in / re-subscribe.
                        lead = (f"{active} is no longer entitled and every other {tool} seat is "
                                f"resting" if exit_revoked else f"all {tool} seats are resting")
                        notify(lead
                               + (f"; soonest unlocks at {dec.unlocks_at}" if dec.unlocks_at else ""))
                        return EXIT_GAVE_UP
                return status  # clean exit, or a plain failure — child's real exit code

            # The callback already classified the banner, selected this exact landing seat, and
            # checked the budget while the child was alive. Commit that decision without choose() or
            # another usage fetch on the corpse.
            live_identity = _claude_live_identity()  # slow Claude status happens before the flock
            committed = False
            handoff_blocked = ""
            with ctx.locked():
                state = ctx.load_state()
                # The preference can change after the callback approved the stop but before the
                # child exits.  Do not let that stale approval write a credential swap.
                if not state.settings().get("auto_switch", True):
                    handoff_blocked = "auto-switch was turned off before the handoff completed"
                elif state.active(tool) != hit["active"]:
                    handoff_blocked = "another switch changed the active seat before the handoff completed"
                else:
                    _commit_switch(state, hit["email"], live_identity)
                    committed = True
            if not committed:
                notify(f"{tool} {handoff_blocked}")
                return status
            mark_session(ctx.data_dir, tool, hit["email"])
            reason_msg = (
                "needs you to sign in again 🔑" if hit["reason"] == "auth"
                else ("is no longer entitled" if hit["reason"] == "revoked" else "needs a rest 💤")
            )
            notify(f"{hit['active']} {reason_msg} — hopping to {hit['email']}, "
                   f"your work's coming with you ✨")
            switches += 1
            resuming = True
            continue
    finally:
        watcher_stop.set()
        if watcher_thread is not None:
            watcher_thread.join()
        # Clear before credential reconciliation so observers never report a session whose child has
        # already exited merely because sync-back is waiting on the state lock or filesystem.
        clear_session(ctx.data_dir, tool)
        # On exit, reconcile the active account's creds (the just-run seat may carry a rotated token).
        #  - codex: it maintained its own home via CODEX_HOME → mirror the home into ~/.codex so
        #    plain codex / the GUI follow the active account.
        #  - claude: sync the live keychain item back into the account's snapshot.
        # The Claude sync-back is NOT optional: Claude rotates refresh tokens mid-session, and the
        # live copy is the freshest one (see switch.sync_back's module docstring). Deferring it to
        # "whenever the next switch happens" loses those bytes outright if an out-of-band login
        # replaces the live item first — the stored snapshot then holds a superseded refresh token
        # and the seat silently stops working. What we DO skip is the cost: when the live blob is
        # byte-identical to the stored snapshot there is nothing to preserve, so the common
        # no-rotation launch never pays for `claude auth status`.
        try:
            if tool == "codex":
                with ctx.locked():
                    st = ctx.load_state()
                    active = st.active(tool)
                    if active:
                        blob = ctx.snapshot_get("codex", active)   # home = source of truth
                        if blob:
                            ctx.set_live("codex", blob)             # mirror → ~/.codex
            else:
                with ctx.locked():
                    st = ctx.load_state()
                    active = st.active(tool)
                    # Cheap local keychain reads only — no CLI, no network, no identity probe.
                    rotated = bool(active) and ctx.cred[tool].get_live() != ctx.snapshot_get(
                        tool, active)
                if rotated:
                    live_identity = _claude_live_identity()  # subprocess — never under the flock
                    with ctx.locked():
                        st = ctx.load_state()
                        if sync_back(ctx, st, tool, live_identity=live_identity):
                            st.save()
        except Exception:
            pass
