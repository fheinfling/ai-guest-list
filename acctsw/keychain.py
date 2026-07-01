"""macOS Keychain access, behind an interface so tests can swap in an in-memory fake.

We deliberately depend on the ``security`` CLI rather than a third-party library: no extra deps,
and it is exactly what the official tools and the reference apps use.
"""
from __future__ import annotations

import base64
import binascii
import subprocess
from typing import Protocol

from .errors import AcctswError


def _encode(secret: str) -> str:
    """base64 so the keychain only ever holds single-line printable ASCII.

    `security find-generic-password -w` returns multi-line/binary passwords as a HEX string; codex's
    pretty-printed auth.json has newlines, so storing it raw round-trips to hex and corrupts the
    blob. base64 (no newlines) sidesteps that entirely.
    """
    return base64.b64encode(secret.encode("utf-8")).decode("ascii")


def _decode(stored: str) -> str:
    """Decode a value read back from the keychain, tolerating legacy/corrupted formats:
    base64 (current), hex (old security round-trip corruption), or raw (legacy plain)."""
    s = stored.strip()
    try:
        return base64.b64decode(s, validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        pass
    # security may have returned the password hex-encoded
    if s and len(s) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in s):
        try:
            return binascii.unhexlify(s).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            pass
    return stored  # legacy plain


class KeychainBackend(Protocol):
    """Minimal generic-password store keyed by (service, account)."""

    def get(self, service: str, account: str) -> str | None: ...
    def set(self, service: str, account: str, secret: str) -> None: ...
    def delete(self, service: str, account: str) -> bool: ...


class SecurityKeychain:
    """Real backend using `/usr/bin/security` (macOS login keychain)."""

    def __init__(self, security_path: str = "/usr/bin/security") -> None:
        self._security = security_path

    def get(self, service: str, account: str) -> str | None:
        proc = subprocess.run(
            [self._security, "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            return None
        # `-w` prints the password with a trailing newline.
        return _decode(proc.stdout.rstrip("\n"))

    def set(self, service: str, account: str, secret: str) -> None:
        # Pass the (base64) blob inline via `-w <value>`. The tempting alternative — `-w` with no
        # value, feeding the secret over stdin — is BROKEN for real credentials: `security` reads
        # that prompt with readpassphrase(), which silently TRUNCATES to 128 bytes (returncode 0,
        # no error) and, when a child TUI holds the tty in raw mode, can block waiting on /dev/tty.
        # A base64 OAuth blob is far larger than 128 bytes, so stdin stored a corrupt seat. Inline
        # has no length cap. The `ps` exposure it was meant to avoid is moot: a same-user process
        # that could read our argv can just call `security find-generic-password` and read the item
        # directly. -U updates the item if it already exists.
        blob = _encode(secret)
        proc = subprocess.run(
            [self._security, "add-generic-password", "-U", "-s", service, "-a", account, "-w", blob],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise KeychainError(f"keychain set failed for {service}:{account}: {proc.stderr.strip()}")

    def delete(self, service: str, account: str) -> bool:
        proc = subprocess.run(
            [self._security, "delete-generic-password", "-s", service, "-a", account],
            capture_output=True, text=True,
        )
        return proc.returncode == 0


class InMemoryKeychain:
    """Test double; behaves like a generic-password store."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def get(self, service: str, account: str) -> str | None:
        return self._store.get((service, account))

    def set(self, service: str, account: str, secret: str) -> None:
        self._store[(service, account)] = secret

    def delete(self, service: str, account: str) -> bool:
        return self._store.pop((service, account), None) is not None


class KeychainError(AcctswError):
    """A `security` keychain operation failed (surfaced as a friendly error, not a traceback)."""
