# Architecture & Design: ai guest list

> **Current architecture reference** (kept at `docs/PLAN.md` so existing links resolve).
> Ground truth lives in `acctsw/` (the Python engine) and `app/` (the macOS menubar app).

---

## 1. Overview & Design Invariants

**ai guest list** is a macOS menubar app and CLI supervisor that manages multiple **OpenAI Codex** (`codex` CLI and `Codex.app`) and **Anthropic Claude Code** (`claude` CLI) subscription accounts ("seats"), plus optional pay-per-token **API-key seats** (BYOK). When an active subscription seat hits a usage limit or authentication failure—or when the user manually picks another seat—the supervisor can move the session to an eligible target seat and continue the conversation, subject to the guards below.

### Core invariants

1. **Zero modification of stock binaries.** Stock `codex`, `claude`, and `Codex.app` are never patched, renamed, or replaced on disk. The managed shell block (`# >>> ai guest list (acctsw) >>>`) puts `~/.local/bin` first on `PATH` and, with shell supervision enabled, adds `alias codex=cx` and `alias claude=cl`. The installed wrappers are `acctsw`, `cx`, and `cl`. The block goes in `~/.zshrc` for zsh; for bash, `~/.bash_profile` unless only `~/.bashrc` already exists; otherwise `~/.profile` (`acctsw/install.py`). When the menubar app is not running, ordinary work invocations through `cx` / `cl` exec the stock binary directly (explicit `--key` pins stay supervised while the app is closed).
2. **Secrets stay in Keychain and tool-native stores.** Codex subscription credentials are real `0600` files at `ch/<hash>/auth.json`; Claude snapshots, API keys, and factory credential backups are stored in the macOS Keychain. `state.json` stores non-secret metadata, usage readings, settings, and 4-character API-key fingerprints. State saves use atomic temp-file + `os.replace` with mode `0600`; an `fcntl.flock` store lock (`.lock`) protects state read-modify-write transactions, not every credential or filesystem write. The lockfile uses the process's default umask.
3. **Structured signals first, banners as hints and fallback.** Limit detection uses Codex rollout JSONL (`token_count` and `task_complete.error.codex_error_info`), Claude per-session proxy responses, official usage API responses, and shared state. Terminal banner regexes remain hints that trigger probes, with specific fallback paths; an unambiguously attached Codex rollout vetoes the hard-limit banner fallback (`acctsw/launcher.py`). Claude transcripts supply resume IDs, not limit signals.
4. **Reversible installation.** First-time setup (`acctsw install`) captures a one-time factory backup of any pre-existing stock credentials into the Keychain (`__factory__:codex`, `__factory__:claude`) and records non-secret hashes in `~/.account-switcher/backups/manifest.json`. Running `acctsw uninstall` restores the original identity, preferring its freshest credentials over the factory backup, and removes the shell block.

---

## 2. Store & Credential Layout

### macOS Keychain

| Service | Account key | Contents |
|---|---|---|
| `"acct-switcher"` | `claude:<email>` | Snapshot of Claude OAuth JSON (`claudeAiOauth`) for a Claude subscription seat |
| `"acct-switcher"` | `key:<id>` | Raw API key secret for a BYOK key seat (`acctsw/keyseats.py`) |
| `"acct-switcher"` | `__factory__:codex` | Pre-install factory backup of stock `~/.codex/auth.json` (`acctsw/install.py`) |
| `"acct-switcher"` | `__factory__:claude` | Pre-install factory backup of stock `"Claude Code-credentials"` (`acctsw/install.py`) |
| `"Claude Code-credentials"` | macOS username | Default stock Claude Code login (mirrored to the active seat unless locally logged out) |
| `"Claude Code-credentials-<8-hex>"` | macOS username | Per-seat Claude home login (`sha256(NFC(clh_path))[:8]`, `acctsw/claudehome.py`) |

### Filesystem (`~/.account-switcher/`, mode `0700`)

