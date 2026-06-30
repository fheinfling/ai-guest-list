# End-to-end verification — ai guest list

Maps the plan's verification section to concrete steps. **Safe** steps touch nothing destructive;
**live** steps require your second account and mutate the canonical credential locations (always
reversible via `acctsw uninstall`).

## Automated (safe)
```sh
bash scripts/smoke.sh          # 136 python + 11 node UI tests
acctsw install --dry-run       # prints every action, changes nothing
```

## Read-only live probes (safe)
- Identity: engine reads the live Codex email from the auth.json JWT and the live Claude email
  from `claude auth status` — verified.
- Usage endpoints: Codex `wham/usage` (`rate_limit.{primary,secondary}_window`) and Claude
  `oauth/usage` (`five_hour`/`seven_day`) parse correctly against the real APIs — verified.

## Install (non-destructive, reversible)
```sh
acctsw install                 # backs up originals to Keychain + manifest, registers the
                               # currently-logged-in account as seat #1, installs cx/cl/acctsw
acctsw list                    # shows the registered seat
acctsw status --json           # active account + cached usage
```

## Add your second seat
```sh
codex logout && codex login    # sign into account #2 (or use the app's "add a seat")
acctsw add codex               # snapshots account #2
acctsw list                    # both seats listed
```

## Switch + continuity (the headline)
```sh
acctsw switch codex <email#1>  # codex login status / JWT confirms #1
acctsw switch codex <email#2>  # back to #2
cx                             # supervised codex; on a real usage limit it auto-switches and
                               # resumes the same session on the other seat
```
- Continuity dry-run (no real limit): start `cx`, do one turn, Ctrl-C, `acctsw switch codex <other>`,
  then `codex resume --last` → same conversation continues under the other seat.

## Menubar app
```sh
bash scripts/run-app.sh        # 🎟️ appears in the menu bar
```
- Popover shows seats, "on the floor", 5h/weekly bars + reset timers, switch, add-a-seat, toggles.
- Dot glyph reflects fresh / resting / needs-a-hello.
- Toggle "save credit" → routes agents through Headroom (install link if missing).

## Headroom
```sh
acctsw status --json | grep headroom_available
pip install "headroom-ai[all]"   # then the save-credit toggle wraps `headroom wrap <agent>`
```

## Uninstall (full reversal)
```sh
acctsw uninstall               # restores the freshest copy of the ORIGINAL account, removes wrappers
acctsw uninstall --purge       # also deletes the store + all our keychain items (system as before)
```

## Known gaps to confirm live (tracked)
- Real limit-message strings: `launcher.LIMIT_PATTERNS` is conservative; confirm/extend against the
  actual Codex/Claude limit output on a real cap.
- Resume-by-id: currently `codex resume --last` / `claude --continue` (MVP); capture the session id
  at spawn to resume by id if you run multiple concurrent sessions.
- We deliberately do NOT use `headroom install apply/remove/status` — its macOS launchd deploy is
  broken. Global app-managed mode instead runs the proxy ourselves (`headroom proxy`, detached +
  PID-tracked) and hand-writes provider routing. Live
  checks to confirm against a real install:
  - **`headroom proxy` flags + `/readyz`**: confirm `headroom proxy --host 127.0.0.1 --port 8787
    --mode token --backend anthropic --no-telemetry` starts and `GET /readyz` returns `ready:true`
    (this is what `start_proxy`/`proxy_ready` rely on).
  - **Routing actually works end-to-end**: with the proxy up and routing written, confirm a plain
    `codex` and a plain `claude` run both reach the proxy (watch `~/.headroom/logs/proxy.log` for
    `proxy_inbound_request`), and that Codex's OpenAI-format requests are handled by the
    anthropic-backed proxy (if not, the codex side may need `--backend` adjusted or codex routing
    dropped).
  - **Injection markers** (`headroom.INJECT_MARKERS`): we now WRITE the routing ourselves, so the
    markers are ours by construction — `model_provider = "headroom"` (Codex `config.toml`) and the
    loopback proxy URL `http://127.0.0.1:8787` (Claude `settings.json` env `ANTHROPIC_BASE_URL`).
    Confirm a real-world user config never legitimately contains the loopback URL (it would be
    treated as our routing).
  - **Headless rtk integrity**: `cx`/`cl` run with the menubar app closed don't re-verify rtk (the
    GUI poll does while it's open, and a closed app means the proxy is down → routing is healed
    away). NB: the new path never runs `headroom wrap`, so `rtk` may never be downloaded at all and
    `verify_rtk` is a no-op ("not present yet") — fine, but confirm savings don't depend on rtk.
  - **Savings seeding / shaper env**: enabling save-credit (a) starts the proxy with
    `HEADROOM_OUTPUT_SHAPER=1` + `HEADROOM_OUTPUT_HOLDOUT` directly in the child's env (we own it now,
    so no plist workaround) and (b) seeds the baseline once via `headroom learn --verbosity --apply
    --all` (background, best-effort). Confirm against a live install that `output-savings` returns a
    real number after some routed traffic. `learn --all` is a slow, LLM-driven analysis of your
    coding history — verify its token/time cost is acceptable.
