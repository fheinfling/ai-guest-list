# Headroom "save credit" — removed (measured, not worth it)

Earlier versions had an optional **"save credit"** toggle that routed plain `codex`/`claude` (and the
GUI) through a local **Headroom** (`headroom-ai`) context-compression proxy. It kept turning itself
off, needed a hundreds-of-MB ML install, downloaded an `rtk` helper at runtime, shipped an
unauditable `_core.abi3.so`, and required a babysat version pin. Before removing it we **measured
whether it actually saved anything**, on real data, at zero provider cost, using the tool's own
instruments (`audit-reads`, `perf`, `output-savings`).

## What the measurement showed

- **Claude / Anthropic wire traffic** (`perf`, 2,049 real routed requests): **10.8% raw** token
  reduction. But cache-hit was 86% and `cache_read/before ≈ 96%`, so stale-read compression was
  shrinking tokens already billed at the 0.1× cached rate → **~1–3% cache-adjusted**. And it holds
  that 86% cache only by compressing ~nothing (`content_router` fired on 1.7% of traffic); pushing a
  stronger mode mutates the cached prefix and the savings eat themselves.
- **Output shaper** (`output-savings`): 51% of *output* tokens, but output was **0.05% of volume** —
  a rounding error, and statistically noisy (95% CI 18–85%).
- **Codex exec output** (17,364 real exec outputs): 46% aggregate, **but median per-call 0.2%** —
  the entire benefit was a handful of runaway commands (top 10 = 47% of savings) getting truncated.
  A tail guardrail against pathological outputs, not steady savings.

**Verdict:** compression on this workload is a guardrail, not a money-saver; the account **switcher**
is what keeps sessions sound while saving money. Headroom was removed.

## Cleanup / migration

`acctsw/headroom.py` now contains only an idempotent `cleanup_legacy()` that runs on the next app
launch / `cx` / `cl`: it strips any leftover provider routing from `~/.codex/config.toml` and
`~/.claude/settings.json` (restoring the exact pre-routing config from the snapshot when present),
stops an orphaned proxy by its PID file, and deletes the managed venv + bookkeeping. After it runs,
plain `codex`/`claude` talk directly to the provider as before.

The subprocess env-hygiene helpers that used to live here (`harden_env`, `_PY_ENV_STRIP` — strip
py2app's `PYTHONHOME`/`PYTHONPATH` from spawned children) moved to `acctsw/procenv.py`; they are
launcher/terminal infrastructure, unrelated to compression.