```text
~/.account-switcher/
├── state.json                 # 0600: version 1 state (tools, seats, keys metadata, settings, prompts)
├── .lock                      # default umask: fcntl.flock for state read-modify-write transactions
├── pricing.json               # 0600: cached provider model catalogs (sanitized with redact_key)
├── backups/
│   └── manifest.json          # 0600: non-secret SHA-256 + email manifest for __factory__:<tool> backups
├── ch/<8-hex-sha256>/         # 0700: short-path homes (keeps Unix domain sockets under 104-byte SUN_LEN)
│   ├── (subscription Codex)   #   real auth.json + private app-server daemon dirs; symlinks to ~/.codex
│   └── (BYOK key / pin home)  #   isolated CODEX_HOME or CLAUDE_CONFIG_DIR; NO symlinks to shared homes
├── codex-homes/<email>        # by-address symlink into ch/<hash>/
├── clh/<8-hex-sha256>/        # 0700: per-seat Claude homes (CLAUDE_CONFIG_DIR)
│   ├── .claude.json           #   per-seat identity/caches + 3-way merged shared preferences
│   └── projects, skills, ...  #   symlinks to shared ~/.claude/<entry>
├── session-<tool>-<pid>.json   # 0600: live supervised session records
├── session-<tool>.json         # 0600: legacy session record
├── thread-<tool>-<pid>.json    # 0600: claimed thread ID
├── updates/                   # 0700: staging directory for in-app updates
├── update.json                # updater status
├── update-ack                 # startup acknowledgement containing the running version
└── update-helper.log          # detached update helper output
```

### Per-seat homes (`ch/` and `clh/`)

