# Releasing

GitHub Actions builds an unsigned draft and its artifact attestation when you push a tag. The
maintainer verifies provenance, signs and notarizes locally, tests the signed app, then explicitly
publishes the draft. CI cannot run the signing script. The Developer ID private key stays in the
login keychain, never in the repo or GitHub secrets. See [the workflow](../.github/workflows/release.yml) and
[`scripts/release-sign.sh`](../scripts/release-sign.sh).

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

- Use macOS with Xcode command-line tools, Python 3.11+, and the GitHub CLI (`gh auth login`).
  `gh attestation verify --help` must support `--signer-workflow`, `--source-ref`, `--cert-identity`,
  `--cert-oidc-issuer` and `--deny-self-hosted-runners`; upgrade `gh` if it does not.
  Install the **Developer ID Application** certificate and its private key in the **login keychain**.
  The signing script selects the last matching identity by SHA-1 fingerprint. Set `AGL_SIGN_ID`
  to a specific fingerprint to override that selection; it must exist in the login keychain.
- Store notarization credentials locally (replace the placeholders locally; enter the app-specific
  password at the prompt, never in a file or command argument):

  ```sh
  xcrun notarytool store-credentials app-signing-password --apple-id '<APPLE_ID>' --team-id '<TEAM_ID>'
  ```

  The profile name defaults to `app-signing-password`; override it with `AGL_NOTARY_PROFILE`.
  No signing identity, certificate, password, or Apple ID belongs in Actions secrets.
- The tap checkout still needs the `TAP_PUSH_TOKEN` secret described below.

## Cut a release

1. Bump `__version__` in `acctsw/__init__.py` and its mirror in `pyproject.toml` (skip if already
   correct). Update the README and regenerate its screenshots after the version bump. Run the
   Python and web tests, then commit the release changes to `main` through the normal PR flow.
   If other work is in progress, prepare the release in a separate worktree based on `origin/main`.
2. Tag and push, then wait for the `build-and-release` job:
   ```sh
   git fetch origin main
   git tag vX.Y.Z origin/main
   git push origin vX.Y.Z
   ```
   CI keeps the macOS 14 / Python 3.11 universal build: tag verification, Python + web tests,
   web build, py2app, debug-symbol stripping, archive scan, and bundle smoke test. It creates a
   **draft** with generated notes and `AI-Guest-List-vX.Y.Z-unsigned.zip`. Before uploading the zip,
   CI prints its SHA-256 and uses the SHA-pinned `actions/attest-build-provenance` action. Only the
   build job has `id-token: write` and `attestations: write`; no Apple credentials are available to CI.
   It does not update the tap.
3. From a trusted checkout containing these scripts, sign locally and leave the draft unpublished:

   ```sh
   scripts/release-sign.sh vX.Y.Z --no-publish
   ```

   Before accessing the signing identity or Apple services, the script verifies the downloaded zip's
   attestation for `fheinfling/ai-guest-list`, `.github/workflows/release.yml`, the exact
   `refs/tags/vX.Y.Z` source ref and certificate identity, GitHub's OIDC issuer, and GitHub-hosted
   runners. It refuses missing CLI support, failed verification, or a missing unsigned zip and
   prints the verified SHA-256. A signed draft asset can never replace the attested unsigned input.
   Compare the verified digest with CI's job log.

   The script validates the bundle, signs every Mach-O and nested bundle inside out with hardened
   runtime, verifies the designated Developer ID requirement, notarizes, staples, and checks
   Gatekeeper. It uploads `AI-Guest-List-vX.Y.Z.zip` and `latest.json`, removes the unsigned zip,
   and keeps the generated notes. `--no-publish` leaves the draft unpublished for testing.
4. Download the signed draft zip and test the signed app: launch it, check the settings version,
   and exercise the normal menubar and terminal flows. Check Gatekeeper and the stapled ticket.
   Verify the zip's SHA-256 against `latest.json`. The manifest contains only public release metadata.
5. After testing, publish explicitly from the GitHub draft UI or locally:

   ```sh
   gh release edit vX.Y.Z --repo fheinfling/ai-guest-list --draft=false --latest
   ```

   This publishes the tested bytes without signing again. `update-tap` runs only on the
   **release published** event: it checks the published zip's SHA-256 against the manifest and
   bumps the cask. Homebrew manages upgrades because this branch has no in-app updater; the job
   removes any obsolete `auto_updates` declaration. Verify that job succeeds.
   Omitting `--no-publish` from a fresh local signing run also explicitly authorizes publication.

## Recovery and local checks

- Immediately after py2app, CI runs `python scripts/strip-bundle.py "dist/AI Guest List.app"`.
  PyObjC ships `.dSYM` debug bundles containing DWARF-only Mach-O files that py2app can copy into
  `python311.zip`; these are never loaded at runtime. The idempotent strip step removes only
  `.dSYM/` members from ZIPs under `Contents/Resources/lib/` and `.dSYM` directories in the app.
  CI then runs `python scripts/release-bundle.py scan "dist/AI Guest List.app"` to reject any
  remaining native code hidden in archives before smoke testing and creating the draft.
  Local builds need the same steps; the signing script fails closed and never strips files itself.
- If signing or notarization fails, fix the reported problem and re-run the same command on the
  draft. Notarization rejection prints the submission log command and retrieves the log. A zip
  containing native code inside another zip is rejected before signing: rebuild with those binaries
  unpacked. After a successful upload the unsigned asset is removed: publish the tested signed draft
  directly, or re-run CI to regenerate the unsigned asset and attestation before signing again.
  The script never edits a published release.
- To discard a draft, run `gh release delete vX.Y.Z --repo fheinfling/ai-guest-list --yes`.
  This leaves the tag in place; re-run the tag's workflow to recreate its draft. Avoid deleting a
  published release that installed apps or the tap already reference.
- An offline signing check needs no GitHub or notary access:

  ```sh
  AGL_SIGN_ID=- AGL_SKIP_NOTARIZE=1 scripts/release-sign.sh vX.Y.Z --from-zip /tmp/test-app.zip
  ```

  Both environment settings and `--from-zip` are required. Attestation verification is skipped with
  an explicit message and no GitHub call. The script verifies the ad-hoc signatures,
  then exits with failure at the Developer ID requirement, as expected. This mode cannot upload or
  publish. Temporary working files are cleaned up on exit.
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

- **README screenshots.** Regenerate with `.venv/bin/python scripts/screenshots.py` whenever the
  popover or settings view changes (and check the version in the settings footer). It seeds sample
  seats, runs them through the real engine, and shoots the real web UI — so the images can't drift
  from the app. They had: the shots advertised a "save credit" toggle for months after the feature
  was removed, captioned with a savings number the code itself disproved.
- **Signed releases.** Starting with 1.4.0, published apps are Developer ID signed, notarized and
  stapled. CI's unsigned zip exists only on the draft. Upgrade older builds through Homebrew or a
  direct download; see [Install](../README.md#install).
- **Homebrew tap.** The `update-tap` job authenticates with a fine-grained PAT stored as the
  `TAP_PUSH_TOKEN` Actions secret (scoped to `fheinfling/homebrew-tap`, **Contents: Read and write**).
  If the token expires the job fails with a 403 — rotate it by generating a new PAT and running
  `gh secret set TAP_PUSH_TOKEN --repo fheinfling/ai-guest-list`. The job no-ops when the cask is
  already current, so re-running a release is safe.
