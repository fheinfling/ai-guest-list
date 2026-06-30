#!/usr/bin/env bash
# Safe, non-destructive end-to-end verification: tests + dry-run install + read-only live probes.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"

echo "== unit + UI tests =="
bash scripts/smoke.sh

echo; echo "== install dry-run (changes nothing) =="
$PY -m acctsw install --dry-run

echo; echo "== read-only live identity + usage probe =="
$PY - <<'PYEOF'
from acctsw.context import Context
from acctsw import usage as U, identity
ctx = Context.default()
print("codex email:", identity.live_email(ctx, "codex"))
print("claude email:", identity.live_email(ctx, "claude"))
blob = ctx.cred["codex"].get_live()
if blob:
    tok, acc = U.codex_token_account(blob)
    u = U.fetch_codex(tok, acc)
    print("codex usage ok:", u.ok, {k: v.used_pct for k, v in u.windows.items()})
cblob = ctx.cred["claude"].get_live()
if cblob:
    cu = U.fetch_claude(U.claude_token(cblob), user_agent=U.claude_user_agent(ctx.claude_bin))
    print("claude usage ok:", cu.ok, {k: v.used_pct for k, v in cu.windows.items()})
PYEOF
echo; echo "✓ verify ok (no changes made)"
