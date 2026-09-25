# Plan: Lean menubar app + auto-switch engine for Codex & Claude accounts

## Context
You run multiple paid agent subscriptions — **two Codex (ChatGPT) business/team accounts now,
Claude accounts soon**. You want to **automatically switch** between accounts of the same tool
when one hits its usage limit, **continue the in-progress work** on the new account, and when all
accounts are limited, pick the one that **unlocks soonest**. On top of that you want a **lean,
minimal macOS menubar app** for manual control: switch accounts (Codex/Claude), show each
account's limits, and add (login) / remove (logout) accounts — kept minimal but with **some fun &
love personality**. Everything must run on this Mac **without messing up the existing setup**
(stock `codex`/`claude` and the desktop apps keep working; fully reversible).

We chose to **build custom** rather than adopt an existing tool, because (a) no off-the-shelf tool
meets both "continue the same work after switching" *and* "pick the soonest-unlocking account",
and (b) for something touching business OAuth tokens, owning 100% of the code beats trusting a
young low-star third-party app. The design below distills the proven mechanics from the three best
existing projects so we reuse battle-tested patterns without reinventing or trusting them.

> **Design locked.** A design sample defines the menubar UI:
> **"ai guest list"** — a warm guest-list/club theme (seats = accounts, "on the floor" = active,
> "add a seat" = login, "no browser dance"). Full spec in the Menubar section below.
>
> **~~Headroom is integrated as a toggleable usage-saver.~~ Retired.** The plan called for a
> "save credit" toggle routing agents through the [headroom-ai](https://pypi.org/project/headroom-ai/)
> context-compression proxy. It shipped, was measured on real workloads, and saved ~1–3%
> cache-adjusted — a guardrail against runaway outputs, not a money-saver — so it was **removed**.
> The switcher alone is what keeps sessions going. See
> [`SECURITY-headroom.md`](SECURITY-headroom.md) for the measurements and the retired sections below.

---

## Architecture (two layers, one shared store)

```
acctsw (engine, Python 3, stdlib + `security` CLI)        ← all credential/usage/switch logic
   └── used by both the CLI wrappers and the menubar app
menubar app (lean, minimal, fun)                          ← thin UI over the engine
shared store:
   Keychain service "acct-switcher": codex:<email> / claude:<email>  → credential blobs
   ~/.account-switcher/state.json   → non-secret: active acct + cached usage + limited_until
   ~/.account-switcher/backups/     → factory image of original creds (for clean restore)
```

### Engine `acctsw` (the source of truth)
Subcommands (also the menubar's backend, callable with `--json`):
- `install` / `uninstall [--purge] [--dry-run]` — see Install/Uninstall below.
- `add <tool>` — run the official login, snapshot resulting creds → keychain `<tool>:<email>`
  (identity from Codex JWT email / `claude auth status`).
- `remove <tool> <email>` — delete that keychain snapshot (and live creds if it's active).
- `list` / `status [--json]` — accounts, active one, cached usage %, reset countdowns.
- `usage refresh [--tool] [--json]` — fetch live usage (cached, backoff-aware) for the menubar.
- `switch <tool> <email>` — the swap primitive (below). Used by menubar + auto-switch.
- `run codex|claude [args…]` — supervised launcher with auto-switch + resume (below).
- CLI aliases `cx` = `acctsw run codex`, `cl` = `acctsw run claude` (stock **binaries** in
  `~/.local/bin` are never renamed/shadowed — `cx`/`cl` are added alongside, the real `codex`/
  `claude` are untouched on disk).
- **Zero-touch shell aliasing (opt-in, reversible):** for the "it just works" path, install can also
  add shell *aliases* `codex=cx` / `claude=cl` so a plain `codex`/`claude` is supervised
  (auto-switch + resume). This is a reversible shell alias, not a binary rename, and the app is the
  master switch: when it's closed, `cx`/`cl` `exec` the stock tool, so aliased `codex`/`claude`
  behave exactly like stock. The managed rc block is delimited by begin/end markers and removed by
  `acctsw uninstall`.

### Swap primitive (`switch`)
1. **Sync-back first** (longevity-critical — Codex/Claude rotate refresh tokens): copy the
   *current live* creds of the outgoing account back into its keychain snapshot.
2. Install chosen blob into the canonical location **atomically** (temp + `rename()`, preserve `0600`):
   - Codex → `~/.codex/auth.json`; Claude → `security add-generic-password -U -s "Claude Code-credentials"`.
3. Update `state.json.active`.

### Account selection logic (used by `run` and at launch)
Available = `limited_until` null/past. Prefer current active if available; else first available.
If **all limited** → pick **min(reset)** and report `"all limited; <email> unlocks in Xm (HH:MM)"`,
launch anyway (works the moment it resets). "Unlock soonest" uses cached usage reset timestamps.

### Supervised launcher (`run`, the core auto-switch + continuity)
1. Select account, swap creds.
2. Spawn the tool under a **PTY** (stdlib `pty`) so the TUI stays interactive while we tee output.
3. **Attach to Codex's own session log** (`acctsw/rollout.py`): mtime-snapshot
   `<CODEX_HOME>/sessions/**/rollout-*.jsonl` before spawn, diff after, confirm the match via
   `session_meta.cwd` — a *resumed* thread keeps appending to its **original** dated file, so never
   trust the date in the path. (Claude: `--continue`/session id; no structured source yet.)
4. **Tick every ~2 s while the child runs** (cheap, no network) and decide on **structured** signals,
   in this authority order:
   1. **Rollout events** — `task_complete` with `error.codex_error_info == "usage_limit_exceeded"`;
      `token_count` with `rate_limits.rate_limit_reached_type` set (e.g.
      `workspace_member_credits_depleted`), `spend_control_reached`, or a window at 100%.
      `credits.has_credits:false` is **not** a signal — healthy seats report it.
   2. **Usage-endpoint flags** (`acctsw/usage.py`) — `rate_limit.allowed`, `rate_limit_reached_type`,
      `spend_control.reached`, `plan_type`, `reset_after_seconds`: authoritative where percentages
      are blind (credits-depleted returns *null* windows).
   3. **Engine state** — a seat rested by **any** process (menubar poll, another launcher;
      `limit_source` `usage`/`hard`, never our own weak `reactive` guess) makes the **running**
      session hop and resume.
   4. **stdout banners — hint/fallback only** — a match merely *triggers* a usage check, a fresh
      structured "healthy" reading dismisses it, and the hard banner acts alone only when no session
      log is attached.

   Then: set `limited_until`, swap, **relaunch with resume** (`codex resume --last` /
   `claude --resume`). Before a **hard** (credits/spend) hop the landing seat is pre-flighted with a
   fresh usage fetch, so a workspace-wide outage can't ping-pong. Unchanged invariant: never abort a
   live session without a confirmed limit **and** a free landing seat **and** switch budget; a manual
   GUI switch of the active seat never kills a healthy run (one notice: *"still running on `<seat>`;
   your new seat applies to the next session"*).
5. On exit: sync-back refreshed creds, persist state.
   *(Claude has no structured log to read yet — it relies on banners + the usage endpoint — but the
   tick's state check (c) applies to it just the same.)*
   *(For Claude, optionally install Claude Code hooks `Stop`/`PostToolUseFailure` for cleaner triggers.)*

---

## Menubar app — "ai guest list" (design locked)

**Theme & voice:** a warm, lowercase guest-list/club metaphor. Accounts = **seats**; the active
account is **"on the floor"**; usage = **"credit left"**; adding = **"add a seat" / "who's joining
the list?"**; removing = **"wave goodbye"**; tagline **"no browser dance."** Keep it minimal and
charming — exactly the copy in the sample.

**Rendering stack (recommended): menu-bar icon + WebView popover that reuses your HTML/CSS.**
Your design is already HTML/CSS, so we make *that* the actual UI: a `pyobjc` `NSStatusItem` for the
bar dot + a `WKWebView` popover that loads the (cleaned-up) `ai guest list` markup, with a tiny JS↔
Python bridge calling the same `acctsw` engine (`status --json`, `switch`, `add`, `remove`,
toggles). This maximizes reuse of your visual work, keeps all logic in one Python engine, and is far
leaner than re-implementing in SwiftUI. *(Alternative if you later want a fully native feel: a Swift
`MenuBarExtra` + popover shelling to `acctsw --json` — more code, same engine. Recommend WebView now.)*

**Visual system (from the file):** fonts **Hanken Grotesk** (display/body) + **Space Mono** (numbers/
timers); light `#eef1f6` bg / cards `#fff` / ink `#1b212c`, dark navy ramp (`#0c0f15…#232b38`);
per-tool gradients — **Codex coral** `#e0795a`, **Claude blue** `#5b8def`, **fresh/on-floor teal**
`#46c2a8`, **resting yellow** `#f3c969`, **love/needs-hello rose** `#e2778f`. Light/dark toggle.

**Menu-bar dot states** (the at-a-glance signal, copy from sample):
- 🟢 **everyone's fresh** — all seats have credit.
- 🔵 **just switched you** — recent auto-swap (brief).
- 🟡 **a seat's resting** — an account is cooling down (limited); shows "unlocks in Xm".
- 🌸 **needs a hello** — an account needs re-login/re-auth.

**Main popover:**
- Per tool (Codex / Claude): the seat that's **on the floor**, its **credit left** with 5h + weekly
  bars and **reset timers** (Space Mono), and a **switch** button to the other seat.
- **auto-switch** toggle (top-level).
- Footer: **made with ♥**, **settings**, **quit**.

**Add-a-seat flow** ("who's joining the list?" → "how should i sign you in?" → "name this seat" →
"your seat's saved — i'll keep it warm"):
- **Codex:** browser **ChatGPT sign-in** *or* paste **auth.json** (no-browser path).
- **Claude:** **Claude.ai sign-in** only. *(Investigated: `claude setup-token` is an env-var
  inference token that 403s on the OAuth endpoints and doesn't write the Keychain login this app
  snapshots — so there's no working no-browser path for Claude. See PR #32.)*

**Settings (exactly the sample's set):**
- **keep me on the same tool** — *"a Codex limit hops to your other Codex seat, never to Claude."*
  (Locks the within-tool-only switching rule.)
- **tell me when it switches** — gentle Notification Center note: who's on now + why + ETA.
- **restart Codex after a swap** — auto-relaunch the desktop app so it picks up new creds (resolves
  the GUI-restart caveat as a user toggle).
- **little celebrations** — tiny confetti/blip when a seat unlocks or a swap saves your flow.
- **what the menu-bar dot means** — the legend (states above).
- **theme:** dark / light.

**Dev/QA affordances present in the sample** (keep, behind a hidden/debug area): **cap both Codex
seats** + **reset** to simulate limits, and **"or peek at the list i've got"** to preview seats.

## ~~Headroom integration ("save credit" toggle)~~ — retired
Planned, built, measured, removed. The toggle routed agents through a local Headroom
context-compression proxy to stretch each seat's credit. On real traffic the saving was ~1–3%
cache-adjusted (the 10.8% raw reduction was mostly shrinking tokens already billed at the 0.1×
cached rate), and it cost a hundreds-of-MB ML install, a runtime `rtk` download, an unauditable
native blob, a babysat version pin, and invasive edits to `~/.codex/config.toml` /
`~/.claude/settings.json`. Not worth its fragility.

What remains is `acctsw/headroom.py`'s idempotent `cleanup_legacy()`: on the next app launch or
`cx`/`cl` run it strips leftover routing (restoring the snapshotted original config when present),
stops an orphaned proxy by PID file, and deletes the managed venv. Full measurements and the
migration path: [`SECURITY-headroom.md`](SECURITY-headroom.md).

---

## Install & uninstall (both guaranteed non-breaking)
- **`acctsw install`** — idempotent/additive: preflight (binaries, `security`, Python, PATH); create
  `~/.account-switcher/{backups}` + empty `state.json`; **snapshot current live creds verbatim** into
  `backups/` with a sha256 manifest; register the currently-logged-in account as the first switchable
  account; install `acctsw`/`cx`/`cl` into `~/.local/bin` (**stock `codex`/`claude` binaries never
  renamed/shadowed**); print summary + uninstall command. By default it does NOT edit the shell rc —
  it WARNS if `~/.local/bin` isn't on PATH. Shell wiring (PATH + `codex`/`claude` aliases) is opt-in
  via `acctsw install --path` / `acctsw path`, OR done once automatically on first menubar-app launch
  (the app is the install) — both write only a marked, reversible block and surface a notification.
- **`acctsw uninstall [--purge] [--dry-run]`** — sync-back active creds, then **restore**
  `~/.codex/auth.json` and the Claude keychain item from `backups/` (verified vs manifest); remove
  `acctsw`/`cx`/`cl` + menubar app; remove our managed rc block (the begin/end-delimited block we
  added — only ours). Default keeps `~/.account-switcher/` for later reinstall; `--purge` also
  deletes it and the `acct-switcher` keychain items → system exactly as before.
- **Break-safety (both directions):** atomic `rename()` writes, every overwrite preceded by a
  verified backup, rc edits confined to one reversible marked block (opt-in or first-launch, with a
  notification) and removed by uninstall, stock CLI **binaries** never renamed/shadowed (shell
  aliases are reversible and transparent when the app is closed), `--dry-run` prints all actions
  without doing them, two accounts never active at once (sequential).

## Files to create
- `~/.local/bin/acctsw` — engine (Python 3, stdlib only). `~/.local/bin/{cx,cl}` — 1-line wrappers.
- **Menubar app "ai guest list"**: `pyobjc` `NSStatusItem` + `WKWebView` popover loading the
  cleaned-up `ai guest list` HTML/CSS (Hanken Grotesk + Space Mono bundled), JS↔Python bridge to
  `acctsw`. Packaged to a `.app` (e.g. `py2app`). Source under `~/.account-switcher/app/`.
- `~/.account-switcher/` (state + backups) at install; keychain items at `add`.
- No existing file modified except, at switch time, the two canonical credential locations the
  official tools already own.

## Edge cases & safeguards
- **Refresh-token rotation** → mandatory sync-back before every swap and on exit.
- **Claude usage endpoint 429s** → cache, sparse polling, exponential backoff; degrade to
  reactive-only detection if throttled. **Don't assume 7d** — use the returned reset timestamp.
- **Limit message wording changes** (Codex CLI 0.153.4 reworded the out-of-credits banner) → the
  regexes are a hint/fallback only; the trusted signals are the rollout events and the usage flags.
  Launch-time selection works regardless.
- **Two seats, one account** → seats are fingerprinted by the ChatGPT *user* id, so one person
  signed in twice (Gmail `+alias`) is warned as one quota, while Team/Business members of one
  workspace stay separate seats with their own 5h/weekly windows (only credits pool there, and
  the launcher's hard-limit landing pre-flight already proves the landing seat).
- **auth.json race during refresh** → atomic swap only between child runs.
- **GUI apps** → the menubar's usage poll switches a confirmed-limited active seat only after a
  fresh check proves another same-tool seat has headroom. Manual switching uses the same credential
  primitive. The Codex restart setting gracefully quits and reopens an already-running desktop app;
  continuing its existing thread may require sending a message. Terminal supervisors resume their
  own sessions. The app does not force-quit a desktop app that refuses to exit.
- **Usage freshness** → show both 5h and weekly windows, resets, and last-success age on every seat.
  While the popover is open, active seats refresh every 30 seconds; background and inactive seats
  keep a three-minute cadence. Backoff takes priority over that target. Network fetches run outside
  the state lock, with identity checks before committing results; overlapping polls are coalesced.

## Verification (end-to-end)

CI runs the isolated engine and native callback tests on macOS 14/15/26, including Intel and Apple
silicon, with Python 3.11 and 3.13. Intel and Apple-silicon jobs also build the standalone app and
verify its executable architecture. Tests inject provider responses, Keychain contents, app
identities, and process identities; no developer account or Codex installation is required. Actual
process inspection is a separate integration test and skips where the host prohibits it. GitHub's
hosted runners do not cover macOS 12/13, so those advertised minimum versions still require manual
release verification. Desktop restart resolves Codex by bundle identifier rather than display name
or installation directory.

1. `acctsw install` (dry-run first) → backups + manifest written, stock `codex`/`claude` unaffected.
2. `acctsw add codex` ×2 → `acctsw list` shows both emails (cross-check via JWT / `claude auth status`).
3. `acctsw usage refresh --json` → real 5h/weekly % + reset times for each account (Codex `wham/usage`,
   Claude `oauth/usage` with required headers). Menubar shows them.
4. **Switch**: `acctsw switch codex <b>` → `codex login status` + JWT confirm B; switch back → A;
   Codex.app reflects active account after restart.
5. **Continuity dry-run** (no real limit): start `cx`, one turn, note rollout uuid; `Ctrl-C`;
   `acctsw switch codex <b>`; `codex resume <uuid>` → same conversation continues under B.
6. **Limit path**: use isolated test accounts and injected usage responses to exercise 100%,
   provider out flags, no healthy spare, disabled auto-switch, and concurrent terminal handoff.
   Verify a 97% reading alone does not move the account. On a naturally occurring live limit,
   confirm the destination identity and session continuation; do not edit the live store to force it.
7. **Legacy Headroom cleanup**: on a machine that had "save credit" on, the next app launch or
   `cx`/`cl` run leaves no `model_provider = "headroom"` in `~/.codex/config.toml` and no loopback
   `ANTHROPIC_BASE_URL` in `~/.claude/settings.json`; plain `codex`/`claude` reach the provider
   directly. Cleanup never changes account state.
8. **App**: "ai guest list" popover renders the design (fonts/colors/themes), dot reflects state
   (fresh/resting/needs-hello), switch + add-a-seat + auto-switch drive the engine; notifications
   fire on auto-switch.
9. `acctsw uninstall` → `~/.codex/auth.json` + Claude keychain match `backups/` originals;
   `--purge` leaves no trace (app, engine, state, keychain items all gone).

---

## Implementation invariants (review-driven; honor in later milestones)
- **Add-a-seat orchestration (M4/M6) MUST `sync_back(active)` BEFORE invoking the official login.**
  The official `codex login` / `claude auth login` overwrites the live creds, so the previously
  active seat's (possibly rotated) tokens must be snapshotted first or they are lost. `add()` runs
  *after* login and cannot recover them.
- **Unattended switching (M4 launcher) uses the Codex live-vs-active guard** in `switch.sync_back`
  (skip sync-back when live creds belong to a different account than `state.active`).
- **Per-account Codex homes hold exactly one real file, `auth.json`.** Everything else in
  `~/.account-switcher/ch/<id>/` is a symlink into the shared `~/.codex`, which stays the
  source of truth for config and sessions. **SQLite sidecars (`-wal`/`-shm`/`-journal`) are never
  linked** — SQLite resolves a symlinked database and writes them beside the real file, while a real
  database next to linked sidecars fails every open with error 14. Real files a supervised child
  created in a home are **promoted** into `~/.codex` on the next launch with no live codex session
  (moving files under a running child is not worth the risk); a copy whose name `~/.codex` already
  has is **parked** as `<name>.orphaned-<stamp>` — never deleted, never linked again.
- **A home is named by hash and its root is two letters** because codex 0.157+ binds its app-server
  daemon at `<CODEX_HOME>/app-server-control/app-server-control.sock` and connects to that literal
  resolved path, which macOS caps at 104 bytes (`SUN_LEN`) — the old
  `codex-homes/<address>` layout blew that budget and broke every launch. `codex-homes/<address>`
  survives as a **by-address symlink** into `ch/<id>` (the index, and what a pre-1.0.2 child still
  resolves through). **The daemon's runtime state is per-seat** — `app-server-control/` and
  `app-server-daemon/` are never linked in from `~/.codex` and never promoted out to it, since one
  daemon serves exactly one account's credentials.
- **All timestamps are tz-aware** (`parse_iso` coerces naive→UTC) so selection comparisons never
  raise.
- **Real usage shapes (verified live):** Claude `oauth/usage` → `five_hour`/`seven_day`
  `{utilization, resets_at}`. Codex `wham/usage` → `rate_limit.{primary,secondary}_window`
  `{used_percent, reset_at(epoch)}` plus `rate_limit.limit_reached`.
