"""Opt-in, paid proof of a seat's generated Responses configuration.

This lives outside keyseats: CRUD and free validation must never acquire a paid side effect.
Only the explicit bridge/CLI action calls prove. The provider block and child environment come
from keyhome exactly as on launch; only this thread's permissions, tools and persistence differ.
No provider prose, output, raw error or exception text crosses the result boundary.
"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Any, Callable

from . import appserver, keyhome, keyseats, providers
from .context import Context
from .util import iso, now

TIMEOUT = 30
PROMPT = "Reply only OK. Do not use tools."


def _verdict(provider: providers.Provider, info: Any) -> tuple[str, str]:
    category = appserver.classify_codex_error(provider, info)
    if category != "unknown":
        return "refused", category
    if info == "badRequest":
        return "incompatible", "request_rejected"
    if isinstance(info, dict) and len(info) == 1:
        for variant in ("httpConnectionFailed", "responseStreamConnectionFailed",
                        "responseTooManyFailedAttempts"):
            detail = info.get(variant)
            status = detail.get("httpStatusCode") if isinstance(detail, dict) else None
            if type(status) is not int:
                continue
            if status in (400, 404, 405, 415, 422):
                return "incompatible", "request_rejected"
            if status in (401, 402, 403, 429):
                # A typed HTTP refusal is evidence of a refusal, but without a documented
                # provider discriminator its taxonomy stays unknown (especially a bare 429).
                return "refused", category
    return "inconclusive", "no_verdict"


def _thread_options(cwd: Path, scratch: Path) -> dict:
    # Read-only alone still allows commands to read files. Disable command/image/browser/MCP
    # capabilities as well, and leave ALL server requests to AppServer's default refusal.
    # These are thread-local overrides, never edits to the seat's real provider configuration.
    return {"cwd": str(cwd), "ephemeral": True, "approvalPolicy": "never",
            "sandbox": "read-only", "baseInstructions": PROMPT,
            "config": {
                "features.shell_tool": False, "features.unified_exec": False,
                "features.apply_patch_freeform": False, "tools.view_image": False,
                "features.multi_agent": False, "features.js_repl": False,
                "features.browser_use": False, "features.computer_use": False,
                "features.apps": False, "features.plugins": False,
                "web_search": "disabled", "mcp_servers": {},
                "history.persistence": "none", "log_dir": str(scratch),
                "sqlite_home": str(scratch), "project_doc_max_bytes": 0,
            }}


def _run(ctx: Context, runtime: keyhome.KeyHome, provider: providers.Provider,
         env: dict[str, str], *, spawn: appserver.Spawn,
         kill_group: Callable[[int], None], timeout: float) -> tuple[str, str | None]:
    with TemporaryDirectory(prefix="acctsw-prove-") as directory:
        scratch = Path(directory)
        cwd = scratch / "empty"
        cwd.mkdir()
        # Keep even the child's diagnostic files out of the seat home. Responses may contain
        # echoed credentials; no child output is logged, and the ephemeral scratch is removed.
        env = keyhome.ChildEnv(env)
        env["RUST_LOG"] = "off"
        deadline = monotonic() + timeout
        with appserver.AppServer(ctx, provider, spawn=spawn, env=env, cwd=cwd,
                                 timeout=timeout, lifetime=timeout,
                                 kill_group=kill_group,
                                 config_overrides={"log_dir": str(scratch),
                                                   "sqlite_home": str(scratch)}) as server:
            def remaining() -> float:
                seconds = deadline - monotonic()
                if seconds <= 0 or server.expired:
                    raise TimeoutError
                server.timeout = seconds
                return seconds

            remaining()
            server.initialize()
            remaining()
            started = server.request("thread/start", _thread_options(cwd, scratch))
            # A managed override or model fallback must not certify a different runtime.
            # Fail before inference if the server did not accept the probe's isolation.
            if (started.get("model") != runtime.model
                    or started.get("modelProvider") != runtime.model_provider
                    or Path(started.get("cwd", "")).resolve() != cwd.resolve()
                    or started.get("approvalPolicy") != "never"
                    or started.get("sandbox", {}).get("type") != "readOnly"):
                return "inconclusive", "runtime_mismatch"
            thread = started["thread"]["id"]
            remaining()
            try:
                turn = server.start_turn(thread, PROMPT)["turn"]["id"]
                while True:
                    note = server.next_event(timeout=remaining())
                    if note.params.get("threadId") != thread:
                        continue
                    if note.method == "error" and note.params.get("turnId") == turn:
                        if isinstance(note.signal, appserver.ErrorSignal):
                            verdict = _verdict(provider, note.signal.info)
                            if verdict[0] != "inconclusive":
                                return verdict
                    if note.method == "turn/completed":
                        completed = note.params.get("turn", {})
                        if completed.get("id") != turn:
                            continue
                        if completed.get("status") == "completed" and not completed.get("error"):
                            return "proven", None
                        if isinstance(note.signal, appserver.ErrorSignal):
                            return _verdict(provider, note.signal.info)
                        return "inconclusive", "no_verdict"
            except appserver.RpcError as exc:
                # Only turn/start can carry an inference verdict. A bad thread/start or
                # initialize parameter is a local protocol failure, not endpoint evidence.
                data = exc.error.get("data") if isinstance(exc.error, dict) else None
                info = data.get("codexErrorInfo") if isinstance(data, dict) else None
                return _verdict(provider, info)


def prove(ctx: Context, id: str, *, spawn: appserver.Spawn | None = None,
          kill_group: Callable[[int], None] | None = None,
          timeout: float = TIMEOUT) -> dict[str, Any]:
    """Spend one minimal turn, then persist only a bounded verdict, timestamp and model.

    Only a completed turn changes responses_verified. Incompatibility is recorded separately
    and takes precedence when displaying the seat. Concurrent edits/removal/key
    rotation invalidate the result, so a stale probe cannot certify a different configuration.
    """
    seat = keyseats.get(ctx, id)
    if seat is not None and seat.get("harness") == "claude":
        # A local refusal, not an endpoint verdict: no credential read, launch or state write.
        return {"outcome": "refused", "error": "unsupported_harness",
                "reason": "Responses checks use codex app-server, which cannot exercise a "
                          "Messages endpoint for a Claude Code seat"}
    if seat is None or seat.get("harness") != "codex":
        raise ValueError("Responses checks require an existing Codex key seat")
    runtime = keyhome.prepare(ctx, id)
    env = keyhome.build_env(ctx, runtime)
    secret = env[runtime.env_key]
    provider = providers.get_provider(seat["provider"], region=seat.get("region"),
                                      base_url=seat.get("base_url"))
    checked_at = iso(now())
    try:
        outcome, error = _run(ctx, runtime, provider, env, spawn=spawn or appserver._spawn,
                              kill_group=kill_group or appserver._kill_group, timeout=timeout)
    except TimeoutError:
        outcome, error = "inconclusive", "timeout"
    except Exception:
        outcome, error = "inconclusive", "no_verdict"
    result = {"outcome": outcome, "checked_at": checked_at, "model": runtime.model,
              "error": error}
    with ctx.locked():
        state = ctx.load_state()
        current = state.data["keys"].get(id)
        current_secret = ctx.keychain.get(ctx.keychain_service, ctx.snapshot_key("key", id))
        if current != seat or current_secret != secret or runtime.model != seat["model"]:
            raise ValueError("Key seat changed during endpoint check")
        current["last_proof"] = result
        if outcome == "proven":
            current["responses_verified"] = True
        state.save()
    return dict(result)


def describe(result: dict) -> str:
    """Fixed copy only: error data is never interpolated into diagnostics."""
    outcome = result["outcome"]
    if outcome == "proven":
        return "proven — a Responses turn completed"
    if outcome == "incompatible":
        return "incompatible — this endpoint does not support this Responses request; this seat will not work"
    if outcome == "refused":
        if result.get("error") == "unsupported_harness":
            return ("refused — Responses checks use codex app-server, which cannot exercise "
                    "a Messages endpoint for a Claude Code seat")
        reason = {"invalid_key": "authentication", "insufficient_quota": "quota",
                  "rate_limited": "rate limit"}.get(result.get("error"), "access or billing")
        return f"refused — provider rejected the request ({reason}); Responses support is undetermined"
    reason = "check timed out" if result.get("error") == "timeout" else "no completed turn or definitive provider reply"
    return f"inconclusive — {reason}; Responses support is undetermined"
