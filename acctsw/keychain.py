"""macOS Keychain access, behind an interface so tests can swap in an in-memory fake.

We deliberately depend on the ``security`` CLI rather than a third-party library: no extra deps,
and it is exactly what the official tools and the reference apps use.
"""
from __future__ import annotations

import subprocess
from typing import Protocol


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
        return proc.stdout.rstrip("\n")

    def set(self, service: str, account: str, secret: str) -> None:
        # -U updates if it already exists. -w passes the secret (avoids interactive prompt).
        proc = subprocess.run(
            [self._security, "add-generic-password", "-U",
             "-s", service, "-a", account, "-w", secret],
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


class KeychainError(RuntimeError):
    pass
