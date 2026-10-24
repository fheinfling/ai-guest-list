"""Converge duplicate Claude credential items without ever spending a refresh token.

The menubar's three-second state timer scans stock + registered homes (one Keychain read each).
Changed bytes and due unknown-owner retries get a profile request, outside the store flock. Evidence lives solely in
memory, per Context and item; a restart verifies again. Unknown owners never join a group. A home
whose proven owner differs from its seat is quarantined for seat use, never relabelled. Its
physical item still joins its PROVEN owner's group so it cannot invalidate that owner's copies.

One changed, proven item wins over unchanged same-owner copies. On first observation, or when
several different copies changed, the unique greatest finite expiresAt wins, provided ALL contenders
have an expiry. A tie/missing expiry is ambiguous: write nothing until further evidence arrives.
Every candidate must pass the profile endpoint. We re-read source and target immediately before
writes under acctsw's cross-process flock. This is a cooperative compare-and-swap: Keychain has no
atomic CAS against Claude, so its final read/write window remains a documented race. Snapshots have
the same guard and follow the winner. No credential bytes or profile bodies enter state or logs.

Three seconds costs N+1 local Keychain reads, no HTTP for stable proven items. Unknown owners retry
after 3, 6, 12, ... seconds, capped at 300 seconds (about 12 requests/hour/item at the cap).
A valid Retry-After is a minimum delay, even above the cap. Changed bytes reset the backoff;
explicit post-child verification bypasses it. Missing items never retry. All deadlines and proofs
are memory-only; a restart reproves each item. This is short relative to
Claude's five-minute proactive refresh window, but cannot eliminate simultaneous refreshes across
items. The binary can also clear invalid-grant tokens on disk; unidentifiable cleared items are
never overwritten using historical owner guesses. Re-add such a seat if it cannot recover.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import threading
from time import monotonic

from . import identity
from .util import email_key

@dataclass(frozen=True)
class Seen:
    blob: str | None
    email: str | None


@dataclass(frozen=True)
class Retry:
    delay: float
    at: float


class Tracker:
    def __init__(self):
        self.seen: dict[str | None, Seen] = {}
        self.proofs: dict[str | None, Seen] = {}
        self.retries: dict[str | None, Retry] = {}
        self.lock = threading.RLock()


def tracker(ctx):
    # Context creation is single-threaded; setdefault also keeps callers on the same tracker.
    return ctx.__dict__.setdefault('_claude_tracker', Tracker())


def cached_identity(ctx, *, email=None):
    """Reuse positive OR unknown evidence only while the item's exact bytes remain unchanged."""
    item = ctx.claude_cred(email) if email else ctx.cred['claude']
    t = tracker(ctx)
    with t.lock:
        current = item.get_live()
        for seen in (t.proofs.get(email), t.seen.get(email)):
            if seen and seen.blob == current:
                return identity.ClaudeLiveIdentity(current, seen.email)
    return identity.claude_live_identity(ctx, email=email)


def expiry(blob):
    try:
        value = json.loads(blob)['claudeAiOauth']['expiresAt']
        return value if type(value) in (int, float) and math.isfinite(value) else None
    except (ValueError, TypeError, KeyError):
        return None


def newest(blobs):
    distinct = set(blobs)
    if not distinct:
        return None
    if len(distinct) == 1:
        return next(iter(distinct))
    values = [(expiry(b), b) for b in distinct]
    if any(e is None for e, _ in values):
        return None
    top = max(e for e, _ in values)
    winners = [b for e, b in values if e == top]
    return winners[0] if len(winners) == 1 else None


def quarantine(ctx, state, email, *, save=True):
    """Keep the item for explicit repair, but make it unusable through any acctsw read/launch."""
    from .util import atomic_write_text
    home = ctx.claude_home(email)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_text(home / '.acctsw-quarantined', '1', mode=0o600)
    (home / '.acctsw-unverified').unlink(missing_ok=True)
    seat = state.get_seat('claude', email)
    if seat is not None:
        seat['auth_error'] = True
        if save:
            state.save()
    return seat is not None


