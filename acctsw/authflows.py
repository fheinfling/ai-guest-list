"""Official foreground sign-in in disposable homes; protect seats from stock logout revocation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from . import accounts, claudehome, identity
from .context import Context
from .credlocations import CodexCredLocation, codex_jwt_email
from .errors import AcctswError, CannotIdentify, NoLiveCreds
from .procenv import harden_env
from .switch import switch, sync_back
from .util import email_key

OFFICIAL_LOGIN = {"codex": ["login"], "claude": ["auth", "login"]}


def signin(ctx: Context, tool: str, *, seat: str | None = None, args=()) -> int:
    """Capture only an identified login. Like add/reconcile, a successful sign-in selects its owner.

    No stock login is used as staging, and cleanup is strictly local: NEVER invoke logout here.
    The child owns the foreground TTY; the store lock covers filing, never login or profile HTTP.
    """
    home = None
    service = None
    try:
        ctx.ensure_dirs()
        home = Path(tempfile.mkdtemp(prefix="signin-", dir=ctx.data_dir.absolute()))
        env = harden_env()
        # A parent's secure-storage override must not redirect Claude back to a seat/stock item.
        for key in ("CLAUDE_SECURESTORAGE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN",
                    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"):
            env.pop(key, None)
        if tool == "codex":
            env["CODEX_HOME"] = str(home)
        else:
            env["CLAUDE_CONFIG_DIR"] = str(home)
            env["USER"] = ctx.cred["claude"].account
            service = claudehome.service_name(str(home))
        exe = (ctx.codex_bin if tool == "codex" else ctx.claude_bin) or tool
        result = subprocess.run([exe, *OFFICIAL_LOGIN[tool], *args], env=env)
        if result.returncode:
            return result.returncode
        blob = (CodexCredLocation(home / "auth.json").get_live() if tool == "codex"
                else ctx.keychain.get_raw(service, ctx.cred["claude"].account))
        if not blob:
            raise NoLiveCreds("sign-in did not produce credentials — please try again")
        owner = (codex_jwt_email(blob) if tool == "codex"
                 else identity.claude_profile_email(blob, ctx.claude_bin))
        if not owner:
            raise CannotIdentify("could not identify the signed-in account; no seat was changed")
        if seat and email_key(owner) != email_key(seat):
            raise AcctswError(f"signed into {owner}, not {seat}; no seat was changed. "
                              f"To add that account, run `acctsw signin {tool}` again.")
        stock_identity = identity.claude_live_identity(ctx) if tool == "claude" else None
        with ctx.locked():
            state = ctx.load_state()
            sync_back(ctx, state, tool, live_identity=stock_identity)
            owner = next((e for e in state.accounts(tool) if email_key(e) == email_key(owner)), owner)
            saved = accounts.add(ctx, state, tool, email=owner, blob=blob)
            # Explicit sign-in resumes stock mirroring even after a deliberate local logout.
            state.data["tools"][tool].pop("stock_logged_out", None)
            switch(ctx, state, tool, owner, sync=False)
        print(f"✓ seat saved — {tool}:{saved['email']} (now on the floor)")
        return 0
    except AcctswError:
        raise
    except Exception:
        # CLI/Keychain/filesystem exceptions can contain credentials; expose only our own text.
        raise AcctswError("sign-in failed — please try again") from None
    finally:
        try:
            if service is not None:
                ctx.keychain.delete(service, ctx.cred["claude"].account)
                if ctx.keychain.get_raw(service, ctx.cred["claude"].account) is not None:
                    raise AcctswError("temporary sign-in Keychain item is still present")
        except Exception:
            raise AcctswError("could not remove the temporary sign-in Keychain item") from None
        finally:
            if home is not None:
                try:
                    shutil.rmtree(home)
                except Exception:
                    raise AcctswError("could not remove the temporary sign-in home") from None


def _refresh_digest(blob: str) -> str | None:
    try:
        token = json.loads(blob)["claudeAiOauth"]["refreshToken"]
        return hashlib.sha256(token.encode("utf-8")).hexdigest() if isinstance(token, str) and token else None
    except (ValueError, TypeError, KeyError):
        return None


def _shared_seat(ctx, state, tool, blob, owner=None):
    if owner:
        match = next((e for e in state.accounts(tool) if email_key(e) == email_key(owner)), None)
        if match:
            return match
    if tool == "claude":
        digest = _refresh_digest(blob)
        for email in state.accounts(tool):
            for stored in (ctx.claude_cred(email).get_live(), ctx.snapshot_get(tool, email)):
                if stored and (stored == blob or (digest and _refresh_digest(stored) == digest)):
                    return email
    return None


def safe_logout(ctx: Context, tool: str) -> bool:
    """True if a shared stock login was removed locally. False permits official logout.

    Prefer local evidence, then Claude's read-only profile (no refresh). Recheck credentials and
    registration under the store lock; a racing login must never lose to an old identity answer.
    """
    try:
        blob = ctx.cred[tool].get_live()
        if not blob:
            return False
        owner = codex_jwt_email(blob) if tool == "codex" else None
        if tool == "claude" and not _shared_seat(ctx, ctx.load_state(), tool, blob):
            owner = identity.claude_profile_email(blob, ctx.claude_bin)
        with ctx.locked():
            state = ctx.load_state()
            if ctx.cred[tool].get_live() != blob:
                raise AcctswError("stock login changed during logout — please try again")
            email = _shared_seat(ctx, state, tool, blob, owner)
            if not email:
                return False
            # Persist BEFORE deleting, so another process cannot resurrect the item after a crash.
            state.data["tools"][tool]["stock_logged_out"] = True
            state.save()
            if tool == "codex":
                ctx.cred[tool].auth_path.unlink(missing_ok=True)
            else:
                item = ctx.cred[tool]
                ctx.keychain.delete(item.service, item.account)
                if item.get_live() is not None:
                    raise AcctswError("could not remove the stock Claude login — please try again")
        print(f"Removed the local {tool} login; seat {email} keeps its login. "
              "Use the app's sign-out to sign that seat out.")
        return True
    except AcctswError:
        raise
    except Exception:
        raise AcctswError("could not safely remove the stock login — please try again") from None
