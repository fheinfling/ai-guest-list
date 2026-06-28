#!/usr/bin/env bash
# Launch the "ai guest list" menubar app from the repo (dev mode).
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -m app.menubar
