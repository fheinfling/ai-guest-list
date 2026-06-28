# Security evaluation — Headroom (save-credit)

Headroom (`headroom-ai`, Apache-2.0) is an **optional** integration: when "save credit" is on, the
supervised launcher routes the agent through Headroom's **local** proxy, which compresses context to
cut tokens. Because it sits in the agent's data path (it sees prompts and forwards your OAuth bearer
token) and edits `~/.codex` config, we audited it. **Pinned & audited version: `0.27.0`.**

## Method
Static review of the installed package + a live network capture of `headroom proxy` under our
hardened env (`acctsw.headroom.HARDENING_ENV`). Not covered: the compiled `_core.abi3.so` (native,
unauditable from source) and a full packet capture of in-flight provider traffic.

## Findings

**Verdict: safe for opt-in, local use. No credential leakage or covert egress found.**

- **Telemetry is OFF by default** (flag help: *"anonymous usage telemetry — off by default"*). Even
  when enabled, the reporter's contract is: *"Never sends message content, API keys, prompts, tool
  results, or user data — only aggregate counts."*
- **All cloud features are opt-in, gated behind unset keys:** Langfuse tracing (`LANGFUSE_PUBLIC_KEY`/
  `_SECRET_KEY`), the `api.headroomlabs.ai` callback (`HEADROOM_API_KEY`), Qdrant memory
  (`HEADROOM_QDRANT_API_KEY`). None are set → default is **local-only**.
- **Proxy binds `127.0.0.1` only** (not `0.0.0.0`) — not exposed to the network.
- **Your bearer token only goes to the official provider.** Forwarding map (from the proxy's own
  startup banner): `/v1/messages → api.anthropic.com`, `/v1/chat/completions` & `/v1/responses →
  api.openai.com`, Gemini → `googleapis.com`. No path sends tokens to a third party.
- **Live capture (hardened env):** startup banner showed `License: OSS (no license key)` and
  `Telemetry: DISABLED`; the proxy made **zero non-local connections at idle**. (A `codex-aar →
  Cloudflare` connection observed was the user's Codex desktop app, not Headroom.)

## Caveats (known, not leakage)
1. **Local MITM by design** — when on, the proxy sees prompts and holds the bearer token in memory to
   forward it (inherent to any proxy; it's a local process you opt into).
2. **Runtime binary download** — on first `wrap`, the `rtk` helper is fetched from GitHub release
   assets and executed (supply-chain surface). Mitigated below.
3. **Compiled `_core.abi3.so`** ships in the wheel — trust the PyPI build (not locally auditable).
4. **`litellm`** transitive dep has its own telemetry — disabled below.
5. **Invasive to config** — `headroom wrap` rewrites `~/.codex/config.toml` + `AGENTS.md` + adds an
   MCP server. Our integration runs it **session-scoped** (`headroom.scoped`) and restores those
   files byte-for-byte on exit.

## Hardening we apply (`acctsw/headroom.py`)
- **Version pinned** to the audited `0.27.0` (`PINNED_VERSION`).
- **Env on every wrapped session** (`HARDENING_ENV`): `HEADROOM_TELEMETRY=off`,
  `LITELLM_TELEMETRY=False`, `DO_NOT_TRACK=1`.
- **No cloud keys are ever set** by us (Langfuse/Qdrant/Headroom-API stay off).
- **`rtk` checksum-pinning (TOFU)** — `verify_rtk()` records rtk's sha256 on first sight and refuses
  to run with save-credit if it ever changes unexpectedly (supply-chain tamper guard).
- **Off by default + session-scoped** — nothing routes through Headroom unless you toggle it and run
  via `cx`/`cl`; config is restored afterward.

## To re-verify later
```sh
HEADROOM_TELEMETRY=off LITELLM_TELEMETRY=False DO_NOT_TRACK=1 .venv/bin/headroom proxy
# in another shell: watch egress (should be official providers only, nothing at idle)
lsof -nP -iTCP -sTCP:ESTABLISHED | grep -iE 'headroom|rtk' | grep -v 127.0.0.1
```
For maximum assurance, run a full packet capture (e.g. Little Snitch / `tcpdump`) during a real
wrapped session and confirm destinations are only `api.openai.com` / `chatgpt.com` / `api.anthropic.com`.
