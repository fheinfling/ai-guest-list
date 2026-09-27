# Key-seat credential containment — 2026-09-27

The Codex leak is reproduced and mitigated in `acctsw/keyhome.py`. Shell environment
exclusions alone **do not protect snapshots in Codex 0.157.0**. Key seats now also disable
`features.shell_snapshot`. Claude key seats enable `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`.

## Mechanism and evidence

The [official Codex policy reference](https://developers.openai.com/codex/config-advanced/#shell-environment-policy)
and the pinned [0.157.0 environment implementation](https://github.com/openai/codex/blob/rust-v0.157.0/codex-rs/protocol/src/shell_environment.rs)
establish this order:

1. `inherit`: `all` starts with the process environment; `none` starts empty; `core`
   retains the platform's basic variables. On Unix these are PATH, SHELL, TMPDIR, TEMP,
   TMP, HOME, LANG, LC_ALL, LC_CTYPE, LOGNAME and USER.
2. `ignore_default_excludes = false` removes names matching `*KEY*`, `*SECRET*`, `*TOKEN*`.
   The default is **true**, so this filter does not run unless explicitly enabled.
3. `exclude` removes matching variable names, using case-insensitive `*`/`?` patterns.
4. `set` inserts explicit values **after** exclusion; it can reintroduce an excluded key.
5. A nonempty `include_only` retains matching names. It cannot recover removed values.

Codex then adds thread context and removes its reserved non-inheritable variables.
The pinned [policy defaults](https://github.com/openai/codex/blob/rust-v0.157.0/codex-rs/protocol/src/config_types.rs)
confirm `inherit = all`, `ignore_default_excludes = true`, and empty exclusion/set/include lists.
Current docs also describe the newer `filters` table; legacy `exclude` remains supported.
The generated config uses that supported array and does not mix the two syntaxes.

**Experimentally verified:** with explicit policy exclusions but snapshots enabled, a real
0.157.0 thread still wrote the fake key into `shell_snapshots/*.sh`, and its shell command
reported `KEY_PRESENT`. With exclusions plus snapshots disabled, the command reported
`KEY_ABSENT` and both live and post-session scans were empty. Snapshot replay can therefore
defeat the intended command environment exclusion too.

The pinned snapshot source could not be retrieved through the available web tool. Its exact
internal call chain is **not claimed as source-verified**. The snapshot conclusion comes from
the installed binary experiment, not an assumption that the policy covers both paths.
Codex itself still receives the credential through the provider's unchanged `env_key`;
filtering applies to command subprocesses. Successful provider authentication was not tested.

Claude Code 2.1.282's installed executable contains its bundled JavaScript. Inspection of
`QTn`, `YTn`, `XTn`, and `xnt` showed snapshot creation under `CLAUDE_CONFIG_DIR/shell-snapshots`:
it records functions, aliases, shell options, built-in helpers and PATH, rather than dumping
every exported environment variable. Its `Os()` child-environment builder is also used by
snapshot creation. With the subprocess scrub enabled, it removes recognized credentials.
The [official Claude environment reference](https://code.claude.com/docs/en/env-vars)
documents that `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` strips credentials from Bash, hooks and
MCP subprocesses while keeping them in the parent for API calls.

**Experimentally verified:** a Claude interactive `!` command created an actual snapshot.
Before mitigation the command saw the token, but neither the snapshot nor any other scanned
seat file contained the fake value. After enabling the scrub, the command could no longer
see the token; a snapshot still existed and the scans remained empty. No comparable automatic
Claude disk leak was reproduced. The scrub closes its observed shell inheritance exposure.

## Reproduction

Only a recognisable fake secret was used:
`"sk-ACCTSW-FAKE-SECRET-SECURITY-AUDIT-" + "x" * 128`.
Seats use `Context.for_test` with an in-memory Keychain, isolated HOME/rc files, and a minimal
environment. No real login environment, credentials, or account homes are imported.

```sh
PYTHONPATH=. python3 tests/test_keyhome_live.py /private/tmp/aks-final-audit
```

The driver creates real pinned key-seat homes with `prepare(..., pin=...)` and `build_env`.
For Codex it starts the installed `codex app-server`, initializes a thread, and invokes
`thread/shellCommand`. For Claude it starts the real TUI in a PTY and executes a `!` command.
Commands print only presence/absence, never the secret. The controls restore the old Codex
config or enable snapshots while retaining the new policy; Claude's control removes the scrub.

Each home is scanned while the harness is alive and after SIGKILL, simulating an interrupted
terminal. This matters: Codex can unlink its already-leaked snapshot during graceful shutdown,
making a post-exit-only test falsely pass. The interrupted controls retain the evidence.
Scans include hidden files and binary databases/WALs. The standalone driver additionally runs:

```sh
rg --hidden --no-ignore -a -l -F "$FAKE_SECRET" "$SEAT_HOME"
```

Observed results (exit 0 = match; exit 1 = no match):

| Harness/config | Shell probe | Live scan | Post-session grep |
| --- | --- | --- | --- |
| Codex, old config | KEY_PRESENT | snapshot contains fake key | exit 0 |
| Codex, exclusions only | KEY_PRESENT | snapshot contains fake key | exit 0 |
| Codex, patched config | KEY_ABSENT | no matches | exit 1 |
| Claude, original environment | KEY_PRESENT | no matches; snapshot exists | exit 1 |
| Claude, subprocess scrub enabled | KEY_ABSENT | no matches; snapshot exists | exit 1 |

Actual positive grep outputs:

```text
/private/tmp/aks-final-audit/before/.account-switcher/ch/6ecf2302/shell_snapshots/01a0e219-30bf-7801-9db4-dbd945ea3b3d.1790499696832744000.sh
/private/tmp/aks-final-audit/policy-only/.account-switcher/ch/9dd6623b/shell_snapshots/01a0e219-32d6-7e31-8e5c-5929a5571abc.1790499697368049000.sh
```

The patched Codex home was `/private/tmp/aks-final-audit/after/.account-switcher/ch/af615d4f`:
grep produced **no output, exit 1**. Claude's before/after homes were respectively
`.../claude-before/.account-switcher/ch/8fece3cb` and
`.../claude-after/.account-switcher/ch/385693b5`: both greps produced **no output, exit 1**.
Full local results: `/private/tmp/aks-final-audit-results.txt`.

Additional initial `codex exec` sessions reproduced the disk leak before any successful
inference. They were stopped after 25–35 seconds. Claude's fake-key print/TUI inference
attempts ended with ENOTFOUND because this environment cannot resolve the API host. Thus
these are real harness and shell-path experiments, not successful paid model turns or an
HTTP 401 authentication test. The Codex RPC probe needs no inference/network round trip.

Tested binaries on macOS arm64:

| Binary | Version | SHA-256 |
| --- | --- | --- |
| Codex | 0.157.0 | ad0be20d04e2ba6146ecdb51d7f8b7b0fe15420a15dc9b0057518d858f1f3714 |
| Claude Code | 2.1.282 | fcfd837103965c64de34a6b9b94370d77a347ea71819715a27d5f0ef01775ea4 |

## Regression coverage and limits

All previous secret-containment assertions remain. The preparation-only test was renamed and
its scope corrected. New policy tests cover every Codex credential variable; the live tests
start actual installed harnesses and inspect post-session files. They run with normal pytest,
skipping a harness only if its executable is absent. The Claude test requires a real snapshot,
so missing snapshot creation cannot count as success. Live tests passed for all four Codex
provider variables and Claude's auth token.

These controls prevent the reproduced inheritance/snapshot paths on the tested versions.
They are not a credential vault boundary against arbitrary same-user process inspection,
shell rc files that independently reload credentials, overridden configuration, or future
harness changes. Snapshot suppression also removes Codex's cached shell-state optimization;
the default secret-name filters can remove other credentials needed by shell commands.
Claude's scrub additionally removes pointers such as CLAUDE_CONFIG_DIR from subprocesses.
Existing compromised artifacts are not retroactively erased, and running sessions need to
restart with regenerated configuration. No real account files were changed during this work.

Final validation, after the implementation and live-test corrections:

- `python3 -m pytest`: **1508 passed, 15 skipped**, 50.93 seconds. All five new live
  containment cases ran and passed; none of the skips are those cases.
- `node --test app/web/render.test.mjs`: **119 passed, 0 failed, 0 skipped**.
- `git diff --check`: passed.
