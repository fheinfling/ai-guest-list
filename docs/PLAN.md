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

> **Design locked.** Your sample `~/Downloads/ai guest list.html` defines the menubar UI:
> **"ai guest list"** — a warm guest-list/club theme (seats = accounts, "on the floor" = active,
> "add a seat" = login, "no browser dance"). Full spec in the Menubar section below.
>
> **Headroom is integrated as a toggleable usage-saver.** It's a token/context-compression layer
> ([headroomlabs-ai/headroom](https://github.com/headroomlabs-ai/headroom), Apache-2.0, 52.8k★) —
> not an account manager, but turning it on compresses what each agent reads (tool outputs, history,
> files) → **fewer tokens → you hit usage limits slower**, complementing the switching. The app
> exposes a simple on/off toggle. See "Headroom integration" below.

---

## Requirements distilled from the 3 reference apps

Reviewed: **[CAAM](https://github.com/Dicklesworthstone/coding_agent_account_manager)** (139★, Go,
most mature, multi-tool, non-destructive uninstall), **[cux](https://github.com/inulute/cux)**
(28★, Go, Claude-only, best at resume-same-conversation), **[Symbioose
claude-account-switcher](https://github.com/Symbioose/claude-account-switcher)** (38★, Python,
native Mac menubar for both tools). What we take from each:

- **Credential model = local blob swap** (all three). Never proxy traffic, never transmit tokens.
  The official CLI/app still does all networking with official endpoints → low security surface.
- **Login/logout = shell out to the official flow** then snapshot (all three). `claude auth login`
  / `codex login`; we capture the resulting creds. No custom OAuth.
- **Keychain naming** (Symbioose): `claude-switcher:{email}`, `codex-switcher:{email}`; active
  Claude = keychain `Claude Code-credentials`; active Codex = `~/.codex/auth.json` (honors `$CODEX_HOME`).
- **Continuity** (cux): on limit, swap creds transactionally then **relaunch with resume** — Claude
  `claude --resume <id>`, Codex `codex resume <uuid>`. Session history is local, so it survives the
  account swap. cux drives this via Claude Code hooks (`Stop`, `PostToolUseFailure`, `SessionStart`,
  `UserPromptSubmit`); we can use the same hook surface for Claude and a PTY wrapper for Codex.
- **Non-destructive uninstall** (CAAM): keep `_original` backups, restore on uninstall, require an
  explicit flag to delete saved profiles.
- **Usage/limit reading — the hard part none documents well, endpoints recovered from source/issues:**
  - **Codex/ChatGPT:** `GET https://chatgpt.com/backend-api/wham/usage` and
    `…/backend-api/accounts`, `Authorization: Bearer <access_token>` (+ ChatGPT account-id header).
    Returns the 5h + weekly windows with percent + reset. Local `rollout-*.jsonl` `rate_limits` is
    often `null` → API is the reliable source.
  - **Claude:** `GET https://api.anthropic.com/api/oauth/usage` → `five_hour` + `seven_day` objects
    with utilization % and reset timestamps. **Must send** `anthropic-beta: oauth-2025-04-20`
    (else 401) and `User-Agent: claude-code/<version>` (else aggressive 429). Endpoint rate-limits
    hard → **cache + poll sparingly** (e.g. ≤ every 2–5 min, exponential backoff on 429). Note the
    "weekly" window empirically resets ~72h, not 7d — read the returned reset timestamp, don't assume.
- **Detection strategy** (gap in all three → our improvement): combine **reactive** (catch the
  limit error in the wrapped session = reliable trigger) with **cached polling** of the usage
  endpoints (powers the menubar display *and* the soonest-unlock choice). Neither alone is enough.

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
3. Track session id (Codex: newest `~/.codex/sessions/**/rollout-*.jsonl` after spawn; Claude:
   `--continue`/session id).
4. **On limit** (regex match on the tee'd limit message → also captures reset time, or a 429 in the
   usage poll): set `limited_until`, swap to next/soonest account, **relaunch with resume**
   (`codex resume <uuid>` / `claude --resume <id>`) so the work continues. Loop.
5. On exit: sync-back refreshed creds, persist state.
   *(For Claude, optionally install cux-style hooks `Stop`/`PostToolUseFailure` for cleaner triggers.)*

---

## Menubar app — "ai guest list" (design locked from your sample)

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
- **Headroom "save credit" toggle** (see below).
- Footer: **made with ♥**, **settings**, **quit**.

**Add-a-seat flow** ("who's joining the list?" → "how should i sign you in?" → "name this seat" →
"your seat's saved — i'll keep it warm"):
- **Codex:** browser **ChatGPT sign-in** *or* paste **auth.json** (no-browser path).
- **Claude:** **Claude.ai sign-in** *or* **`claude setup-token`** long-lived token (no-browser path).
- These map to engine `add codex` / `add claude`; both honor the "no browser dance" promise.

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

## Headroom integration ("save credit" toggle)
- **What it does for us:** when ON, agents run through Headroom so their context is compressed →
  fewer tokens consumed → usage limits are reached more slowly (stretches each seat's credit).
- **Mechanism:** the supervised launcher conditionally routes the agent through Headroom when the
  toggle is on — `headroom wrap codex|claude …` (inner wrap, *inside* our PTY/cred layer), or
  Headroom proxy mode if preferred. When off, the agent runs directly. The toggle just flips an env/
  flag the `run` path reads; no restart of our app needed.
- **Install/detect:** the engine detects whether `headroom` is installed; the toggle offers a
  one-time `pip install "headroom-ai[all]"` (or `npm i -g headroom-ai`) if missing. Headroom stays
  **optional** — everything works without it; it's purely a credit-saver.
- **UI:** a single labeled switch in the main popover (themed copy, e.g. *"slow sips — make the
  credit last"*), plus a small note that it compresses context to save tokens. Optionally surface
  Headroom's `stats` (tokens saved) as a tiny "credit stretched" line — nice-to-have, not required.
- **Safety:** Headroom only sits in the data path of the wrapped CLI; it never touches credentials
  or the keychain. Toggling it never affects account state.

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
- Headroom is an external dependency installed on demand (`pip`/`npm`), not vendored.
- No existing file modified except, at switch time, the two canonical credential locations the
  official tools already own.

## Edge cases & safeguards
- **Refresh-token rotation** → mandatory sync-back before every swap and on exit.
- **Claude usage endpoint 429s** → cache, sparse polling, exponential backoff; degrade to
  reactive-only detection if throttled. **Don't assume 7d** — use the returned reset timestamp.
- **Limit message format unknown until first hit** → regex in a top config block; capture the real
  strings during verification and lock them in. Launch-time selection works regardless.
- **auth.json race during refresh** → atomic swap only between child runs.
- **GUI apps** → menubar `switch` swaps the same creds the apps read; they may need a restart (app
  reads creds at launch). Auto-detection stays CLI-driven (best-effort GUI, as agreed).

## Execution: private repo + team mode with a standing reviewer
- **Repo:** new folder `~/Documents/GitHub/ai-guest-list/` (kebab to avoid space-in-path build
  pain; product display name stays "ai guest list"). `git init`, then
  `gh repo create ai-guest-list --private --source=. --remote=origin` (gh authed as `fheinfling`,
  has `repo` scope). Sensible `.gitignore` (venv, build artifacts, `*.app`, secrets) — **no creds
  ever committed**. Conventional commits per milestone.
- **Build env:** a project `.venv` (Homebrew Python; system 3.9 lacks pyobjc) with `pyobjc`,
  `py2app`; `headroom` installed on demand only.
- **Team mode (build ↔ continuous review loop):** I orchestrate and implement milestone by
  milestone; a **standing reviewer agent** (kept alive via SendMessage so it retains context across
  the whole build) reviews **after every milestone** against this plan — checking architecture
  alignment, credential-safety/non-destructiveness, atomicity, and the design fidelity to "ai guest
  list". **All findings fixed immediately, then re-reviewed** until the milestone is clean before
  moving on. Where milestones are independent, build sub-agents run in parallel.
- **Milestones (each: build → review → fix → re-review → commit):**
  1. Repo scaffold + `.gitignore` + README + venv + CI-less smoke harness.
  2. `acctsw` engine: state store, keychain swap primitive (sync-back + atomic), account selection
     (incl. soonest-unlock), `add/remove/list/status --json`.
  3. Usage readers: Codex `wham/usage`, Claude `oauth/usage` (required headers, cache + backoff).
  4. Supervised launcher `run` + `cx`/`cl`: PTY tee, limit detection, switch + **resume continuity**.
  5. `install`/`uninstall` (`--dry-run`/`--purge`) with factory-image backups + restore.
  6. Menubar app "ai guest list": `NSStatusItem` + `WKWebView` popover from your HTML/CSS, JS↔engine
     bridge, dot states, add-a-seat, toggles, settings, light/dark, notifications, packaging.
  7. **Headroom toggle** wired into `run` (wrap on/off, detect/install).
  8. End-to-end verification pass (below) + reviewer sign-off.

## Verification (end-to-end)
1. `acctsw install` (dry-run first) → backups + manifest written, stock `codex`/`claude` unaffected.
2. `acctsw add codex` ×2 → `acctsw list` shows both emails (cross-check via JWT / `claude auth status`).
3. `acctsw usage refresh --json` → real 5h/weekly % + reset times for each account (Codex `wham/usage`,
   Claude `oauth/usage` with required headers). Menubar shows them.
4. **Switch**: `acctsw switch codex <b>` → `codex login status` + JWT confirm B; switch back → A;
   Codex.app reflects active account after restart.
5. **Continuity dry-run** (no real limit): start `cx`, one turn, note rollout uuid; `Ctrl-C`;
   `acctsw switch codex <b>`; `codex resume <uuid>` → same conversation continues under B.
6. **Limit path**: on a real limit, capture exact message + reset wording, set regex, confirm
   auto-switch + resume. Force all-limited by hand-editing `state.json` → verify soonest pick +
   countdown message.
7. **Headroom toggle**: with it ON, `cx`/`cl` route through `headroom wrap` (verify via Headroom
   `stats` that tokens drop); OFF → direct. Toggling never changes account state.
8. **App**: "ai guest list" popover renders the design (fonts/colors/themes), dot reflects state
   (fresh/resting/needs-hello), switch + add-a-seat + auto-switch + save-credit toggles drive the
   engine; notifications fire on auto-switch.
9. `acctsw uninstall` → `~/.codex/auth.json` + Claude keychain match `backups/` originals;
   `--purge` leaves no trace (app, engine, state, keychain items all gone; Headroom left as-is).

---

## Implementation invariants (review-driven; honor in later milestones)
- **Add-a-seat orchestration (M4/M6) MUST `sync_back(active)` BEFORE invoking the official login.**
  The official `codex login` / `claude auth login` overwrites the live creds, so the previously
  active seat's (possibly rotated) tokens must be snapshotted first or they are lost. `add()` runs
  *after* login and cannot recover them.
- **Unattended switching (M4 launcher) uses the Codex live-vs-active guard** in `switch.sync_back`
  (skip sync-back when live creds belong to a different account than `state.active`).
- **All timestamps are tz-aware** (`parse_iso` coerces naive→UTC) so selection comparisons never
  raise.
- **Real usage shapes (verified live):** Claude `oauth/usage` → `five_hour`/`seven_day`
  `{utilization, resets_at}`. Codex `wham/usage` → `rate_limit.{primary,secondary}_window`
  `{used_percent, reset_at(epoch)}` plus `rate_limit.limit_reached`.
