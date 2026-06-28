# ai guest list 🎟️

A lean macOS menubar app + CLI that keeps your AI coding agents **on the floor** — automatically
switching between multiple **Codex** and **Claude** accounts ("seats") when one hits its usage
limit, and **continuing your in-progress work** on the next seat. When every seat is resting, it
picks the one that **unlocks soonest**. No browser dance.

> Built for the case of running several paid agent subscriptions (e.g. two Codex business seats,
> Claude soon) and never wanting to stop mid-task because one ran out of credit.

## What it does
- **Auto-switch on limit** — when the active account is rate-limited, hop to another *same-tool*
  seat and resume the same conversation (`codex resume` / `claude --resume`).
- **Soonest-unlock selection** — if all seats are resting, choose the one whose limit resets first.
- **Live limits in the bar** — 5h + weekly usage and reset timers, read from the official usage
  endpoints (cached, gently polled).
- **Add / remove seats** — sign in via the official flow *or* a no-browser path (Codex `auth.json`,
  Claude `setup-token`). Credentials live only in the **macOS Keychain**.
- **Save credit (Headroom)** — optional toggle that routes agents through
  [Headroom](https://github.com/headroomlabs-ai/headroom) to compress context → fewer tokens →
  limits reached slower.
- **Non-destructive** — stock `codex` / `claude` and the desktop apps keep working untouched; a
  factory-image backup makes uninstall a clean restore.

## Architecture
- `acctsw/` — the engine (Python, stdlib + `security` CLI): credential swap, usage reading,
  account selection, the supervised launcher, install/uninstall.
- `app/` — the "ai guest list" menubar app (`pyobjc` `NSStatusItem` + `WKWebView` popover) — a thin
  UI over the engine.
- `cx` / `cl` — supervised CLI launchers for codex / claude (stock binaries are never shadowed).

See [`docs/PLAN.md`](docs/PLAN.md) for the full design.

## Status
🚧 Early development — built milestone by milestone with a continuous review loop.

## Safety
Credentials are only ever moved between the Keychain and the locations the official tools already
read. Nothing is proxied or transmitted anywhere. Credentials are **never** committed to git.

## License
MIT — see [`LICENSE`](LICENSE).
