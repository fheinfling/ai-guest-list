# Security Policy

`ai guest list` manages AI-agent subscription seats (OAuth credentials in the macOS Keychain and
per-seat homes under `~/.account-switcher/ch/` and `~/.account-switcher/clh/`, plus the stock
locations `codex` and `claude` read) and optional BYOK API-key seats (`acct-switcher` Keychain
items). Credentials are stored locally and sent to provider endpoints for authentication and API
requests. We take reports seriously.

## Reporting a vulnerability

**Please do not open a public issue for security problems.** Instead, use GitHub's private channel:

- Go to the [**Security tab → Report a vulnerability**](https://github.com/fheinfling/ai-guest-list/security/advisories/new)
  (GitHub private vulnerability reporting is enabled on this repo).

We'll acknowledge within a few days and work with you on a fix and coordinated disclosure.

## Scope

In scope:
- Credential & seat-home handling (`acctsw/keychain.py`, `acctsw/credlocations.py`,
  `acctsw/codexhome.py`, `acctsw/claudehome.py`, `acctsw/claudesync.py`, `acctsw/authflows.py`,
  `acctsw/switch.py`) — exposure/leakage of Keychain blobs or OAuth tokens (logs, temp files,
  `argv`, world-readable files), cross-seat home contamination, or clobbering/revoking a live login.
- API-key seats & secret containment (`acctsw/keyseats.py`, `acctsw/keyhome.py`,
  `acctsw/pricing.py`, `acctsw/keyprove.py`, `acctsw/handoff.py`) — Keychain-only secret storage
  (`key:<id>` under service `acct-switcher`), fingerprint-only metadata in `state.json`, passing
  keys strictly via child environment (`ChildEnv`, never `argv` or files), isolated `0700` per-key
  homes (`~/.account-switcher/ch/<hash>`) with no symlinks to shared `~/.codex` or `~/.claude`,
  disabling Codex shell snapshots (`shell_snapshot = false` and `shell_environment_policy`) and
  scrubbing Claude subprocess environments (`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`), and redacting
  echoed keys from catalog caches (`pricing.json`), errors, and UI state.
- Per-session local HTTPS proxy & ephemeral CA (`acctsw/seatproxy.py`, `acctsw/sessionca.py`) —
  used for in-place Claude Code seat switching: loopback-only binding (`127.0.0.1:0`), per-session
  random Basic `Proxy-Authorization` secret (`acctsw:<secret>`), ephemeral EC P-256 CA (`SessionCA`)
  constrained with `basicConstraints=critical,CA:TRUE,pathlen:0` and
  `nameConstraints=critical,permitted;DNS.0=api.anthropic.com`, trusted only by that child process
  via environment variables in a `0700` temp directory (never added to the macOS Keychain) with
  private keys (`ca.key`, `leaf.key`) unlinked from disk immediately after loading into the
  in-memory `ssl.SSLContext`, and bearer-token replacement restricted to `POST /v1/messages` and
  `POST /v1/messages/count_tokens` carrying Claude Code OAuth headers (other hosts use a blind byte
  tunnel; tokens stay in memory and log only as `sha256[:10]`).
- The in-app updater (`acctsw/updater.py`, `scripts/update-requirement.txt`) — redirect host
  allowlisting, zip traversal/symlink containment, SHA-256 verification against `latest.json`,
  designated-requirement `codesign` verification, Gatekeeper assessment, `Info.plist` checks, and
  bundle swap/rollback integrity.
- The install/uninstall and shell-wiring paths, the supervised launcher (`acctsw/launcher.py`), and
  idle engine self-restart (`acctsw/enginerestart.py`).
- The build and release pipeline (`scripts/`, the GitHub workflows).
- The one-time `cleanup_legacy` migration that strips leftover Headroom routing from
  `~/.codex/config.toml` and `~/.claude/settings.json` (see
  [`docs/SECURITY-headroom.md`](docs/SECURITY-headroom.md)) — in particular, anything that could
  restore the wrong config or leave a stale proxy in the authenticated traffic path.

Out of scope:
- Vulnerabilities in the upstream `codex` / `claude` tools or third-party API providers (report
  those to their projects).
- The `headroom-ai` package itself — the "save credit" proxy was removed; we no longer install,
  start, or route traffic through it.
- Social-engineering or physical access to an already-unlocked machine/keychain.

## Release signing & in-app update verification

Releases are signed and notarized by the `release` workflow. Its signing job runs only for `v*` tags (which only the
maintainer can create) and only after the maintainer approves it. It uses a Developer ID certificate kept just for
CI and an App Store Connect API key, both stored as secrets of the `release` environment. No other job,
pull request or fork can read them. Each release zip has a provenance attestation (`gh attestation verify`).
See [`docs/RELEASING.md`](docs/RELEASING.md).

The in-app updater (`acctsw/updater.py`) verifies updates before replacing the app bundle:

1. Fetches `latest.json` and the release zip over HTTPS only from allowlisted GitHub hosts
   (`github.com`, `release-assets.githubusercontent.com`, `objects.githubusercontent.com`), verified
   on the initial URL and on every redirect hop, with strict byte-size caps.
2. Verifies the downloaded zip's exact byte size and SHA-256 digest against `latest.json`. Before
   extraction, checks member names for absolute paths, `..`, backslashes, and anything outside a
   single top-level `AI Guest List.app/`, with entry-count and unpacked-size caps. After `ditto`
   extraction in a private `0700` staging directory under `~/.account-switcher/updates`,
   `_check_layout` rejects symlinks escaping the app and special files; links contained within the
   app are allowed.
3. Verifies the extracted app with `/usr/bin/codesign --verify --deep --strict -R "=<requirement>"`
   against the designated requirement bundled in `Contents/Resources/update-requirement.txt`
   (pinning the Developer ID authority and Team ID), assesses it with Gatekeeper
   (`/usr/sbin/spctl --assess --type execute`), and validates `Info.plist` (`CFBundleIdentifier`,
   `CFBundleShortVersionString` matching the release tag, numeric `CFBundleVersion`, and
   `LSMinimumSystemVersion`).
4. Swaps the bundle via a detached helper that re-verifies `codesign --verify --deep --strict -R`
   on `.<App>.app.new-<pid>` beside the running bundle and keeps the previous bundle at
   `.<App>.previous-<pid>.app`. The target is the running bundle's existing location, which must be
   writable and outside App Translocation. The helper restores the backup if the new app fails to
   launch or `~/.account-switcher/update-ack` does not contain the new version within the
   acknowledgement wait.

## Supported versions

This is early-stage software; only the latest release / `main` is supported. Please test against the
newest version before reporting.
