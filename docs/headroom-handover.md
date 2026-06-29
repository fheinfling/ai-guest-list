# Headroom — Handover (resume in CLI)

> Written 2026-06-29. Context: recurring `Unable to connect to API (ConnectionRefused)`
> crashes in Claude Code, traced to Headroom's persistent (launchd) deployment.

## TL;DR — how to run Claude Code through Headroom from the CLI

```bash
cd ~/Documents/GitHub/ai-guest-list
.venv/bin/headroom wrap claude
```

That's it. `wrap` starts its own managed proxy, sets `ANTHROPIC_BASE_URL`, and launches
Claude Code routed through it. Everything is scoped to that one process — quit Claude and
the proxy + routing disappear. **Do NOT use `headroom install apply` or the desktop app's
Headroom toggle** — that's the broken path (see root cause below).

Verify routing is live (in another terminal, while a wrapped session is running):
```bash
tail -f ~/.headroom/logs/proxy.log   # look for `proxy_inbound_request` lines
```

How to tell ANY session's routing state:
- `echo $ANTHROPIC_BASE_URL` → `https://api.anthropic.com` = direct (NOT Headroom);
  `http://127.0.0.1:8787` = routed through Headroom.
- `lsof -nP -iTCP:8787 -sTCP:LISTEN` → a listener means the proxy is up.

## Root cause (confirmed, not fixed upstream)

The **proxy itself works perfectly** — verified by running it directly:
`/readyz` returns `ready:true, status:healthy` in ~5s. The failure is **only** in
Headroom's `persistent-service` (launchd) wrapper used by `install apply` / the app toggle:

1. The generated launchd plist has **no `StandardErrorPath`/`StandardOutPath`** — so
   startup crashes are silent (`headroom/install/supervisors.py` `_macos_launchd_plist`).
2. launchd `gui/$UID` agents run with a bare environment; the runner command resolved via
   `shutil.which("headroom")` at apply time isn't reachable, so it dies before binding 8787
   (`headroom/install/runtime.py:resolve_headroom_command`).
3. On readiness failure, `install apply` **rolls back and `rmtree`s the profile dir**
   (`headroom/install/state.py:delete_manifest`), erasing all evidence — `~/.headroom/deploy/`
   is always empty afterward.

**Why the crashes:** `install apply` injects `ANTHROPIC_BASE_URL → http://127.0.0.1:8787`
*before* the proxy is ready. Proxy never comes up → live Claude points at a dead port →
`ConnectionRefused 1/10…10/10 → crash`. Each diagnostic retry re-triggered it.

**Upstream status:** Installed `headroom-ai==0.27.0` is the latest published release. Checked
`main` (unreleased): all three flaws above are still present. The only queued launchd/install
fixes are tangential — #833 (docker-preset ENTRYPOINT dup) and #1289 (`install restart/start`
recovery after a stop, not first-apply readiness). **Upgrading will not fix this.**

## Current machine state (clean / safe)

- `ANTHROPIC_BASE_URL=https://api.anthropic.com` (direct — untouched throughout diagnosis)
- Nothing listening on 8787; no `com.headroom.*` LaunchAgent loaded; `~/.headroom/deploy/` empty
- The diagnosis only ran the proxy in the foreground once (killed) — no persistent changes made.

## Optional: always-on proxy (durable, survives reboot)

Only if you want Headroom up without typing `wrap` each time. This bypasses the broken
launchd machinery with a correctly-written LaunchAgent (absolute venv python, KeepAlive,
**with** error logging). Then route Claude with `headroom wrap claude --no-proxy` (uses the
already-running proxy) or by setting `ANTHROPIC_BASE_URL=http://127.0.0.1:8787`.

Save as `~/Library/LaunchAgents/com.headroom.claude.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.headroom.claude</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/franz.heinfling/Documents/GitHub/ai-guest-list/.venv/bin/python</string>
    <string>-m</string><string>headroom.cli</string><string>proxy</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>8787</string>
    <string>--mode</string><string>token</string>
    <string>--backend</string><string>anthropic</string>
    <string>--no-telemetry</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>/Users/franz.heinfling/.headroom/logs/launchd-claude.out.log</string>
  <key>StandardErrorPath</key><string>/Users/franz.heinfling/.headroom/logs/launchd-claude.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key><string>/Users/franz.heinfling</string>
    <key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HEADROOM_TELEMETRY</key><string>off</string>
  </dict>
</dict>
</plist>
```

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.headroom.claude.plist
curl -s http://127.0.0.1:8787/readyz | python3 -c 'import sys,json;d=json.load(sys.stdin);print("ready:",d["ready"],"upstream:",d["checks"]["upstream"]["status"])'
# to remove later:
# launchctl bootout gui/$(id -u)/com.headroom.claude && rm ~/Library/LaunchAgents/com.headroom.claude.plist
```

## Open item

File the upstream bug (silent launchd failures + no error logging + destructive first-apply
rollback — all confirmed on `main`). Repo: https://github.com/chopratejas/headroom/issues
