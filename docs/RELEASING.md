# Releasing

Pushing a tag runs [the release workflow](../.github/workflows/release.yml) in three jobs:

- **build** has no secrets. It verifies the tag, runs the tests, builds, strips, scans and
  smoke-tests the app, and hands the unsigned zip and its SHA-256 to the next job.
- **sign** runs in the `release` environment, the only place the signing secrets exist. GitHub
  starts it only after the maintainer approves it, and only for `v*` tags, which only the
  maintainer can create (tag ruleset). It runs the tag's own release scripts and GitHub's own
  actions, nothing third-party, in a throwaway keychain that is deleted afterwards.
- **publish** has no secrets. It attests the signed zip's provenance and attaches it, with
  `latest.json`, to a **draft** release. Publishing the draft is a separate, explicit step.

## Versioning

- **Marketing version** (`vX.Y.Z`) is defined in: `acctsw/__init__.py` `__version__`.
  `setup.py` reads it for `CFBundleShortVersionString`; `pyproject.toml` mirrors it for package
  metadata, and the smoke tests require the two to agree. The release job **fails** if the pushed tag
  doesn't match it, so the tag and the code can never disagree.
- **Build number** is the git commit count (`git rev-list --count HEAD`) → `CFBundleVersion`.
  It's monotonic and automatic; you never set it by hand.
- Both are shown in the app's **settings sheet** (`v0.1.0 · build 123`), sourced at runtime from the
  bundle's `Info.plist` (so it's correct in the shipped `.app` and reads `dev` from a source checkout).

## One-time prerequisites

The `release` environment (Settings › Environments) requires the maintainer's approval and only
accepts `v*` tags. It holds five secrets:

