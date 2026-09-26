# End-to-end verification — ai guest list

Maps the plan's verification section to concrete steps. **Safe** steps touch nothing destructive;
**live** steps require your second account and mutate the canonical credential locations (always
reversible via `acctsw uninstall`).

## Automated (safe)
```sh
bash scripts/smoke.sh          # full python suite + node UI tests (PY=python3 if .venv is stale)
acctsw install --dry-run       # prints every action, changes nothing
```

## Read-only live probes (safe)
- Identity: engine reads the live Codex email from the auth.json JWT and the live Claude email
  from `claude auth status` — verified.
- Usage endpoints: Codex `wham/usage` (`rate_limit.{primary,secondary}_window`) and Claude
  `oauth/usage` (`five_hour`/`seven_day`) parse correctly against the real APIs — verified. The Codex
  reader also keeps the endpoint's *authoritative* flags, which is what auto-switch trusts:
```sh
acctsw usage refresh --tool codex --json \
  | grep -E '"(plan_type|allowed|reached_type|spend_control_reached)"'
```
  On a depleted workspace expect `allowed: false` and a non-empty `reached_type` (e.g.
  `workspace_member_credits_depleted`) *with null windows* — percentages alone are blind there.
  (Touches no credentials; only the usage cache is refreshed.)

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

### A — a manual switch never kills a live session
```sh
mkdir -p ~/gl-check && cd ~/gl-check && cx   # terminal 1: supervised codex, do one turn, leave it running
acctsw switch codex <other-seat-email>       # terminal 2, while it is still running
```
Terminal 1 keeps working on its current seat. Exactly one notification — *"still running on `<seat>`;
your new seat applies to the next session"* — and the new seat takes effect on the next `cx`.

### B — a seat rested by another process makes the running session hop
The field bug: the menubar's usage poll rests seat A while a supervised session is on it. Rest it the
way the engine itself does, from a second terminal while `cx` runs on seat A:
```sh
python3 - <<'PY'
from datetime import timedelta
from acctsw.context import Context
from acctsw.util import iso, now
ctx = Context.default()
with ctx.locked():                      # same cross-process lock every engine writer takes
    st = ctx.load_state()
    st.set_limited_until("codex", "<seat-A-email>", iso(now() + timedelta(hours=1)), source="usage")
    st.save()
PY
```
Within ~2 s the running session hops to seat B, relaunches with `codex resume --last`, and notifies.
Then hand the seat back (same block, `None` instead of a timestamp — and no `source`):
```sh
python3 - <<'PY'
from acctsw.context import Context
ctx = Context.default()
with ctx.locked():
    st = ctx.load_state()
    st.set_limited_until("codex", "<seat-A-email>", None)
    st.save()
PY
```
Only `source="usage"` (or `"hard"`) drives a hop; the launcher's own weak `"reactive"` guess never
evicts a live session.

### C — the structured signal, replayed offline (no real limit needed)
Feed a real rollout file through the classifier — this is exactly what the launcher reads:
```sh
python3 -c 'import json,sys; from acctsw import rollout; print(*[s for s in (rollout.classify(json.loads(l)) for l in open(sys.argv[1]) if l.startswith("{")) if s], sep="\n")' <path to one ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl>
```
A depleted session prints hard limits, e.g.
`RolloutSignal(kind='limit', hard=True, reset_at=None, detail='workspace_member_credits_depleted', ...)`
and a `task_complete` line carrying `usage_limit_exceeded`. A healthy session prints
`kind='healthy'` lines or nothing at all — in particular `credits.has_credits: false` must **never**
produce a signal, since healthy accounts report it.

## Menubar app
```sh
bash scripts/run-app.sh        # builds and opens a development .app with the real app icon
```
- Popover shows seats, "on the floor", 5h/weekly bars + reset timers, switch, add-a-seat, toggles.
- Dot glyph reflects fresh / resting / needs-a-hello.
- Quit any previously running copy first. The development bundle uses the existing icon and app
  identity; running `python -m app.menubar` directly identifies notifications as Python instead.

## Legacy "save credit" (Headroom) removal — migration check
The Headroom compression proxy was removed (measured as not worth it; see
`docs/SECURITY-headroom.md`). On a machine that used it, confirm the one-time cleanup:
```sh
# Simulate an older build's leftover routing, then trigger cleanup on the next cx/cl run:
grep -q headroom ~/.codex/config.toml && echo "routing present"
cx --help >/dev/null 2>&1 || true          # any cx/cl run triggers cleanup_legacy when the app is closed
grep -q headroom ~/.codex/config.toml && echo "STILL routed (bug)" || echo "routing cleaned ✓"
```
- `headroom.legacy_present(ctx)` should read False afterwards; `~/.account-switcher/hr-venv`,
  `headroom-proxy.pid`, and the backup dir should be gone; the user's original `config.toml` /
  `settings.json` restored (exact bytes when a snapshot backup existed).

## Uninstall (full reversal)
```sh
acctsw uninstall               # restores the freshest copy of the ORIGINAL account, removes wrappers
acctsw uninstall --purge       # also deletes the store + all our keychain items (system as before)
```

