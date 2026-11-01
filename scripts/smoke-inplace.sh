#!/usr/bin/env bash
# Opt-in smoke test of in-place seat switching, transport v2 (HTTPS_PROXY interception), with the
# REAL claude binary against tests/fixtures/fake_anthropic.py. Nothing leaves the machine:
#
# - claude runs under sandbox-exec with outbound network limited to loopback;
# - its HTTPS_PROXY is the seat proxy, which decrypts api.anthropic.com with a per-run SessionCA
#   and forwards it to the fake API on 127.0.0.1;
# - every other host claude CONNECTs to is blind-tunnelled into a local sinkhole proxy that logs
#   the target and refuses it (403);
# - all tokens are fake, CLAUDE_CONFIG_DIR is a temp dir, no Keychain item or state.json is touched.
#
#   scripts/smoke-inplace.sh [OUTDIR]        (PY=..., CLAUDE_BIN=... to override)
#   scripts/smoke-inplace.sh --self-test     (a Python stand-in client instead of claude, no
#                                             sandbox-exec: checks the harness itself)
#
# Experiments:
#   a-intercept   claude -p succeeds through the intercepted tunnel; the fake API sees seat B's
#                 injected bearer, never claude's own token
#   b-limit       the first turn runs a tool on seat A, then A hits its limit: the proxy replays on
#                 seat B and the session continues to a normal end
#   c-tls-reject  claude without the session CA in its trust: on_tls_reject fires (once)
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PY="${PY:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"
SELF_TEST=0
if [ "${1:-}" = "--self-test" ]; then SELF_TEST=1; shift; fi
OUT="${1:-$(mktemp -d "${TMPDIR:-/tmp}/smoke-inplace.XXXXXX")}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd -P)"
if [ "$SELF_TEST" = 1 ]; then
  CLAUDE="-"
else
  CLAUDE="${CLAUDE_BIN:-$(command -v claude || true)}"
  command -v sandbox-exec >/dev/null || { echo "needs macOS sandbox-exec" >&2; exit 2; }
  [ -x "$CLAUDE" ] || { echo "claude binary not found (set CLAUDE_BIN)" >&2; exit 2; }
  echo "claude: $("$CLAUDE" --version)"
fi
echo "out: $OUT"

cat > "$OUT/net.sb" <<'EOF'
(version 1)
(allow default)
(deny network-outbound)
(allow network-outbound (remote ip "localhost:*"))
(allow network-outbound (remote unix-socket))
(deny network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))
EOF

exec "$PY" - "$ROOT" "$OUT" "$CLAUDE" <<'PYEOF'
import json, os, secrets, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

root, out, claude = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
sys.path[:0] = [str(root), str(root / "tests" / "fixtures")]
import fake_anthropic as FA
from acctsw.seatproxy import Seat, SeatProxy
from acctsw.sessionca import SessionCA

# The proxy's own upstream decisions read the launcher-side environment: the fake API on 127.0.0.1
# is reached directly, every other host goes to the sinkhole.
for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(var, None)
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

A = Seat("a@smoke.invalid", "sk-ant-oat01-smoke-A-" + secrets.token_hex(12))
B = Seat("b@smoke.invalid", "sk-ant-oat01-smoke-B-" + secrets.token_hex(12))
CLIENT = "sk-ant-oat01-smoke-client-" + secrets.token_hex(12)
NAMES = {FA.tok(A.token): "A", FA.tok(B.token): "B", FA.tok(CLIENT): "client"}

