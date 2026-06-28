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
- `headroom wrap <agent>` exact CLI form: confirm against the installed Headroom version.
