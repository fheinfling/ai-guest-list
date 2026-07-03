# Releasing

Releases are built and published by GitHub Actions when you push a tag — see
[`.github/workflows/release.yml`](../.github/workflows/release.yml). You don't build locally.

## Versioning

- **Marketing version** (`vX.Y.Z`) lives in **one** place: `acctsw/__init__.py` `__version__`.
  `setup.py` reads it for `CFBundleShortVersionString`. The release job **fails** if the pushed tag
  doesn't match it, so the tag and the code can never disagree.
- **Build number** is the git commit count (`git rev-list --count HEAD`) → `CFBundleVersion`.
  It's monotonic and automatic; you never set it by hand.
- Both are shown in the app's **settings sheet** (`v0.1.0 · build 123`), sourced at runtime from the
  bundle's `Info.plist` (so it's correct in the shipped `.app` and reads `dev` from a source checkout).

## Cut a release

1. Bump `__version__` in `acctsw/__init__.py` (skip if the version is already what you want to ship).
   Commit it to `main`.
2. Tag and push:
   ```sh
   git tag v0.1.0
   git push origin v0.1.0
   ```
3. The `release` workflow runs on a macOS runner: verifies the tag matches `__version__`, runs the
   test suite + web tests (a hard gate), builds the `.app` via py2app, zips it with `ditto`, and
   creates a GitHub Release with auto-generated notes and the zip attached. A second job
   (`update-tap`) then bumps `Casks/ai-guest-list.rb` in `fheinfling/homebrew-tap` to the new version
   + zip sha256 and pushes it — the tap is **not** maintained by hand.

## Notes

- **Unsigned.** No Apple Developer certificate is configured, so the `.app` is unsigned/un-notarized.
  Installing via `brew install --cask --no-quarantine fheinfling/tap/ai-guest-list` skips the
  Gatekeeper warning. If a user installs without it (or uses the zip) and macOS blocks the app: on
  **macOS 15 (Sequoia)+** open the app once, then **System Settings → Privacy & Security → Open
  Anyway** (right-click → Open no longer bypasses Gatekeeper on 15+); on **macOS 14 and earlier**
  right-click → Open → Open; or, on any version,
  `xattr -dr com.apple.quarantine "/Applications/AI Guest List.app"`. Wire signing/notarization into
  the workflow once a Developer ID is available.
- **Homebrew tap.** The `update-tap` job authenticates with a fine-grained PAT stored as the
  `TAP_PUSH_TOKEN` Actions secret (scoped to `fheinfling/homebrew-tap`, **Contents: Read and write**).
  If the token expires the job fails with a 403 — rotate it by generating a new PAT and running
  `gh secret set TAP_PUSH_TOKEN --repo fheinfling/ai-guest-list`. The job no-ops when the cask is
  already current, so re-running a release is safe.
- **Re-running a release:** delete the tag and GitHub Release, then re-tag
  (`git tag -d vX.Y.Z && git push origin :vX.Y.Z`).
