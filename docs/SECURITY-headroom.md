# Security evaluation — Headroom (save-credit)

Headroom (`headroom-ai`, Apache-2.0) is an **optional** integration: when "save credit" is on, the
supervised launcher routes the agent through Headroom's **local** proxy, which compresses context to
cut tokens. Because it sits in the agent's data path (it sees prompts and forwards your OAuth bearer
token) and edits `~/.codex` config, we reviewed it. **Pinned & reviewed version: `0.29.0`.**

## Method
Static review of upstream `0.29.0` source/docs + a live network-capture recipe for `headroom proxy`
under our hardened env (`acctsw.headroom.HARDENING_ENV`). Not covered: the compiled `_core.abi3.so`
(native, unauditable from source) and a full packet capture of in-flight provider traffic.

## Findings

**Verdict: safe for opt-in, local use. No credential leakage or covert egress found.**

- **Telemetry is forced OFF by us** on every subprocess (`--no-telemetry` +
  `HEADROOM_TELEMETRY=off`). Upstream exposes both the flag and env opt-out; we never rely on the
  package default.
- **All cloud features are opt-in, gated behind unset keys:** Langfuse tracing (`LANGFUSE_PUBLIC_KEY`/
  `_SECRET_KEY`), the `api.headroomlabs.ai` callback (`HEADROOM_API_KEY`), Qdrant memory
  (`HEADROOM_QDRANT_API_KEY`). None are set → default is **local-only**.
- **Proxy binds `127.0.0.1` only** (not `0.0.0.0`) — not exposed to the network.
- **Your bearer token only goes to the official provider.** Forwarding map (from the proxy's own
  startup banner): `/v1/messages → api.anthropic.com`, `/v1/chat/completions` & `/v1/responses →
  api.openai.com`, Gemini → `googleapis.com`. No path sends tokens to a third party.
- **Live-capture check is documented below:** under the hardened env, expected idle behavior is zero
  non-local Headroom connections; routed calls should go only to official provider endpoints.

## Caveats (known, not leakage)
1. **Local MITM by design** — when on, the proxy sees prompts and holds the bearer token in memory to
   forward it (inherent to any proxy; it's a local process you opt into).
2. **Runtime binary download** — on first `wrap`, the `rtk` helper is fetched from GitHub release
   assets and executed (supply-chain surface). Mitigated below.
3. **Compiled `_core.abi3.so`** ships in the wheel — trust the PyPI build (not locally auditable).
4. **`litellm`** transitive dep has its own telemetry — disabled below.
5. **Invasive to config** — routing through the proxy means writing `model_provider = "headroom"` +
   a `[model_providers.headroom]` block into `~/.codex/config.toml` and an `ANTHROPIC_BASE_URL` env
   entry into Claude's `settings.json`. We deliberately do **not** use `headroom install apply` (its
   macOS launchd deploy is broken); instead we run the proxy ourselves as a detached, PID-tracked
   subprocess and hand-write that minimal routing
   (`acctsw/headroom.py` `_route_all`). Our integration is **global & app-managed**: enabling
   snapshots the ORIGINAL files (bytes + mode + symlink target); disabling restores that exact
   backup when it exists, and falls back to a surgical unroute only for orphan/desync cleanup with no
   backup. A serialized `heal()`
   (keyed off actual on-disk injection state, not a flag) strips any dangling routing and stops the
   proxy on the next app launch or `cx`/`cl` run. So after a crash/force-quit, the dangling routing is
   healed the next time the app starts or you run `cx`/`cl`; a plain `codex`/`claude` run in that gap
   (app force-killed, not yet relaunched, not via `cx`/`cl`) can still hit the dead proxy until then.
   Normal quit removes routing on exit, so the gap only opens on an abnormal kill.

## Hardening we apply (`acctsw/headroom.py`)
- **Version pinned** to the reviewed `0.29.0` (`PINNED_VERSION`).
- **Env on every Headroom subprocess** (`HARDENING_ENV`): `HEADROOM_TELEMETRY=off`,
  `LITELLM_TELEMETRY=False`, `DO_NOT_TRACK=1`.
- **No cloud keys are ever set** by us (Langfuse/Qdrant/Headroom-API stay off).
- **`rtk` checksum-pinning (TOFU)** — `verify_rtk()` records rtk's sha256 on first sight and refuses
  to enable save-credit if it ever changes unexpectedly (supply-chain tamper guard). It runs on the
  enable path *before* the "already on" early-return, so a swapped binary is caught even when routing
  is already up.
- **Off by default** — nothing routes through Headroom unless you turn save-credit on. When you do,
  routing applies globally (plain `codex`/`claude` + the GUI + `cx`/`cl`); turning it off, quitting
  the app, or a dead-proxy health-check all restore your config.

## To re-verify later
```sh
HEADROOM_TELEMETRY=off LITELLM_TELEMETRY=False DO_NOT_TRACK=1 .venv/bin/headroom proxy
# in another shell: watch egress (should be official providers only, nothing at idle)
lsof -nP -iTCP -sTCP:ESTABLISHED | grep -iE 'headroom|rtk' | grep -v 127.0.0.1
```
For maximum assurance, run a full packet capture (e.g. Little Snitch / `tcpdump`) during a real
wrapped session and confirm destinations are only `api.openai.com` / `chatgpt.com` / `api.anthropic.com`.
