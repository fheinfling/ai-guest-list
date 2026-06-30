"""Typed errors so the CLI and the app can present clear, friendly messages."""
from __future__ import annotations


class AcctswError(RuntimeError):
    """Base class for expected, user-facing errors."""


class NoLiveCreds(AcctswError):
    """No active credentials found for a tool (user must sign in first)."""


class CannotIdentify(AcctswError):
    """Could not determine the account email for the live creds."""


class UnknownSeat(AcctswError):
    """Referenced a seat (email) that is not registered."""


class MissingSnapshot(AcctswError):
    """A seat is registered but its credential snapshot is missing from the keychain."""
