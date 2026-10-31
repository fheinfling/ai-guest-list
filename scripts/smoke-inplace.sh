#!/usr/bin/env bash
# Opt-in smoke test of acctsw/seatproxy.py with the REAL claude binary against tests/fixtures/
# fake_anthropic.py. Nothing leaves the machine: claude runs under sandbox-exec with outbound network
# limited to loopback, HTTPS_PROXY points at a closed local port, every token is fake and
# CLAUDE_CONFIG_DIR is a temp dir. No Keychain item and no state.json is touched.
#
#   scripts/smoke-inplace.sh [OUTDIR]        (PY=..., CLAUDE_BIN=... to override)
#
# Scenarios:
#   failover     the first turn runs a tool on seat A, then A hits its limit mid-session: the proxy
#                asks decide, replays on seat B and claude finishes without noticing
#   passthrough  A is limited and decide has no seat: claude shows its own limit message
#   failopen     the bound seat's token is dead (401) and decide has no seat: the request goes out
#                on claude's own login and succeeds
#   home401      the client's own login gets a 401 while home is bound: passed through, no decide
#   stream       SSE deltas reach claude incrementally through the proxy
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PY="${PY:-$ROOT/.venv/bin/python}"
CLAUDE="${CLAUDE_BIN:-$(command -v claude)}"
OUT="${1:-$(mktemp -d "${TMPDIR:-/tmp}/smoke-inplace.XXXXXX")}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd -P)"
command -v sandbox-exec >/dev/null || { echo "needs macOS sandbox-exec" >&2; exit 2; }
[ -x "$CLAUDE" ] || { echo "claude binary not found (set CLAUDE_BIN)" >&2; exit 2; }

cat > "$OUT/net.sb" <<'EOF'
(version 1)
(allow default)
(deny network-outbound)
(allow network-outbound (remote ip "localhost:*"))
(allow network-outbound (remote unix-socket))
(deny network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))
EOF

echo "claude: $("$CLAUDE" --version)   out: $OUT"
exec "$PY" - "$ROOT" "$OUT" "$CLAUDE" <<'PYEOF'
import json, os, secrets, subprocess, sys, time
from pathlib import Path

root, out, claude = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
sys.path[:0] = [str(root), str(root / "tests" / "fixtures")]
import fake_anthropic as FA
from acctsw.seatproxy import Seat, SeatProxy, tok

for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(var, None)
A = Seat("a@smoke.invalid", "sk-ant-oat01-smoke-A-" + secrets.token_hex(12))
B = Seat("b@smoke.invalid", "sk-ant-oat01-smoke-B-" + secrets.token_hex(12))
HOME = Seat("home@smoke.invalid", None)
CLIENT = "sk-ant-oat01-smoke-client-" + secrets.token_hex(12)
NAMES = {tok(A.token): "A", tok(B.token): "B", tok(CLIENT): "client"}
failed = []