## Known gaps to confirm live (tracked)
- Real limit-message strings: `launcher.LIMIT_PATTERNS` is now a **fallback only** — a banner match
  merely triggers a usage check, a fresh structured "healthy" reading dismisses it, and the hard
  banner acts alone only when no rollout file is attached. (Codex CLI 0.153.4 reworded the
  out-of-credits banner and the old trusted match stopped firing; that is why the banners were
  demoted.) Claude has no structured source yet, so its patterns still carry weight — confirm/extend
  them against the actual Claude limit output on a real cap.
- Codex SQLite error 14 ("unable to open database file") from a mixed database/sidecar family is
  now healed automatically on the next `cx` launch with no other supervised codex session running:
  `ls -la ~/.account-switcher/codex-homes/<seat>/` (the by-address symlink into `ch/<id>`) must show
  `auth.json` as the ONLY real file and no `*-wal` / `*-shm` links — apart from the app-server
  daemon's own `app-server-control/` and `app-server-daemon/`, which are real and per-seat by design.
  Stock codex legitimately owns copies of those in `~/.codex`; what must never happen is a seat
  *sharing* them — they are never symlinked into a home and never promoted out of one, because a
  shared daemon serves the wrong account's auth. A `*.orphaned-<stamp>` copy parked there is
  expected and inert.
- Codex daemon socket: a seat home must stay at most 60 bytes **as resolved** so
  `<home>/app-server-control/app-server-control.sock` fits macOS's 104-byte `SUN_LEN`; past it every
  interactive `cx` launch dies with "app server did not become ready … path must be shorter than
  SUN_LEN" (`--no-daemon` still runs). Check with
  `python3 -c "from acctsw import codexhome as c; print(c.daemon_socket_fits(c.home_dir('<seat>')))"`.
- Resume-by-id: currently `codex resume --last` / `claude --continue` (MVP); capture the session id
  at spawn to resume by id if you run multiple concurrent sessions.
- The Headroom "save credit" proxy is gone; only the one-time `cleanup_legacy` migration remains
  (see the migration check above). Confirm on a machine that had it enabled that a plain `codex` and
  `claude` run directly (no `model_provider = "headroom"` / loopback `ANTHROPIC_BASE_URL` left in
  their configs) after the next app launch or `cx`/`cl` run.


## Codex 0.157 daemon maintenance

1. Run `python3 -m pytest -q`. PTY tests reproduce a full unread output queue and require shutdown
   to return within three seconds for a cooperative child. They also exercise a chatty SIGTERM
   handler saving its session, no-PTY shutdown, nested signals and bounded waits after SIGKILL.
   Lifecycle/heal/rescue tests inject kernel records and rescue signals. Real AF_UNIX tests skip
   only if the sandbox denies bind; all homes are temporary and signalled children are test-owned.
2. Run `acctsw daemons --json` for read-only inspection. Check each seat's home, socket state,
   `packages_bytes`, `orphan_pids`, `wedged_supervisors` and `process_inspection`. Detecting a
   candidate wedge takes at least one second for the second sample. `unavailable` means process
   enumeration or kernel topology failed. `partial (N records incomplete)` means executable/argv
   details are missing; deleted or replaced executables are normal on a long-running Mac. Readable
   argv still permits rescue/reaping. The live regression copies `/bin/sleep` into tmp, signs the
   copy on Darwin, deletes it after launch, and verifies its record before killing only that child.
3. On the reviewer machine, run `acctsw daemons --fix --json` after inspecting the candidates.
   `signalled_supervisors` should contain only same-uid Python `-m acctsw run …` supervisors with
   PPID 1, no tty and stable exiting/zombie children across two samples. A live/unknown child or
   tty vetoes rescue. Confirm those old supervisor trees actually disappear; the report records
   signals sent, not verified deaths. This rescue also runs at app startup, even when a wedged
   supervisor leaves a stale active-session marker. Healthy sessions must continue uninterrupted.
4. For the formerly stuck seat, expect `heal: healed` and an archived
   `app-server-daemon/daemon.pid.stale-<ts>` with the original bytes. The dead socket/alias and
   startup lock are removed; updater/auth/package files are retained. Launch stock Codex and verify
   a new PID, successful `daemon version` and a working TUI. Matching running PIDs must remain busy
   if their socket refuses, and successful listening sockets must remain untouched.
5. Verify future shutdown on throwaway sessions: close a supervised terminal tab while the child
   emits heavy output, and trigger a supervised auto-switch whose TUI prints during SIGTERM.
   Session saving gets about five seconds with the master still open and actively drained; after
   KILL's one-second grace the master closes, followed by at most one second of nonblocking reap.
   No supervisor should remain waiting indefinitely in wait4. Do not use real credentials for the
   automated repro and signal only processes spawned for the experiment.
6. With a healthy supervised Codex session live, `gc` and `reaper` must remain `session-live`.
   Otherwise GC retains current and all executing releases. `packages` remains a real per-seat
   directory after launch with promotion enabled. Either executable or argv[0] pins a release.
   A live same-uid record lacking both defers GC with `process-details-unavailable` and a reason;
   rescue/reaping still use records with readable argv. Unavailable topology defers all three.

The reviewer already verified that ordinary SIGKILL of a managed daemon respawns with stock 0.157.0,
including after a `current` flip to 0.157.1. That is no longer an open root-cause question. Remaining
integration checks are actual wedged-seat recovery, Codex's startup-lock compatibility, and two-home
authentication isolation/update safety before considering package sharing. See
[root-cause findings](ISSUES-codex-0.157-daemon.md#resolution-and-root-cause-findings).
