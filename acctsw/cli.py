"""Command-line entrypoint for the ``acctsw`` engine.

This is the single backend used by both the CLI wrappers (``cx``/``cl``) and the menubar app.
Milestone 1 ships the dispatch skeleton; subcommands are implemented in later milestones.
"""
from __future__ import annotations

import argparse
import sys

from . import APP_NAME, TOOLS, __version__


def _add_tool_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("tool", choices=TOOLS, help="which agent tool")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="acctsw",
        description=f"{APP_NAME} — switch between Codex/Claude accounts on usage limits.",
    )
    parser.add_argument("--version", action="version", version=f"acctsw {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser("install", help="set up the engine (non-destructive, idempotent)")
    uni = sub.add_parser("uninstall", help="remove the engine and restore original creds")
    uni.add_argument("--purge", action="store_true", help="also delete the store + keychain items")
    uni.add_argument("--dry-run", action="store_true", help="print actions without doing them")

    add = sub.add_parser("add", help="add (sign in) a seat")
    _add_tool_arg(add)
    rm = sub.add_parser("remove", help="remove (sign out) a seat")
    _add_tool_arg(rm)
    rm.add_argument("email", help="account email to remove")

    sub.add_parser("list", help="list seats")
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)
    if not ns.command:
        parser.print_help()
        return 0
    # Subcommands land here as they are implemented (M2+).
    print(f"acctsw: '{ns.command}' is not implemented yet (scaffold).", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
