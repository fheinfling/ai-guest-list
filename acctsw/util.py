"""Small, dependency-free helpers shared across the engine.

Atomic writes + strict permissions are security-critical here (we move OAuth tokens around), so
they live in one place rather than being re-implemented per call site (per reviewer guidance).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def now() -> datetime:
    """Timezone-aware 'now' in UTC (single source so tests can monkeypatch)."""
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(s: str | None) -> datetime | None:
    """Parse an ISO timestamp, coercing naive values to UTC.

    Usage endpoints may return naive timestamps; we compare against an aware ``now()`` in
    ``selection``/usage, so a naive value would raise ``TypeError``. Coercing to UTC keeps all
    datetime comparisons safe.
    """
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Write ``data`` to ``path`` atomically with explicit permissions.

    Uses a temp file in the same directory + ``os.replace`` (atomic rename on the same
    filesystem). ``mode`` is enforced with ``os.fchmod`` so it is not subject to the umask.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def write_json(path: Path, obj, *, mode: int = 0o600) -> None:
    atomic_write_text(path, json.dumps(obj, indent=2, sort_keys=True) + "\n", mode=mode)


def chmod_dir(path: Path, mode: int = 0o700) -> None:
    """Enforce a directory mode regardless of umask."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def jwt_payload(token: str) -> dict:
    """Decode the (unverified) payload of a JWT. Used only to read our own id_token's email.

    We never trust this for auth decisions — only to label seats with their account email.
    """
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}
