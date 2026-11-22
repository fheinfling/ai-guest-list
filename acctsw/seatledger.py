"""Best-effort, append-only session-to-seat bindings. No credentials live here."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path


def record(data_dir: Path, tool: str, email: str, *, pid: int,
           thread: str | None = None, event: str = "bind") -> None:
    row = {"v": 1, "at": datetime.now().astimezone().isoformat(), "tool": tool,
           "email": email, "pid": pid, "thread": thread, "event": event}
    try:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        fd = os.open(Path(data_dir) / "seat-ledger.jsonl",
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, (json.dumps(row, separators=(",", ":")) + "\n").encode())
        finally:
            os.close(fd)
    except OSError:
        # Exception messages can contain private paths; log only a fixed diagnostic.
        logging.getLogger(__name__).warning("Could not append seat ledger")


def read(data_dir: Path) -> list[dict]:
    rows = []
    try:
        with open(Path(data_dir) / "seat-ledger.jsonl", "rb") as stream:
            for line in stream:
                if not line.endswith(b"\n"):
                    continue
                try:
                    row = json.loads(line)
                    if (isinstance(row, dict) and row.get("v") == 1
                            and row.get("tool") in ("claude", "codex")
                            and isinstance(row.get("at"), str)
                            and isinstance(row.get("email"), str)
                            and row.get("event") in ("bind", "end")):
                        rows.append(row)
                except (ValueError, UnicodeError):
                    continue
    except OSError:
        pass
    return rows
