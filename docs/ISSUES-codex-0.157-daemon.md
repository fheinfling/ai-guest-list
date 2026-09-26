# Codex 0.157 daemon failures and per-seat package growth

**Status:** supervisor shutdown deadlock fixed in this worktree; healing, orphan reaping, release
GC and upgrade rescue implemented. Real-seat recovery with the new build remains for the reviewer
to verify. Package sharing is deliberately not shipped. Updated 2026-09-26.
**Affects:** shipped app (`acctsw 1.0.2`), independent of the `byok-key-seats` branch.

Codex 0.157 uses a managed app-server daemon per `CODEX_HOME`. Every seat has its own home under
`~/.account-switcher/ch/<hash>`, daemon runtime, control socket and packages. The observed failure
was caused by **our supervisor deadlocking during PTY shutdown**. Package growth is a separate issue.

## Resolution and root-cause findings

The reviewer established the following outside the execution sandbox. These are supplied,
verified observations; this work did not inspect or modify the real seats or signal those processes.

1. Killing a managed daemon with SIGKILL is recoverable in stock Codex 0.157.0. `daemon version`
   initially fails with ECONNREFUSED, then `codex app-server daemon start` and a TUI started in a PTY
   both launch a fresh daemon. The same holds after flipping `current` to 0.157.1. A dead PID and
   refusing socket alone do not explain the incident.
2. The affected `daemon.pid` names **47703**, which is **still alive but exiting** (`?E`, P_WEXIT),
   with no open file descriptors. Its parent TUI **47696** is also exiting (`?Es`); its parent is
   our supervisor **50896**, `python -P -m acctsw run codex`, orphaned to PID 1 with no tty after
   its terminal tab closed. The daemon is a child of the TUI in its process group, not detached.
   Its node_repl, codex-code-mode-host and node children are also exiting. Codex sees a live PID
   with matching identity and therefore keeps trusting it while the socket refuses connections.
3. A sample of supervisor 50896 places its main thread inside a Python signal handler, blocked
   in `__wait4`: `launcher._on_term` → `_terminate(pid)` → the final `os.waitpid(pid, 0)`.
   The supervisor still holds the PTY master open but has stopped reading it. On macOS, an exiting
   process can wait for tty output to drain; the child cannot finish, and the supervisor cannot
   finish waiting. Five orphaned `acctsw run claude` supervisors show the same shape, including
   exiting children and zombie grandchildren.
4. The minimal reproduction is `pty.fork()` running `sh -c "yes aaaa…"`, no reads from the master,
   then `_terminate(pid)` in a thread. The old code remains hung after 15 seconds with an exiting
   child. Closing the master immediately releases it.

The earlier account that PID 47703 was dead was incorrect. Daemon death, a `/tmp` reap and the
updater were not the cause of this incident. Socket-only cleanup cannot make Codex stop trusting
an exiting but still-present matching PID.

### What ships

- `_terminate(pid, master_fd=None)` drains and discards PTY output throughout SIGTERM's five-second
  session-save grace and SIGKILL's one-second grace. It then closes the master and polls for at most
  one more second. There is no blocking waitpid in termination; an unreaped child is logged and
  left for init when the supervisor exits. The no-PTY caller remains supported and cannot signal
  its caller's shared process group. Both tab-close signals and auto-switch use this shutdown.
  A shared guard prevents nested SIGHUP/SIGTERM from entering termination a second time.
- Healing probes the resolved control socket with a 0.2-second timeout and holds the startup flock
  during the final recheck. Missing/refusing sockets with dead, reused, exiting or zombie recorded
  PIDs can heal. Matching running PIDs, uncertain identity/state without other proof, held locks
  and unexpected socket errors defer. Repair archives the record as `daemon.pid.stale-<ts>`,
  removes the socket alias and startup lock, and removes only the corresponding dead uid-owned
  temp socket. An absent socket with a stale PID record heals too. Updater/auth/package files stay.
