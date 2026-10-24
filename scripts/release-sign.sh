#!/usr/bin/env bash
# Sign CI's draft locally: the Developer ID private key stays in the login keychain.
# Never export a certificate/private key or put signing credentials in GitHub secrets.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="fheinfling/ai-guest-list"
PROFILE="${AGL_NOTARY_PROFILE:-app-signing-password}"
PY="${PY:-python3}"
NO_PUBLISH=0
FROM_ZIP=""
TAG=""

die() { echo "release-sign: $*" >&2; exit 1; }
usage() {
  echo 'Usage: scripts/release-sign.sh vX.Y.Z [--no-publish] [--from-zip PATH]'
  echo 'Offline dry run: AGL_SIGN_ID=- AGL_SKIP_NOTARIZE=1 ... vX.Y.Z --from-zip PATH'
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-publish) NO_PUBLISH=1; shift ;;
    --from-zip) [[ $# -ge 2 ]] || die '--from-zip needs a path'; FROM_ZIP="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    v*) [[ -z "$TAG" ]] || die 'only one tag is allowed'; TAG="$1"; shift ;;
    *) die "unexpected argument: $1" ;;
  esac
done
[[ "${CI:-false}" == false && "${GITHUB_ACTIONS:-false}" == false ]] ||
  die 'signing requires an explicit local run; CI is forbidden'
[[ "$(uname -s)" == Darwin ]] || die 'macOS is required (codesign, ditto, notarytool)'
[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die 'tag must be vX.Y.Z'
DRY_RUN=0
if [[ "${AGL_SIGN_ID:-}" == '-' || "${AGL_SKIP_NOTARIZE:-0}" != 0 ]]; then
  [[ "${AGL_SIGN_ID:-}" == '-' && "${AGL_SKIP_NOTARIZE:-0}" == 1 ]] ||
    die 'dry run requires BOTH AGL_SIGN_ID=- and AGL_SKIP_NOTARIZE=1'
  [[ -n "$FROM_ZIP" ]] || die 'dry run requires --from-zip; upload/publish is forbidden'
  DRY_RUN=1
fi
if [[ -n "$FROM_ZIP" ]]; then
  [[ "$DRY_RUN" == 1 ]] || die '--from-zip is only for the offline ad-hoc dry run'
  [[ -f "$FROM_ZIP" ]] || die "zip does not exist: $FROM_ZIP"
fi
for tool in "$PY" codesign ditto; do command -v "$tool" >/dev/null || die "missing tool: $tool"; done
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/agl-release.XXXXXXXX")"
trap 'rm -rf "$WORK_DIR"' EXIT
UNSIGNED="AI-Guest-List-${TAG}-unsigned.zip"
ASSET="AI-Guest-List-${TAG}.zip"

check_draft() {
  gh release view "$TAG" --repo "$REPO" --json isDraft,isPrerelease,body > "$WORK_DIR/release.json"
  "$PY" - "$WORK_DIR/release.json" <<'PYEOF'
import json, sys
release = json.load(open(sys.argv[1], encoding="utf-8"))
if release["isDraft"] is not True or release["isPrerelease"] is not False:
    sys.exit("release-sign: release must exist as a non-prerelease draft")
PYEOF
}

if [[ "$DRY_RUN" == 1 ]]; then
  IDENTITY='-'
  cp "$FROM_ZIP" "$WORK_DIR/$UNSIGNED"
  echo 'OFFLINE AD-HOC DRY RUN: no GitHub access, notarization, upload or publication.'
  echo 'Skipping attestation verification for the offline ad-hoc dry run.'
else
  for tool in gh security xcrun spctl; do command -v "$tool" >/dev/null || die "missing tool: $tool"; done
  gh attestation verify --help >/dev/null 2>&1 ||
    die 'gh attestation verify is required; upgrade GitHub CLI before signing'
  gh auth status
  check_draft
  # Only CI's attested unsigned bytes may enter signing. A previously signed zip
  # has a different digest and must never serve as a recovery fallback.
  gh release download "$TAG" --repo "$REPO" --pattern "$UNSIGNED" --dir "$WORK_DIR/input" ||
    die 'draft has no unsigned app zip; re-run CI before signing again'
  mv "$WORK_DIR/input/$UNSIGNED" "$WORK_DIR/$UNSIGNED"
  WORKFLOW="$REPO/.github/workflows/release.yml"
  if ! gh attestation verify "$WORK_DIR/$UNSIGNED" \
      --repo "$REPO" --signer-workflow "$WORKFLOW" \
      --source-ref "refs/tags/$TAG" \
      --cert-identity "https://github.com/$WORKFLOW@refs/tags/$TAG" \
      --cert-oidc-issuer https://token.actions.githubusercontent.com \
      --deny-self-hosted-runners > "$WORK_DIR/attestation.log" 2>&1; then
    die 'artifact attestation verification failed; refusing to sign or upload'
  fi
  "$PY" - "$WORK_DIR/$UNSIGNED" <<'PYEOF'
import hashlib, sys
with open(sys.argv[1], "rb") as stream:
    print("Verified unsigned SHA-256: " + hashlib.file_digest(stream, "sha256").hexdigest())
PYEOF
  # Use the last (newest) matching identity, by SHA-1, never by an ambiguous display name.
  LOGIN_KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"
  security find-identity -v -p codesigning "$LOGIN_KEYCHAIN" > "$WORK_DIR/identities.txt"
  IDENTITY="$("$PY" - "$WORK_DIR/identities.txt" "${AGL_SIGN_ID:-}" <<'PYEOF'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
identities = re.findall(r'\b([0-9A-Fa-f]{40}) "Developer ID Application:[^"\n]+"', text)
override = sys.argv[2].upper()
if override and override not in [item.upper() for item in identities]:
    sys.exit("release-sign: AGL_SIGN_ID must be a Developer ID Application SHA-1 in the login keychain")
if not identities:
    sys.exit("release-sign: no Developer ID Application identity in the login keychain")
print(override or identities[-1])
PYEOF
)"
  xcrun notarytool history --keychain-profile "$PROFILE" >/dev/null
fi

"$PY" "$SCRIPT_DIR/release-bundle.py" check-zip "$WORK_DIR/$UNSIGNED"
mkdir "$WORK_DIR/unpacked"
ditto -x -k "$WORK_DIR/$UNSIGNED" "$WORK_DIR/unpacked"
APP="$WORK_DIR/unpacked/AI Guest List.app"
"$PY" - "$APP" "$TAG" "$SCRIPT_DIR/update-manifest.py" <<'PYEOF'
from pathlib import Path
import runpy, sys
app = Path(sys.argv[1])
if not app.is_dir() or list(app.parent.iterdir()) != [app]:
    sys.exit("release-sign: expected exactly one top-level AI Guest List.app")
runpy.run_path(sys.argv[3])["read_app"](app, sys.argv[2])
PYEOF

sign_bundle() {
  set -- "$SCRIPT_DIR/release-bundle.py" sign "$APP" --identity="$IDENTITY" \
    --entitlements "$SCRIPT_DIR/../app/entitlements.plist"
  if [[ "$DRY_RUN" == 1 ]]; then set -- "$@" --adhoc; fi
  "$PY" "$@"
}
verify_bundle() {
  local target="${1:-$APP}"
  codesign --verify --deep --strict --verbose=2 "$target"
  # Any updater MUST enforce update-requirement.txt with codesign -R / SecStaticCode,
  # in addition to manifest SHA-256. No in-app updater exists on this branch.
  # The public Team ID is mechanically required here, as in every signed app.
  local requirement
  requirement="$(cat "$SCRIPT_DIR/update-requirement.txt")"
  codesign --verify --deep --strict -R "=$requirement" "$target"
}
zip_app() {
  # Omit Finder resource-fork sidecars so the only zip root is AI Guest List.app.
  ditto -c -k --norsrc --keepParent "$APP" "$1"
}

sign_bundle
if [[ "$DRY_RUN" == 1 ]]; then
  # A successful structural check followed by a failed Developer ID requirement
  # is the expected result. Still return failure, so this can never reach upload.
  codesign --verify --deep --strict --verbose=2 "$APP"
  echo 'Ad-hoc signatures verified; the Developer ID requirement must now fail.'
  verify_bundle
  die 'unexpected: ad-hoc app satisfied the Developer ID requirement; refusing upload'
fi
verify_bundle
zip_app "$WORK_DIR/notarize.zip"
NOTARY_EXIT=0
xcrun notarytool submit "$WORK_DIR/notarize.zip" --keychain-profile "$PROFILE" \
  --wait --output-format json > "$WORK_DIR/notary.json" || NOTARY_EXIT=$?
# Even on nonzero exit notarytool can return JSON with a submission id and rejection.
"$PY" - "$WORK_DIR/notary.json" "$NOTARY_EXIT" "$PROFILE" <<'PYEOF'
import json, subprocess, sys
try:
    result = json.load(open(sys.argv[1], encoding="utf-8"))
except (ValueError, OSError) as exc:
    sys.exit(f"release-sign: notarytool returned no valid JSON: {exc}")
if sys.argv[2] != "0" or result.get("status") != "Accepted":
    print(f"release-sign: notarization was not Accepted: {result.get('status')}", file=sys.stderr)
    if result.get("id"):
        command = ["xcrun", "notarytool", "log", result["id"], "--keychain-profile", sys.argv[3]]
        print("Run: " + " ".join(command), file=sys.stderr)
        subprocess.run(command, check=False)
    sys.exit(1)
PYEOF
xcrun stapler staple "$APP"
spctl --assess --type execute -vv "$APP"
xcrun stapler validate "$APP"
verify_bundle
zip_app "$WORK_DIR/$ASSET"
"$PY" "$SCRIPT_DIR/release-bundle.py" check-zip "$WORK_DIR/$ASSET"
# Verify the bytes users will extract, including preservation of the stapled ticket.
mkdir "$WORK_DIR/final-check"
ditto -x -k "$WORK_DIR/$ASSET" "$WORK_DIR/final-check"
verify_bundle "$WORK_DIR/final-check/AI Guest List.app"
xcrun stapler validate "$WORK_DIR/final-check/AI Guest List.app"
"$PY" "$SCRIPT_DIR/update-manifest.py" --zip "$WORK_DIR/$ASSET" --app "$APP" \
  --tag "$TAG" --out "$WORK_DIR/latest.json"

# Re-check before mutating the draft; never overwrite an already-published release.
check_draft
gh release upload "$TAG" --repo "$REPO" --clobber "$WORK_DIR/$ASSET" "$WORK_DIR/latest.json"
gh release view "$TAG" --repo "$REPO" --json assets > "$WORK_DIR/assets.json"
if "$PY" - "$WORK_DIR/assets.json" "$UNSIGNED" <<'PYEOF'
import json, sys
sys.exit(0 if any(a["name"] == sys.argv[2] for a in json.load(open(sys.argv[1]))["assets"]) else 1)
PYEOF
then
  gh release delete-asset "$TAG" "$UNSIGNED" --repo "$REPO" --yes
fi
"$PY" - "$WORK_DIR/release.json" "$WORK_DIR/body.md" <<'PYEOF'
import json, sys
body = json.load(open(sys.argv[1], encoding="utf-8"))["body"] or ""
# softprops appends GitHub's generated notes after its body. Preserve that section
# verbatim; discard all previous signing/workaround instructions (also on re-runs).
markers = ("## What's Changed", "## What’s Changed", "## New Contributors", "**Full Changelog**")
starts = [body.index(marker) for marker in markers if marker in body]
notes = body[min(starts):] if starts else ""
if body.strip() and not starts and not body.startswith(("Draft awaiting local", "Developer ID signed")):
    sys.exit("release-sign: cannot identify generated notes; leaving draft for manual review")
intro = "Developer ID signed and notarized for macOS."
with open(sys.argv[2], "w", encoding="utf-8") as stream:
    stream.write(intro + ("\n\n" + notes if notes else "") + "\n")
PYEOF
gh release edit "$TAG" --repo "$REPO" --notes-file "$WORK_DIR/body.md"
if [[ "$NO_PUBLISH" == 0 ]]; then
  gh release edit "$TAG" --repo "$REPO" --draft=false --latest
  echo "Published $TAG (Developer ID signed, notarized and stapled)."
else
  echo "Signed draft $TAG is ready; --no-publish kept it unpublished. Test the signed app, then publish the draft manually."
fi
"$PY" - "$WORK_DIR/latest.json" <<'PYEOF'
import json, sys
m = json.load(open(sys.argv[1], encoding="utf-8"))
print(f"sha256: {m['sha256']}\nsize: {m['size']} bytes\nzip: {m['url']}\n"
      f"manifest: https://github.com/fheinfling/ai-guest-list/releases/download/v{m['version']}/latest.json\n"
      f"release: {m['notes_url']}")
PYEOF