- **Codex subscription homes (`acctsw/codexhome.py`):** Each Codex seat gets `~/.account-switcher/ch/<8-hex>` (hash of normalized email). Its `auth.json` is a real `0600` file. `app-server-control/`, `app-server-daemon/`, `app-server-startup.lock`, and `packages/` stay private to the seat so concurrent seats never share a background daemon or overwrite each other's tokens. Shared history, sessions, and `config.toml` are symlinked to `~/.codex`.
- **Claude subscription homes (`acctsw/claudehome.py`, `acctsw/claudesync.py`):** Each Claude seat runs with `CLAUDE_CONFIG_DIR=~/.account-switcher/clh/<8-hex>`. Because Claude Code derives its Keychain service name from `CLAUDE_CONFIG_DIR`, each seat gets its own `"Claude Code-credentials-<8-hex>"` Keychain item and never logs out another seat. Shared directories (`projects`, `skills`, `agents`, `plugins`, `commands`, `hooks`, `tasks`, `todos`, `plans`, `file-history`, `session-env`, `shell-snapshots`, `debug`, `ide`) are symlinked to `~/.claude`, while `.claude.json` performs a locked 3-way merge of shared preference keys (`SHARED_CONFIG` and `SHARED_PROJECT_CONFIG`) with `~/.claude.json` while keeping account identity and policy caches private. `claudesync.py` reconciles OAuth token refreshes across the per-seat Keychain item, the `acct-switcher` snapshot, and (for the active seat) the stock Keychain item.
- **BYOK key homes (`acctsw/keyhome.py`):** Key sessions use `~/.account-switcher/ch/<8-hex>` derived from `key:<harness>:<seat_id>` (or `key:<harness>:<seat_id>:<pin>` for dedicated terminal pins). Key homes contain **no symlinks** to `~/.codex` or `~/.claude`. To prevent the harness from persisting the child's API-key environment variable to disk or leaking it to tool subprocesses:
  - Codex key homes write a private `config.toml` with `shell_snapshot = false` and `shell_environment_policy` (`inherit = "all"`, `ignore_default_excludes = false`, `exclude = [<env_key>]`, where `<env_key>` is the provider's key variable), and disable analytics and otel exporters.
  - Claude key homes write a private `settings.json` containing only `{"skipWebFetchPreflight": true}` and set `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` and `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` in the child's environment.

---

## 3. Sign-In, Logout Protection & Warm-Up

- **Sync-back before mirror changes (`acctsw/switch.py`, `app/terminal.py`):** Because OAuth refresh tokens are single-use, before switching seats or launching a new sign-in that will change the stock credential mirror, the currently-active seat is always synced back (`switch.sync_back` / `app.terminal.prepare_then_login`) so any rotated refresh token is captured first.
- **Isolated sign-in (`acctsw/authflows.py`):** `acctsw signin <tool> [--seat EMAIL] [--name NAME]` (and the popover's **add a seat** / **sign in again** actions) runs `codex login` or `claude auth login` inside a temporary directory (`CODEX_HOME` or `CLAUDE_CONFIG_DIR`), reads the resulting credential, identifies the owner (`id_token` JWT claims for Codex; `GET /api/oauth/profile` for Claude), saves the seat snapshot, switches the floor to that seat, and deletes the temporary home and temporary Keychain item. Existing seats are never logged out during sign-in.
- **Safe logout (`authflows.safe_logout`):** Running `codex logout` or `claude auth logout` through the shell wrapper checks whether the stock login belongs to a registered seat. If so, it removes only the local stock credential and sets `stock_logged_out = True` in `state.json` without calling the provider's token-revocation endpoint. Passing `--revoke` runs the upstream revoking logout.
- **Parked Claude seat warm-up (`claudehome.keep_warm`):** When `keep_warm` is enabled (default), background polling checks eligible parked Claude seats whose OAuth token expires within 30 minutes (hardcoded in `acctsw/claudehome.py`) and runs `claude mcp list` inside that seat's `clh/` home so Claude Code can refresh the token without making a model call.

---

## 4. Usage Polling & Limit Detection

### Official usage endpoints (`acctsw/usage.py`)

- **Codex:** `GET https://chatgpt.com/backend-api/wham/usage` with the seat's bearer token and `ChatGPT-Account-Id`. Reads `rate_limit.primary_window` (5h) and `secondary_window` (weekly) `used_percent`, `limit_reached`, and `reset_at`. For eligible seats, polling can refresh an expired access-token JWT via `POST https://auth.openai.com/oauth/token` when a refresh token exists, the seat is not the stock `~/.codex` login, and no supervised session is running on it. Recovery writes only the seat's private snapshot, not the shared `~/.codex/auth.json` mirror; neither a `401` nor the age of `last_refresh` is the trigger.
- **Claude:** `GET https://api.anthropic.com/api/oauth/usage` with `anthropic-beta: oauth-2025-04-20`. Reads `five_hour` and `seven_day` `utilization` and `resets_at`.
- **Polling cadence (`app/menubar.py`, `acctsw/paths.py`):** While the popover is open, the active seat refreshes every 30s (`USAGE_POLL_OPEN_SECONDS`); when closed, background polling runs every 180s (`USAGE_POLL_HIDDEN_SECONDS`, with a 150s floor for parked seats). Readings store `used_pct` internally and render in the UI as **% left** (`100 - used_pct`).

### Live session limit signals (`acctsw/rollout.py`, `acctsw/launcher.py`)

1. **Codex rollout watcher (`RolloutWatcher`):** Finds a rollout under `~/.codex/sessions/` by comparing file mtimes before and after spawning and matching `session_meta.cwd`. Ambiguous matches are tracked as such; a single changed file with a different cwd is a provisional fallback. It classifies `token_count.rate_limits.primary` / `secondary` windows whose `used_percent >= LIMIT_PCT` (`100.0`), a non-empty `rate_limit_reached_type`, `spend_control_reached`, and `task_complete.error.codex_error_info` equal to `usage_limit_exceeded` or `rate_limit_exceeded`.
2. **Claude per-session proxy:** Emits `Hit` records for HTTP `401` (auth) and `429` with `anthropic-ratelimit-unified-status: rejected` (usage limit) in `acctsw/seatproxy.py`. Other `429` responses are treated as transient. `~/.claude/projects/*/*.jsonl` is read for resume session IDs, not limit detection.
3. **Shared state poll (`_tick_state`):** Every ~2s, the supervisor checks for state changes, including a current seat rested with `limited_until` and `limit_source` of `usage` / `hard`, or a user-initiated `manual_switch`.

---

## 5. Switching, Session Continuity & Guards

### Claude Code: in-place proxy switching (`acctsw/seatproxy.py`, `acctsw/sessionca.py`, `acctsw/seatswap.py`)

When `in_place_switch` is enabled (default), `cl` starts a loopback HTTPS proxy (`127.0.0.1:0`) gated by a per-session random Basic `Proxy-Authorization` secret (`acctsw:<secret>`) and an ephemeral EC P-256 certificate authority (`SessionCA`) constrained by `basicConstraints=critical,CA:TRUE,pathlen:0` and `nameConstraints=critical,permitted;DNS.0=api.anthropic.com`. The CA certificate is trusted only by the child process via environment variables (`NODE_EXTRA_CA_CERTS`, `SSL_CERT_FILE`, etc.) in a `0700` temp directory, and private key files (`ca.key`, `leaf.key`) are unlinked immediately after loading into the in-memory `ssl.SSLContext`.

For `POST /v1/messages` and `POST /v1/messages/count_tokens` carrying Claude Code OAuth bearer headers, `SeatProxy` injects the currently bound seat's OAuth access token; all other hosts use a blind byte tunnel. For an eligible, approved limit or manual switch, the supervisor can swap the bound seat in memory (`seatswap.py`) so the live Claude Code session moves without restarting. If in-place switching cannot serve an approved limit or crossing hop, it can fall back to terminating the child and resuming with `claude --resume <session-uuid>` when the session can be identified. (If `in_place_switch` is turned off in settings, a manual switch leaves a running Claude session on its current seat and applies the new choice to the next session.)

### Codex CLI & `codex exec`: restart + resume (`acctsw/launcher.py`, `acctsw/codexexec.py`)

- **Interactive `cx`:** Runs in a supervised PTY with `CODEX_HOME` pointed at the seat's `ch/<hash>` directory. For an approved rate-limit/auth hop or manual switch (`acctsw switch codex <email>` or clicking another seat row in the popover), `_terminate` sends `SIGTERM`, then `SIGKILL` if needed, to the child's process group, including the app-server daemon it started, while draining shutdown output. It allows about five seconds for session saving before escalation. Manual switches capture the rollout thread ID only when the attachment is unambiguous; respawns then use `codex resume <thread_id>`, including later automatic hops in that supervisor. Before an ID is captured, automatic subscription hops use `codex resume --last`. Engine restarts and key handoffs also use a captured ID when their guards permit.
- **Unattended `codex exec` (`acctsw/codexexec.py`):** Runs over pipes (`pipe_spawn`, no PTY), claims its rollout `session_meta.id` via `session.claim_thread`, and on limit restarts with `codex exec resume <thread_id>`.
- **Codex desktop app (`Codex.app`):** Reads `~/.codex/auth.json`. When the menubar poll detects a confirmed limit on the active Codex seat, it swaps `~/.codex/auth.json` to the next healthy seat; if `restart_app` is enabled, it gracefully quits and relaunches `Codex.app`.

### Seat selection & crossing guards (`acctsw/selection.py`, `acctsw/seatkind.py`, `acctsw/seattier.py`)

- **Selection (`selection.choose`):** Selection filters caller exclusions, unapproved crossings, seats with `auth_error`, usage `forbidden`, or `signin.signed_out`; it keeps the active seat if available, otherwise takes the first available seat in store order. If all eligible seats are resting, it returns the one with the earliest `limited_until` and reports that unlock time. There is no stored selection preference.
- **Business ↔ Personal guard (`seatkind.py`):** Crossing between a business plan (`business`, `team`, `enterprise`, `edu`, `education`) and a personal plan (`plus`, `pro`, `max`, `free`), or moving when either kind is unknown, requires approval and can raise a `pending_crossings` prompt in the popover unless `crossing_auto` is enabled or that direction is already approved. Returning to a session's home seat is exempt. A manual click does not bypass this session guard.
- **Cross-tier in-place guard (`seattier.py`):** Moving a running Claude session in place across known different plan tiers (e.g. Max ↔ Pro/Team) prompts in the popover (`pending_tier_requests`) or falls back to restart unless `in_place_cross_tier` is enabled or that tier pair is already approved.

### Idle engine self-restart (`acctsw/enginerestart.py`)

The supervisor checks the store's newer writer stamp (`enginerestart.newer_writer`) and confirms that `~/.local/bin/acctsw` resolves to a newer engine (`enginerestart.target_engine`). For a session that can safely restart, it waits for 65 minutes without user input, child output, or Claude proxy activity (`RESTART_QUIET_S = 3900.0`), with no unsent draft (`InputTracker`) or descendant tool processes (`has_late_descendants`). Once the app is running and the remaining resume guards pass, `acctsw/cli.py` uses `os.execvpe` to become `~/.local/bin/acctsw run <tool> …` with the captured resume arguments. Controlled by setting `engine_restart` (default `True`).

---

## 6. BYOK Key Seats (`acctsw/keyseats.py`, `acctsw/providers.py`, `acctsw/pricing.py`, `acctsw/handoff.py`)

- **Providers & harnesses:** Supports `openai`, `anthropic`, `openrouter`, `langdock` (`eu`/`us`/`global` on both OpenAI Responses and Anthropic Messages routes), and custom endpoints. OpenAI/Responses-route keys drive `codex`; Anthropic-route keys drive `claude`.
- **Catalog & pricing (`pricing.py`, `keyprove.py`):** Fetches model catalogs from the provider (`fetch_catalog`), sanitizes text with `redact_key` before caching to `pricing.json`, and offers an explicit paid Responses compatibility check (`keyprove.prove`). Proof does not gate fallback: `selection.choose_key` checks `last_validation.error` for `invalid_key` / `insufficient_quota` alongside the fallback, harness, subscription, and pin gates. Adding an endpoint whose Responses support is unverified requires `acctsw keys add --allow-unverified`.
- **Consent & modes:**
  - **Automatic fallback (`key_fallback`):** Off by default. When enabled and all eligible subscription seats for a harness are resting (or no subscriptions are saved), a supervised session can offer a configured key seat (`pending_key_switches` when `confirm_key_switch` is `True`, showing model and price estimate in the popover). `handoff.py` holds the paid-use consent records.
  - **Dedicated terminal pin (`--key <id|label>`):** `cx --key <seat>` / `cl --key <seat>` (or **use in new terminal** in the popover) runs a single terminal on a dedicated isolated home (`ch/<hash>`, with the pin included in the hashed identity) without changing the active subscription floor. After a pinned child flushes, `keyhome.publish_resume` copies only conversation rollouts/transcripts into the shared tool home so work can continue on a subscription seat with `codex resume <id>` or `claude --resume <id>`.

---

## 7. Menubar App & Web UI (`app/menubar.py`, `app/web/render.mjs`, `app/web/app.mjs`)

- **Native shell (`app/menubar.py`):** `pyobjc` `NSStatusItem` with an SF Symbol door icon (`door.left.hand.open` / `door.left.hand.closed`, with emoji fallback) and an `NSPopover` (`376px` wide, dynamically sized between `220px` and `600px` via the web view's `resize` action). Hosts a locked-down `WKWebView` loading local `app/web/index.html`, `styles.css`, and `bundle.js` (built from `render.mjs` + `app.mjs` via `scripts/build-web.py`), communicating over `window.webkit.messageHandlers.agl`.
- **Now-next UI (`now-next` in `app/web/render.mjs`):**
  - **Header:** Brand title, status chip (`all good`, `all limited`, `renew soon`, `N needs you`), `+` add-seat menu, and `...` (`more`) menu (auto-switch toggle, check for updates, settings, quit).
  - **Warm field (`.field`):** Follows the active banners (in-app update banner, engine/supervision warnings, business/personal crossing approvals, tier approvals, paid-key confirmations, active paid sessions) and contains one `.tool` answer block per configured tool showing the active seat (`on now`), its tighter quota window `% left` bar, reset countdown, secondary window `%`, and the `then` next-seat line.
  - **Solid sheet (`.sheet`):** Compact rows for all other subscription seats and BYOK key seats. Clicking a row switches to that seat; toggling `details` reveals inline rename, both quota window bars, sign-in expiry status, re-auth/sign-out/remove actions, and key compatibility proof actions.
  - **Settings view:** Grouped into **switching**, **paid keys**, and **appearance** (theme picker + menu-bar door icon legend + version/build footer).

---

## 8. CLI Reference (`acctsw/cli.py`)

```text
acctsw install                         # set up store, capture factory backups, import current stock logins
acctsw path                            # put ~/.local/bin first on PATH; alias codex=cx, claude=cl
                                      # rc selection follows §1: zsh, bash profile/bashrc, or profile
acctsw uninstall [--purge]             # restore factory credentials and remove shell integration
acctsw signin <codex|claude> [--seat EMAIL] [--name NAME]  # official browser login in an isolated temp home
acctsw add <codex|claude>              # snapshot the current stock credential as a seat
acctsw remove <codex|claude> <email>   # delete a seat snapshot and its per-seat home
acctsw rename <codex|claude> <email> <name>  # set or clear ("") a seat's display name
acctsw list [--json]                   # seat markers, names, and emails; optional JSON output
acctsw status [--json]                 # full engine status
acctsw usage refresh [--tool codex|claude] [--json]  # refresh usage for all seats or one tool
acctsw switch <codex|claude> <email>   # select a seat; live moves are subject to the guards above
acctsw run <codex|claude> [--key <id|label>] [--] [args...]  # supervised session (backing cx / cl)
acctsw keys {add,list,remove,prove,models} ...               # manage BYOK API-key seats
acctsw daemons [--fix] [--json]        # inspect or clean up orphaned Codex app-server daemons
```

---

## 9. In-App Updater (`acctsw/updater.py`)

The menubar app checks `https://github.com/fheinfling/ai-guest-list/releases/latest/download/latest.json` automatically (requests throttled to once per 20 hours) and on demand via **… → check for updates**. When a newer version is available and the user clicks **update**:

1. Using the checked manifest, the release zip is downloaded over HTTPS from allowlisted GitHub hosts (`github.com`, `release-assets.githubusercontent.com`, `objects.githubusercontent.com`) into `~/.account-switcher/updates/` (`0700`). The manifest is stored with updater status in `~/.account-switcher/update.json`.
2. The zip's byte size and SHA-256 digest are verified against `latest.json`. Before extraction, member names are checked for traversal, absolute paths, and unexpected top-level entries, with entry-count and unpacked-size caps. After `ditto` extraction, `_check_layout` rejects links escaping the app and special files; symlinks contained within the app are allowed.
3. The extracted `AI Guest List.app` is verified with `/usr/bin/codesign --verify --deep --strict -R` against the bundled designated requirement (`Contents/Resources/update-requirement.txt`), assessed with `/usr/sbin/spctl --assess --type execute`, and checked via `Info.plist`.
4. A detached helper copies the staged app to `.<App>.app.new-<pid>` beside the running bundle and re-verifies it with `codesign`. It moves the current bundle to `.<App>.previous-<pid>.app`, replaces the bundle at its existing location, and launches it. That location must be writable and outside App Translocation. The helper rolls back if the new app fails to launch or `~/.account-switcher/update-ack` does not contain the new version within the acknowledgement wait.