- Darwin lifecycle comes from `sysctl(KERN_PROC_PID)` → `kinfo_proc`: `p_stat == SZOMB` (5) or
  `p_flag & P_WEXIT` (0x2000). The LP64 layout and offsets were checked with a compiled SDK probe
  (`sizeof(kinfo_proc) == 648`); proc_bsdinfo flags are not interchangeable with p_flag.
  Linux reads `/proc/<pid>/stat` states Z/X. Identity remains kernel epoch seconds/microseconds.
- Upgrade maintenance finds wedged supervisors using kernel UID, PPID, tty, argv and child records.
  Only same-uid Python `-m acctsw run …` processes with PPID 1, no controlling tty and exclusively
  exiting/zombie direct children qualify. Two samples at least one second apart must retain the
  same supervisor and at least one same exiting child; all predicates are rechecked before SIGKILL.
  Live/unknown children, foreign children, tty ownership, PID reuse or unavailable topology veto
  rescue. Missing executable paths do not veto rescue; unknown argv excludes only that candidate.
  Closing the killed supervisor's master releases the old tree for launchd to reap.
- `acctsw daemons [--json]` reports seats, package bytes, orphan PIDs and **wedged supervisors**.
  `--fix` also reports healing, reaping, GC and supervisor signals sent (not claimed exits).
  Menubar startup invokes the same pass on its existing background recovery thread. Rescue runs
  independently of a stale active-session marker; live sessions still defer orphan reaping and GC.
- Prior orphan reaping and GC remain: only same-uid legacy/unregistered home executables qualify,
  fresh identity/seat membership is checked before signals, and GC retains current plus executing
  releases under the install lock. Packages remain private to each seat and are never promoted.
  Executable lookup failures (including ENOENT after an upgrade) and argv failures are recorded
  independently as partial inspection. Either executable or argv[0] pins a release; a live same-uid
  record lacking both defers GC with an explicit reason. Darwin has no pidfd; start-time checks
  immediately before signals mitigate, but cannot eliminate, the PID-reuse TOCTOU window.

### Verification and remaining live checks

Regression tests cover the real unread-PTY reproduction with a timed thread join, no-PTY callers,
chatty SIGTERM session saving, bounded post-KILL waits and handler reentrancy. Injected lifecycle
records cover healing and conservative supervisor rescue, including PID reuse and live-child/tty
vetoes. Tests signal only children they spawn; rescue tests inject every signal.

The Round 1 stock-daemon experiments in throwaway homes were blocked by sandbox EPERM during
Codex's `ps` invocation. They did not establish respawn behavior. The reviewer's subsequent
unsandboxed experiments establish ordinary SIGKILL/current-flip respawn as described above.
The new work verifies the kernel ABI and shutdown behavior locally; recovery of the real stuck
seat and old supervisors is deliberately left to the reviewer. This round's sandbox also denies
AF_UNIX bind; real-socket tests skip specifically on that permission error, while injected heal
and real PTY tests still run.