def converge(ctx, *, verify: str | None = None):
    """Unlocked boundary: inspect changed items, resolve owners, then CAS under the store flock.

    verify forces one post-child probe even if an expired migration seed stayed byte-identical.
    Network/Keychain failures are intentionally opaque at this boundary (never surface secrets).
    """
    if getattr(ctx, '_store_lock_held', lambda: False)():
        raise RuntimeError('Claude convergence requires an unlocked store')
    t = tracker(ctx)
    with t.lock:
        previous = t.seen.copy()
        try:
            _converge(ctx, verify=verify)
        except Exception:
            # Preserve changed-item evidence for retry; retain exact-byte profile proofs so
            # retrying a failed write never repeats HTTP for unchanged credential bytes.
            # Backend and transport exceptions can include tokens; discard them entirely.
            t.seen = previous
            return


def _converge(ctx, *, verify=None):
    from . import accounts
    t = tracker(ctx)
    with t.lock:
        state = ctx.load_state()
        seats = state.accounts('claude')
        if not seats:
            return
        locations = {None: ctx.cred['claude'], **{e: ctx.claude_cred(e) for e in seats}}
        blobs = {key: item.get_live() for key, item in locations.items()}
        clock = monotonic()
        changed = {key for key, value in blobs.items()
                   if key not in t.seen or t.seen[key].blob != value
                   or (key in t.retries and clock >= t.retries[key].at)}
        if not changed and verify is None:
            return
        snapshots = {e: ctx.snapshot_get('claude', e) for e in seats}
        observed = {}
        for key, value in blobs.items():
            if key in changed or (verify is not None and key == verify):
                proof = t.proofs.get(key)
                retry = t.retries.get(key)
                same = proof is not None and proof.blob == value
                if (same and (proof.email or (retry and clock < retry.at))
                        and (verify is None or key != verify)):
                    observed[key] = proof
                else:
                    answer = identity.claude_live_identity(ctx, email=key) if value else None
                    # The resolver reads again; bind only an answer about our original bytes.
                    owner = answer.email if answer and answer.blob == value else None
                    observed[key] = Seen(value, email_key(owner) if owner else None)
                    t.proofs[key] = observed[key]
                    if value and not owner:
                        delay = min(retry.delay * 2, 300) if same and retry else 3
                        hint = answer.retry_after if answer else None
                        wait = max(delay, hint) if type(hint) in (int, float) and math.isfinite(hint) else delay
                        # Count from completion, so a slow request still gets the full delay.
                        t.retries[key] = Retry(delay, monotonic() + wait)
                    else:
                        t.retries.pop(key, None)
            else:
                observed[key] = t.seen[key]
        t.seen = observed.copy()
        t.retries = {key: retry for key, retry in t.retries.items() if key in locations}
        with ctx.locked():
            state = ctx.load_state()
            dirty = False
            try:
                groups = {}
                for key, seen in observed.items():
                    if not seen.blob or not seen.email or locations[key].get_live() != seen.blob:
                        continue
                    if key is not None:
                        if key not in state.accounts('claude'):
                            continue
                        if seen.email != email_key(key):
                            dirty = quarantine(ctx, state, key, save=False) or dirty
                        else:
                            (ctx.claude_home(key) / '.acctsw-unverified').unlink(missing_ok=True)
                        # Quarantine blocks seat use, not proven same-owner token coordination.
                    groups.setdefault(seen.email, []).append(key)
                for owner, keys in groups.items():
                    contenders = [k for k in keys if k in changed] or keys
                    winner = newest([observed[k].blob for k in contenders])
                    if winner is None:
                        continue
                    source = next(k for k in contenders if observed[k].blob == winner)
                    for key in keys:
                        if key is None and state.data["tools"]["claude"].get("stock_logged_out"):
                            continue
                        if (locations[source].get_live() != winner
                                or locations[key].get_live() != observed[key].blob):
                            continue
                        if observed[key].blob != winner:
                            locations[key].set_live(winner)
                            t.seen[key] = Seen(winner, owner)
                            t.proofs[key] = t.seen[key]
                            if key is None:
                                # Our propagation is a mirror update, not an out-of-band selection.
                                state.data['tools']['claude']['captured_stock_digest'] = accounts._claude_digest(winner)
                                dirty = True
                    for email in state.accounts('claude'):
                        if email_key(email) != owner:
                            continue
                        if (ctx.claude_home(email) / '.acctsw-quarantined').exists():
                            continue
                        if (locations[source].get_live() == winner
                                and ctx.snapshot_get('claude', email) == snapshots.get(email)
                                and snapshots.get(email) != winner):
                            ctx.snapshot_set('claude', email, winner)
                            accounts._creds_refreshed(state, 'claude', email)
                            dirty = True
            finally:
                # Preserve completed state changes even if a later Keychain write fails.
                if dirty:
                    state.save()
