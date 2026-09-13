#!/usr/bin/env bash
# Launch source through a development .app so notifications use the app's identity and icon.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"
"$PY" setup.py py2app --alias --dist-dir build/dev/dist --bdist-base build/dev/temp
exec open -n "build/dev/dist/AI Guest List.app"