def run(name, prompt, *, bind, rules, answer, expect):
    d = out / name
    (d / "cfg").mkdir(parents=True, exist_ok=True)
    (d / "work").mkdir(exist_ok=True)
    fake = FA.FakeAnthropic(d / "fake.jsonl")
    fake.update(rules)
    srv = FA.serve(fake)
    hits, logs = [], []
    log = (d / "proxy.jsonl").open("w")

    def decide(hit):
        hits.append(hit)
        return answer

    def logger(event, **fields):
        log.write(json.dumps({"event": event, **fields}) + "\n")
        log.flush()

    proxy = SeatProxy(decide, upstream=f"http://127.0.0.1:{srv.server_address[1]}", log=logger)
    proxy.bind(bind)
    base = proxy.start()
    env = {"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "TERM": "dumb",
           "USER": "seatproxy-smoke", "CLAUDE_CONFIG_DIR": str(d / "cfg"), "ANTHROPIC_BASE_URL": base,
           "CLAUDE_CODE_OAUTH_TOKEN": CLIENT, "HTTPS_PROXY": "http://127.0.0.1:9",
           "HTTP_PROXY": "http://127.0.0.1:9", "NO_PROXY": "127.0.0.1,localhost"}
    t0 = time.monotonic()
    with (d / "claude.jsonl").open("w") as so, (d / "claude.err").open("w") as se:
        rc = subprocess.run(["sandbox-exec", "-f", str(out / "net.sb"), claude, "-p", prompt,
                             "--output-format", "stream-json", "--verbose", "--allowedTools", "Bash"],
                            cwd=d / "work", env=env, stdin=subprocess.DEVNULL, stdout=so, stderr=se,
                            timeout=180).returncode
    proxy.stop()
    srv.shutdown()
    srv.server_close()
    log.close()
    events = [json.loads(x) for x in (d / "claude.jsonl").read_text().splitlines() if x.startswith("{")]
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    seq = [(NAMES.get(r["bearer"], r["bearer"]), r["status"]) for r in fake.requests
           if r["path"].startswith("/v1/messages") and r["method"] == "POST"]
    text = (d / "proxy.jsonl").read_text()
    leaked = [n for n, s in (("A", A.token), ("B", B.token), ("client", CLIENT)) if s in text]
    facts = {"rc": rc, "is_error": result.get("is_error"), "result": (result.get("result") or "")[:90],
             "upstream": seq, "hits": [(h.kind, h.seat) for h in hits], "leaked": leaked,
             "secs": round(time.monotonic() - t0, 1)}
    problems = [k for k, v in expect.items() if not v(facts)] + (["token in proxy log"] if leaked else [])
    print(f"--- {name}: {'ok' if not problems else 'FAIL ' + ', '.join(problems)}")
    for k, v in facts.items():
        print(f"    {k}: {v}")
    if problems:
        failed.append(name)


limited_after_first = {"tool_first": True, "next": [200], "status_by_token": {tok(A.token): 429}}
run("failover", "run the tool", bind=A, rules=limited_after_first, answer=B, expect={
    "exit 0": lambda f: f["rc"] == 0 and not f["is_error"],
    "A, A 429, B": lambda f: f["upstream"][:3] == [("A", 200), ("A", 429), ("B", 200)],
    "one limit hit on A": lambda f: f["hits"] == [("limit", A.email)],
    "answered by B": lambda f: tok(B.token) in f["result"]})
run("passthrough", "say hi", bind=A, rules={"status_by_token": {tok(A.token): 429}}, answer=None, expect={
    "claude saw the limit": lambda f: f["is_error"] and "limit" in f["result"].lower(),
    "only A": lambda f: {s for s, _ in f["upstream"]} == {"A"}})
run("failopen", "say hi", bind=B, rules={"status_by_token": {tok(B.token): 401}}, answer=None, expect={
    "exit 0": lambda f: f["rc"] == 0 and not f["is_error"],
    "B 401 then client": lambda f: f["upstream"][:2] == [("B", 401), ("client", 200)],
    "one auth hit": lambda f: f["hits"][:1] == [("auth", B.email)]})
run("home401", "say hi", bind=HOME, rules={"status_by_token": {tok(CLIENT): 401}}, answer=B, expect={
    "no decide": lambda f: f["hits"] == [],
    "only the client login": lambda f: {s for s, _ in f["upstream"]} == {"client"}})
run("stream", "say hi", bind=B, rules={"chunk_delay": 0.4}, answer=None, expect={
    "exit 0": lambda f: f["rc"] == 0 and not f["is_error"],
    "on B": lambda f: {s for s, _ in f["upstream"]} == {"B"},
    "took the stream's time": lambda f: f["secs"] >= 2})
print("\nsmoke-inplace:", "FAILED " + ", ".join(failed) if failed else "all ok")
sys.exit(1 if failed else 0)
PYEOF