STAND_IN = r'''
import base64, http.client, json, os, ssl, sys
from urllib.parse import unquote, urlsplit
def result(ok, text):
    print(json.dumps({"type": "result", "is_error": not ok, "result": text}))
    sys.exit(0 if ok else 1)
u = urlsplit(os.environ["HTTPS_PROXY"])
ctx = ssl.create_default_context(cafile=os.environ.get("NODE_EXTRA_CA_CERTS"))
conn = http.client.HTTPSConnection(u.hostname, u.port, context=ctx, timeout=30)
cred = base64.b64encode(f"{unquote(u.username)}:{unquote(u.password)}".encode()).decode()
conn.set_tunnel("api.anthropic.com", 443, headers={"Proxy-Authorization": "Basic " + cred})
headers = {"authorization": "Bearer " + os.environ["CLAUDE_CODE_OAUTH_TOKEN"],
           "anthropic-beta": "oauth-2025-04-20", "content-type": "application/json"}
msgs = [{"role": "user", "content": sys.argv[1]}]
try:
    for _ in range(4):
        conn.request("POST", "/v1/messages?beta=true", json.dumps(
            {"model": "m", "max_tokens": 5, "messages": msgs}), headers)
        resp = conn.getresponse()
        body = json.loads(resp.read() or b"{}")
        if resp.status != 200:
            result(False, f"API Error: {resp.status} {body}")
        if body.get("stop_reason") != "tool_use":
            result(True, " ".join(b.get("text", "") for b in body["content"]))
        tool = body["content"][0]
        msgs += [{"role": "assistant", "content": body["content"]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool["id"],
                                               "content": "ok"}]}]
    result(False, "too many turns")
except (ssl.SSLError, OSError) as e:
    result(False, f"connection error: {e}")
'''


class Sinkhole:
    """The proxy's upstream_proxy: logs every CONNECT target and refuses it, so blind tunnels
    never reach the internet."""

    def __init__(self):
        self.targets: list[str] = []
        sink = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_CONNECT(self):
                sink.targets.append(self.path)
                self.send_response(403, "smoke sinkhole: no egress")
                self.send_header("content-length", "0")
                self.send_header("connection", "close")
                self.end_headers()
                self.close_connection = True

            do_GET = do_POST = do_PUT = do_HEAD = do_DELETE = do_CONNECT

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class SmokeProxy(SeatProxy):
    # Decrypt api.anthropic.com:443 as in production, but forward to the fake API on 127.0.0.1.
    _authority = "api.anthropic.com:443"


results = []


