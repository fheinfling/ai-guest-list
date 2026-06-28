"""Command-line entrypoint for the ``acctsw`` engine.

This is the single backend used by both the CLI wrappers (``cx``/``cl``) and the menubar app.
Subcommands that touch usage / launching land in M3/M4; M2 wires up the credential + seat core.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import APP_NAME, TOOLS, __version__
from . import accounts as acct
from .context import Context
from .errors import AcctswError
from .switch import switch as do_switch

# Exit codes: 0 ok, 1 expected error, 2 argparse usage error, 3 not-implemented-yet.
EXIT_OK, EXIT_ERR, EXIT_NOIMPL = 0, 1, 3


def _add_tool_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("tool", choices=TOOLS, help="which agent tool")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="acctsw",
        description=f"{APP_NAME} — switch between Codex/Claude accounts on usage limits.",
    )
    parser.add_argument("--version", action="version", version=f"acctsw {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    ins = sub.add_parser("install", help="set up the engine (non-destructive, idempotent)")
    ins.add_argument("--dry-run", action="store_true", help="print actions without doing them")
    ins.add_argument("--no-register", action="store_true",
                     help="don't auto-register the currently logged-in account")
    uni = sub.add_parser("uninstall", help="remove the engine and restore original creds")
    uni.add_argument("--purge", action="store_true", help="also delete the store + keychain items")
    uni.add_argument("--dry-run", action="store_true", help="print actions without doing them")

    add = sub.add_parser("add", help="add (sign in) a seat from the live account")
    _add_tool_arg(add)
    add.add_argument("--name", help="friendly name for this seat")
    add.add_argument("--email", help="override detected email")
    rm = sub.add_parser("remove", help="remove (sign out) a seat")
    _add_tool_arg(rm)
    rm.add_argument("email", help="account email to remove")

    lst = sub.add_parser("list", help="list seats")
    lst.add_argument("--json", action="store_true", help="machine-readable output")
    st = sub.add_parser("status", help="show seats, active account, usage")
    st.add_argument("--json", action="store_true", help="machine-readable output")

    usage = sub.add_parser("usage", help="refresh/show live usage")
    usage.add_argument("action", choices=["refresh"], help="usage action")
    usage.add_argument("--tool", choices=TOOLS, help="limit to one tool")
    usage.add_argument("--json", action="store_true", help="machine-readable output")

    sw = sub.add_parser("switch", help="switch the active seat for a tool")
    _add_tool_arg(sw)
    sw.add_argument("email", help="account email to switch to")

    run = sub.add_parser("run", help="launch an agent with auto-switch + resume")
    run.add_argument("tool", choices=TOOLS)
    run.add_argument("args", nargs=argparse.REMAINDER, help="args passed to the agent")

    return parser


# --- command handlers -------------------------------------------------------------------------

def _cmd_add(ctx: Context, ns) -> int:
    state = ctx.load_state()
    seat = acct.add(ctx, state, ns.tool, name=ns.name, email=ns.email)
    print(f"✓ seat saved — {ns.tool}:{seat['email']} (now on the floor)")
    return EXIT_OK


def _cmd_remove(ctx: Context, ns) -> int:
    state = ctx.load_state()
    existed = acct.remove(ctx, state, ns.tool, ns.email)
    print(f"✓ waved goodbye to {ns.tool}:{ns.email}" if existed
          else f"(no seat {ns.tool}:{ns.email})")
    return EXIT_OK


def _cmd_list(ctx: Context, ns) -> int:
    state = ctx.load_state()
    data = {t: acct.list_seats(state, t) for t in TOOLS}
    if getattr(ns, "json", False):
        print(json.dumps(data, indent=2))
        return EXIT_OK
    for tool in TOOLS:
        seats = data[tool]
        print(f"{tool}:")
        if not seats:
            print("  (no seats yet)")
        for s in seats:
            mark = "● on the floor" if s["active"] else ("💤 resting" if s["limited"] else "○")
            print(f"  {mark}  {s['name']} <{s['email']}>")
    return EXIT_OK


def _cmd_status(ctx: Context, ns) -> int:
    state = ctx.load_state()
    data = acct.status(ctx, state)
    if getattr(ns, "json", False):
        print(json.dumps(data, indent=2))
        return EXIT_OK
    for tool in TOOLS:
        t = data["tools"][tool]
        print(f"{tool}: active={t['active'] or '—'}")
        for s in t["seats"]:
            tag = "active" if s["active"] else ("resting" if s["limited"] else "ready")
            print(f"  [{tag}] {s['email']}")
    return EXIT_OK


def _cmd_switch(ctx: Context, ns) -> int:
    state = ctx.load_state()
    do_switch(ctx, state, ns.tool, ns.email)
    print(f"✓ {ns.tool} is now on {ns.email}")
    return EXIT_OK


def _cmd_usage(ctx: Context, ns) -> int:
    from . import usage as usage_mod
    state = ctx.load_state()
    summary = usage_mod.refresh(ctx, state, tool=getattr(ns, "tool", None))
    if getattr(ns, "json", False):
        out = {"refresh": summary, "status": acct.status(ctx, state)}
        print(json.dumps(out, indent=2))
        return EXIT_OK
    for tool in (TOOLS if not ns.tool else [ns.tool]):
        print(f"{tool}:")
        for s in acct.list_seats(state, tool):
            u = s.get("usage") or {}
            wins = u.get("windows") or {}
            parts = []
            for key in ("5h", "weekly"):
                w = wins.get(key) or {}
                pct = w.get("used_pct")
                parts.append(f"{key} {pct:.0f}%" if isinstance(pct, (int, float)) else f"{key} —")
            tag = "resting" if s["limited"] else "ok"
            print(f"  {s['email']}: {', '.join(parts)}  [{tag}]")
    return EXIT_OK


def _cmd_run(ctx: Context, ns) -> int:
    from .launcher import run as launch

    def notify(msg: str) -> None:
        print(f"· {msg}", file=sys.stderr)

    args = list(ns.args or [])
    if args and args[0] == "--":  # argparse REMAINDER keeps a leading separator
        args = args[1:]
    return launch(ctx, ns.tool, args, notify=notify)


def _cmd_install(ctx: Context, ns) -> int:
    from . import install as inst
    plan = inst.install(ctx, dry_run=getattr(ns, "dry_run", False),
                        register=not getattr(ns, "no_register", False))
    for a in plan.actions:
        print(f"  {a}")
    print("✓ installed — add seats with `acctsw add codex` / `acctsw add claude`")
    return EXIT_OK


def _cmd_uninstall(ctx: Context, ns) -> int:
    from . import install as inst
    plan = inst.uninstall(ctx, purge=getattr(ns, "purge", False),
                          dry_run=getattr(ns, "dry_run", False))
    for a in plan.actions:
        print(f"  {a}")
    print("✓ uninstalled — originals restored")
    return EXIT_OK


def _not_impl(ctx: Context, ns) -> int:
    print(f"acctsw: '{ns.command}' arrives in a later milestone.", file=sys.stderr)
    return EXIT_NOIMPL


HANDLERS = {
    "add": _cmd_add,
    "remove": _cmd_remove,
    "list": _cmd_list,
    "status": _cmd_status,
    "switch": _cmd_switch,
    "usage": _cmd_usage,
    "run": _cmd_run,
    "install": _cmd_install,
    "uninstall": _cmd_uninstall,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)
    if not ns.command:
        parser.print_help()
        return EXIT_OK
    ctx = Context.default()
    ctx.ensure_dirs()
    handler = HANDLERS.get(ns.command, _not_impl)
    try:
        return handler(ctx, ns)
    except AcctswError as e:
        print(f"acctsw: {e}", file=sys.stderr)
        return EXIT_ERR


if __name__ == "__main__":
    raise SystemExit(main())
