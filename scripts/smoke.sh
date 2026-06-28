#!/usr/bin/env bash
# Minimal smoke harness: lint-free import + unit tests. Run from repo root.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"
echo "→ python: $($PY --version)"
echo "→ import check"
$PY -c "import acctsw; print('acctsw', acctsw.__version__)"
echo "→ pytest"
$PY -m pytest -q
echo "✓ smoke ok"