The fresh-install lock was previously verified as flock-compatible. Codex's startup lock type,
two-home authentication isolation and shared-package update safety remain unverified. Package
sharing is not enabled. See [VERIFY.md](VERIFY.md#codex-0157-daemon-maintenance) for the live checks.

## Issue 1 — an exiting daemon's PID blocks a seat

The observed `app server did not become ready` error ends in `Connection refused (os error 61)`
for `app-server-control/app-server-control.sock`, an alias into `/private/tmp/codex-daemon-<uid>`.
This means no process listens on that socket. It does **not** establish that the recorded PID is
absent. Here the daemon was stuck exiting under our deadlocked supervisor.

The repair addresses both ends: keep reading during future supervisor shutdowns, rescue proven
wedges from older builds, and archive stale daemon PID records as well as cleaning dead sockets.
Use the guarded `acctsw daemons --fix` maintenance path; socket/lock removal alone is insufficient
for this incident. The reviewer will verify stock respawn on the recovered real seat.

---

## Issue 2 — 628 MB of daemon per seat, growing linearly

```
~/.codex/packages                          none          ← shared home has no daemon
~/.account-switcher/ch/be30a5f0/packages   628M
~/.account-switcher/ch/be8d0b1d/packages   628M
~/.account-switcher/ch                     1.2G total    ← for two seats
```

Every seat carries a full copy, and each copy **auto-updates independently**: both homes are on
daemon **0.157.1** while the installed CLI is **0.157.0**.

Per-seat `CODEX_HOME` was close to free when it held an `auth.json` and a symlinked state dir. Under
0.157.x it costs ~628 MB and one background process per seat, plus an independent update channel per
seat. A user with four seats is carrying ~2.5 GB and four daemons for what is conceptually four
credential blobs.

**Options considered (this change chooses option 3; option 1 remains unproven):**

1. **Share the daemon across seats.** Point `packages/` (or whatever `CODEX_HOME` subpath the daemon
   installs into) at one shared location via symlink, the way the seat homes already symlink shared
   state back to the real `~/.codex`. Cheapest if the daemon tolerates it — **unverified whether it
   does**, and a shared daemon may itself be per-account-scoped in ways that break switching.
2. **Stop giving each seat a `CODEX_HOME`.** The reason seats have private homes is that Codex
   rotates refresh tokens per account and the home is the source of truth; undoing that reopens the
   token-clobbering class of bug the current design exists to prevent. Expensive and risky.
3. **Garbage-collect old daemon releases.** `packages/app-server-daemon/releases/` accumulates
   versions; keeping only `current` would cut the growth even if not the baseline.
4. **Accept it and document it**, with a disk-usage note in the app and a cleanup command.

Option 1 then 3 looks like the pragmatic pair, but option 1 needs a real experiment first: symlink
one seat's `packages/` at another's and confirm the daemon starts, switches accounts, and does not
cross-contaminate credentials.

---

## Ruling that out

This is **not** the `byok-key-seats` branch. Three independent checks:

1. The engine terminals actually run is the installed app's, and it does not contain the branch:

   ```
   installed acctsw version: 1.0.2
     has keyhome:  False
     has keyseats: False
     has keyprove: False
     has providers: False
   ```

   (`~/.local/bin/acctsw` execs `/Applications/AI Guest List.app`, whose engine is bundled in
   `python311.zip` — editing the working tree does not affect it.)
2. The failing seat home `be8d0b1d` dates from **25 Sep 17:46**; `acctsw/keyhome.py` did not exist
   until **26 Sep**.
3. The reviewed process sample identifies the shipped supervisor's PTY shutdown path as the
   cause. This path is shared by Codex and Claude and predates the BYOK branch.

## Where it does touch the branch

`acctsw/keyhome.py` gives every **key seat** its own `CODEX_HOME` as well, so each key seat would add
another ~628 MB and another daemon on top of the subscription seats. Whatever is decided in Issue 2
should be decided for key seats at the same time — possibly differently, since a key seat has no
OAuth token to protect and therefore a weaker reason to need a private home at all. That is the one
design question the BYOK work should not settle on its own.

## Original incident verification (before this change)

**Verified on this machine (2026-09-26):** the error text; socket present with nothing listening
(`lsof` empty); socket is a symlink into `/private/tmp/codex-daemon-501/`; 628 MB per home across two
homes, 1.2 GB total; `~/.codex` has no `packages/`; daemon 0.157.1 vs CLI 0.157.0; the seat is
`franz.heinfling+codex@franzh.com`; the installed engine lacks the branch modules.

**Updated by Round 2 review:** the supervisor deadlock and stock recovery after ordinary daemon
SIGKILL/current-flip are verified outside the sandbox. The remaining checks are recovery of the
actual wedged seat with this build, startup lock behavior, and package-sharing isolation/update
safety. No real user home or pre-existing process was changed during this work.
