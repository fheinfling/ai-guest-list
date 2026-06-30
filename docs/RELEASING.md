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
   creates a GitHub Release with auto-generated notes and the zip attached.

## Notes

- **Unsigned.** No Apple Developer certificate is configured, so the `.app` is unsigned/un-notarized.
  Users open it via right-click → Open, or
  `xattr -dr com.apple.quarantine "/Applications/AI Guest List.app"`. Wire signing/notarization into
  the workflow once a Developer ID is available.
- **Re-running a release:** delete the tag and GitHub Release, then re-tag
  (`git tag -d vX.Y.Z && git push origin :vX.Y.Z`).