| Secret | What it is |
|---|---|
| `MACOS_CERT_P12` | A **Developer ID Application** certificate and its private key, exported as `.p12`, base64-encoded |
| `MACOS_CERT_PASSWORD` | The `.p12` export password |
| `NOTARY_API_KEY` | An App Store Connect API key (`AuthKey_….p8`, the file's contents) with the Developer role |
| `NOTARY_API_KEY_ID` | That key's ID |
| `NOTARY_API_ISSUER` | The issuer ID shown above the keys in App Store Connect |

Use a certificate kept just for CI, so it can be revoked without touching the one on your Mac. Set
the secrets from a trusted machine, never in a file in the repo:

```sh
base64 -i ci-signing.p12 | gh secret set MACOS_CERT_P12 --env release --repo fheinfling/ai-guest-list
gh secret set MACOS_CERT_PASSWORD --env release --repo fheinfling/ai-guest-list
gh secret set NOTARY_API_KEY --env release --repo fheinfling/ai-guest-list < AuthKey_XXXXXXXXXX.p8
gh secret set NOTARY_API_KEY_ID --env release --repo fheinfling/ai-guest-list
gh secret set NOTARY_API_ISSUER --env release --repo fheinfling/ai-guest-list
```

The certificate must belong to the team in `scripts/update-requirement.txt` (`<TEAM_ID>` below);
the sign job refuses anything else. The tap job also needs the `TAP_PUSH_TOKEN` secret described
under Notes.

## Cut a release

1. Bump `__version__` in `acctsw/__init__.py` and its mirror in `pyproject.toml` (skip if already
   correct). Update the README and regenerate its screenshots after the version bump. Run the
   Python and web tests, then commit the release changes to `main` through the normal PR flow.
   If other work is in progress, prepare the release in a separate worktree based on `origin/main`.
2. Tag and push:
   ```sh
   git fetch origin main
   git tag vX.Y.Z origin/main
   git push origin vX.Y.Z
   ```
   The build job keeps the macOS 14 / Python 3.11 build: tag verification, Python + web tests, web
   build, py2app, debug-symbol stripping, archive scan and bundle smoke test.
3. When the build job is done, approve the sign job: open the run, **Review deployments**, tick
   `release`, **Approve and deploy**. The sign job checks the zip's SHA-256 against the build job's
   and validates the bundle and `Info.plist`. It then signs every Mach-O and nested bundle inside
   out with the hardened runtime (`scripts/release-bundle.py sign`), verifies the designated
   requirement, notarizes, staples and checks Gatekeeper. Finally it re-checks the extracted
   download zip and writes `latest.json`.
4. The publish job attests `AI-Guest-List-vX.Y.Z.zip` and creates the draft with generated notes.
   Download the zip from the draft and check where it came from:

   ```sh
   gh attestation verify AI-Guest-List-vX.Y.Z.zip --repo fheinfling/ai-guest-list \
     --cert-identity "https://github.com/fheinfling/ai-guest-list/.github/workflows/release.yml@refs/tags/vX.Y.Z" \
     --cert-oidc-issuer https://token.actions.githubusercontent.com --deny-self-hosted-runners
   ```

   Then test the signed app: launch it, check the settings version, and exercise the normal
   menubar and terminal flows. Verify the zip's SHA-256 against `latest.json`. The manifest
   contains only public release metadata.
5. After testing, publish from the GitHub draft UI or locally:

   ```sh
   gh release edit vX.Y.Z --repo fheinfling/ai-guest-list --draft=false --latest
   ```

   `update-tap` runs only on the **release published** event: it checks the published zip's
   SHA-256 against the manifest and bumps the cask. Homebrew manages upgrades because this branch
   has no in-app updater; the job removes any obsolete `auto_updates` declaration and the legacy
   unsigned-build `caveats` notice. Verify that job succeeds.

## Recovery and local checks

- Immediately after py2app, the builds run `python scripts/strip-bundle.py "dist/AI Guest List.app"`.
  PyObjC ships `.dSYM` debug bundles containing DWARF-only Mach-O files that py2app can copy into
  `python311.zip`; these are never loaded at runtime. The idempotent strip step removes only
  `.dSYM/` members from ZIPs under `Contents/Resources/lib/` and `.dSYM` directories in the app.
  Then `python scripts/release-bundle.py scan "dist/AI Guest List.app"` rejects any remaining
  native code hidden in archives. Local builds need the same steps; signing fails closed and never
  strips files itself.
- If signing or notarization fails, the run shows why (a rejected notarization prints Apple's log).
  Fix the problem, then re-run the failed jobs; the sign job asks for approval again. A zip
  containing native code inside another zip is rejected before signing: rebuild with those
  binaries unpacked. The workflow never edits a published release.
- To discard a draft, run `gh release delete vX.Y.Z --repo fheinfling/ai-guest-list --yes`.
  This leaves the tag in place; re-run the tag's workflow to recreate its draft. Avoid deleting a
  published release that installed apps or the tap already reference.
- To check the signing order and the designated requirement without any credentials, sign a build
  ad hoc. The requirement check then fails, as it must for anything but the Developer ID team:

  ```sh
  python scripts/release-bundle.py sign "dist/AI Guest List.app" --identity=- --adhoc \
    --entitlements app/entitlements.plist
  codesign --verify --deep --strict -R "=$(cat scripts/update-requirement.txt)" "dist/AI Guest List.app"
  ```

- The shared updater requirement is in [`scripts/update-requirement.txt`](../scripts/update-requirement.txt).
  Any updater MUST enforce this designated requirement using codesign -R / SecStaticCode, in
  addition to the manifest SHA-256, before installing an update. A matching hash alone does not
  authenticate the publisher. No updater code is included on this branch. The requirement's Team ID
  is mechanically required public code-signing metadata, visible in every signed app; it stays in
  that file only. Documentation uses `<TEAM_ID>` instead.
  [`app/entitlements.plist`](../app/entitlements.plist) allows unsigned executable memory for
  CPython's ctypes/libffi and PyObjC callbacks. Library validation stays enabled because bundled
  binaries are all re-signed with the same team; it need not be disabled for this app.
  See [PyObjC's signing guidance](https://pyobjc.readthedocs.io/en/latest/notes/codesigning.html) and
  [Apple's library validation rules](https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.security.cs.disable-library-validation).

## Notes

- **Bundle content gate.** `scripts/release-bundle.py scan` (run by PR CI on every build and by
  the release build before signing) refuses CPython test suites (excluded in `setup.py`) and
  any PEM private-key block, in loose files or inside archives.
- **README screenshots.** Regenerate with `.venv/bin/python scripts/screenshots.py` whenever the
  popover or settings view changes (and check the version in the settings footer). It seeds sample
  seats, runs them through the real engine, and shoots the real web UI — so the images can't drift
  from the app. They had: the shots advertised a "save credit" toggle for months after the feature
  was removed, captioned with a savings number the code itself disproved.
- **Signed releases.** Starting with 1.4.0, published apps are Developer ID signed, notarized and
  stapled. The unsigned build never leaves Actions: it passes from the build job to the sign job as a
  workflow artifact. Upgrade older builds through Homebrew or a direct download; see
  [Install](../README.md#install).
- **Homebrew tap.** The `update-tap` job authenticates with a fine-grained PAT stored as the
  `TAP_PUSH_TOKEN` Actions secret (scoped to `fheinfling/homebrew-tap`, **Contents: Read and write**).
  If the token expires the job fails with a 403 — rotate it by generating a new PAT and running
  `gh secret set TAP_PUSH_TOKEN --repo fheinfling/ai-guest-list`. The job no-ops when the cask is
  already current, so re-running a release is safe.