def run(name, prompt, *, bind, rules, answer, trust_ca, expect):
    d = out / name
    (d / "cfg").mkdir(parents=True, exist_ok=True)
    (d / "work").mkdir(exist_ok=True)
    fake = FA.FakeAnthropic(d / "fake.jsonl")
    fake.update(rules)
    srv = FA.serve(fake)
    sink = Sinkhole()
    ca = SessionCA(tmp_root=d)
    hits, rejects = [], []
    events: dict[str, int] = {}
    log = (d / "proxy.jsonl").open("w")

    def decide(hit):
        hits.append(hit)
        return answer

    def logger(event, **fields):
        events[event] = events.get(event, 0) + 1
        log.write(json.dumps({"event": event, **fields}) + "\n")
        log.flush()

    proxy = SmokeProxy(decide, upstream=f"http://127.0.0.1:{srv.server_address[1]}", ca=ca,
                       upstream_proxy=sink.url, on_tls_reject=rejects.append, log=logger)
    proxy.bind(bind)
    url = proxy.start()
    env = {"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "TERM": "dumb",
           "USER": "seatproxy-smoke", "CLAUDE_CONFIG_DIR": str(d / "cfg"),
           "CLAUDE_CODE_OAUTH_TOKEN": CLIENT, "HTTPS_PROXY": url, "https_proxy": url,
           "HTTP_PROXY": url, "http_proxy": url, "NO_PROXY": "127.0.0.1,localhost",
           "no_proxy": "127.0.0.1,localhost"}
    if trust_ca:
        env.update(ca.bundle_paths(env))    # what SeatSwap.env() adds
    if claude == "-":
        cmd = [sys.executable, "-c", STAND_IN, prompt]
    else:
        # Already inside a sandbox (e.g. a sandboxed agent shell)? macOS refuses a nested
        # sandbox_apply, so SMOKE_NO_SANDBOX=1 relies on the enclosing sandbox's network rules.
        wrap = [] if os.environ.get("SMOKE_NO_SANDBOX") == "1" else ["sandbox-exec", "-f", str(out / "net.sb")]
        cmd = wrap + [claude, "-p", prompt,
                      "--output-format", "stream-json", "--verbose", "--allowedTools", "Bash"]
    with (d / "claude.jsonl").open("w") as so, (d / "claude.err").open("w") as se:
        try:
            rc = subprocess.run(cmd, cwd=d / "work", env=env, stdin=subprocess.DEVNULL, stdout=so,
                                stderr=se, timeout=180).returncode
        except subprocess.TimeoutExpired:
            rc = "timeout"
    time.sleep(0.5)    # let the proxy finish logging the last exchange
    proxy.stop()
    ca.close()
    sink.close()
    srv.shutdown()
    srv.server_close()
    log.close()
    lines = [x for x in (d / "claude.jsonl").read_text().splitlines() if x.startswith("{")]
    events_out = [json.loads(x) for x in lines]
    result = next((e for e in reversed(events_out) if e.get("type") == "result"), {})
    upstream = [(NAMES.get(r["bearer"], r["bearer"]), r["status"]) for r in fake.requests
                if r["method"] == "POST" and r["path"].split("?")[0] == "/v1/messages"]
    secrets_ = (A.token, B.token, CLIENT, proxy._secret)
    leaked = [n for n, s in zip(("A", "B", "client", "proxy-secret"), secrets_)
              if s in (d / "proxy.jsonl").read_text()]
    facts = {"rc": rc, "is_error": result.get("is_error"), "result": (result.get("result") or "")[:90],
             "upstream": upstream, "hits": [(h.kind, h.seat) for h in hits],
             "tls_rejects": len(rejects), "intercepts": events.get("intercept", 0),
             "proxy_auth_failed": events.get("proxy_auth_failed", 0),
             "sinkholed": sorted(set(sink.targets)), "messages_seen": proxy.messages_seen()}
    problems = [k for k, check in expect.items() if not check(facts)]
    problems += ["token or secret in proxy log"] if leaked else []
    results.append((name, problems))
    print(f"--- {name}: {'PASS' if not problems else 'FAIL: ' + '; '.join(problems)}")
    for k, v in facts.items():
        print(f"    {k}: {v}")


run("a-intercept", "say hi", bind=B, rules={}, answer=None, trust_ca=True, expect={
    "claude succeeded": lambda f: f["rc"] == 0 and not f["is_error"],
    "went through the intercepted tunnel": lambda f: f["intercepts"] >= 1 and f["proxy_auth_failed"] == 0,
    "fake API saw seat B's injected bearer": lambda f: f["upstream"] and all(s == "B" for s, _ in f["upstream"]),
    "answered by B": lambda f: FA.tok(B.token) in f["result"],
    "no tls reject": lambda f: f["tls_rejects"] == 0})
run("b-limit", "run the tool", bind=A, answer=B, trust_ca=True,
    rules={"tool_first": True, "next": [200], "status_by_token": {FA.tok(A.token): 429}}, expect={
    "claude succeeded": lambda f: f["rc"] == 0 and not f["is_error"],
    "A, A 429, then B": lambda f: f["upstream"][:3] == [("A", 200), ("A", 429), ("B", 200)],
    "one limit hit on A": lambda f: f["hits"] == [("limit", A.email)],
    "session continued on B": lambda f: FA.tok(B.token) in f["result"]})
run("c-tls-reject", "say hi", bind=B, rules={}, answer=None, trust_ca=False, expect={
    "on_tls_reject fired once": lambda f: f["tls_rejects"] == 1,
    "nothing reached the fake API": lambda f: f["upstream"] == [],
    "claude failed": lambda f: f["rc"] != 0 or f["is_error"]})

print()
for name, problems in results:
    print(f"{'PASS' if not problems else 'FAIL'}  {name}")
sys.exit(1 if any(p for _, p in results) else 0)
PYEOF
