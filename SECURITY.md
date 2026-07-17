# Security Policy

`ai guest list` moves AI-agent OAuth credentials between the macOS Keychain and the locations the
official `codex` / `claude` tools already read. It never transmits credentials off-device. We take
reports seriously.

## Reporting a vulnerability

**Please do not open a public issue for security problems.** Instead, use GitHub's private channel:

- Go to the [**Security tab → Report a vulnerability**](https://github.com/fheinfling/ai-guest-list/security/advisories/new)
  (GitHub private vulnerability reporting is enabled on this repo).

We'll acknowledge within a few days and work with you on a fix and coordinated disclosure.

## Scope

In scope:
- Credential handling — exposure/leakage of keychain blobs or OAuth tokens (logs, temp files,
  argv, world-readable files).
- The install/uninstall and shell-wiring paths, and the supervised launcher.
- The one-time `cleanup_legacy` migration that strips leftover Headroom routing from
  `~/.codex/config.toml` and `~/.claude/settings.json` (see
  [`docs/SECURITY-headroom.md`](docs/SECURITY-headroom.md)) — in particular, anything that could
  restore the wrong config or leave a stale proxy in the authenticated traffic path.

Out of scope:
- Vulnerabilities in the upstream `codex` / `claude` tools (report those to their projects).
- The `headroom-ai` package itself — the "save credit" proxy was removed; we no longer install,
  start, or route traffic through it.
- Social-engineering or physical access to an already-unlocked machine/keychain.

## Supported versions

This is early-stage software; only the latest release / `main` is supported. Please test against the
newest version before reporting.
