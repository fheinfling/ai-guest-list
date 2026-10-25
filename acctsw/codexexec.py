"""`codex exec` jobs: recognise one and rebuild it as a NON-interactive resume after a seat hop.

A hop stops the child and relaunches the same work on another seat. For the interactive TUI that is
``codex resume <thread>``; for ``codex exec`` it must be ``codex exec … resume <thread> <prompt>``,
or the job opens a TUI nobody is watching and hangs forever. Exec-level options given BEFORE the
``resume`` subcommand apply to the resumed run (codex-rs/exec: sandbox, cwd, add-dir, model, profile
all flow through SharedCliOptions), so the relaunch keeps every original option except the prompt
and its images, which belong to the first turn only.

Option tables mirror ``codex --help``, ``codex exec --help`` and ``codex exec resume --help`` of
codex-cli 0.163.0. Anything we do not recognise makes the job non-resumable: a guessed argv could
silently drop the sandbox or resume someone else's thread. No I/O.
"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import AcctswError

# Sent as the resumed turn's prompt. Codex falls back to stdin when PROMPT is omitted, and the
# original stdin has already been consumed by the first child.
CONTINUE_PROMPT = (
    "This run was interrupted because the session was moved to another account (the previous one "
    "hit its usage limit or was switched by the user). Continue the task from exactly where you "
    "left off."
)

_EXEC_NAMES = frozenset({"exec", "e"})

# Options that take one value. Images are separate: they take one or more (greedy, like clap).
_ROOT_VALUE = frozenset({
    "-c", "--config", "--enable", "--disable", "--remote", "--remote-auth-token-env",
    "-m", "--model", "--local-provider", "-p", "--profile", "-s", "--sandbox", "-C", "--cd",
    "--add-dir", "-a", "--ask-for-approval",
})
_ROOT_FLAGS = frozenset({
    "--strict-config", "--oss", "--approve-for-me", "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-bypass-hook-trust", "--worktree", "--search", "--no-alt-screen", "--no-daemon",
})
_EXEC_VALUE = frozenset({
    "-c", "--config", "--enable", "--disable", "-m", "--model", "--local-provider", "-p",
    "--profile", "-s", "--sandbox", "-C", "--cd", "--add-dir", "--thread-source",
    "--cyber-access-program", "--output-schema", "--color", "-o", "--output-last-message",
})
_EXEC_FLAGS = frozenset({
    "--strict-config", "--oss", "--approve-for-me", "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-bypass-hook-trust", "--worktree", "--skip-git-repo-check", "--ephemeral",
    "--ignore-user-config", "--ignore-rules", "--json",
})
_IMAGES = frozenset({"-i", "--image"})
_HELP = frozenset({"-h", "--help", "-V", "--version"})
_MODEL = frozenset({"-m", "--model"})
_CWD = frozenset({"-C", "--cd"})


@dataclass(frozen=True)
class ExecJob:
    """A parsed ``codex [root options] exec [options] [resume|fork|review …] [prompt]``."""
    root: tuple[tuple[str, ...], ...]    # root option groups, verbatim, images removed
    opts: tuple[tuple[str, ...], ...]    # exec-level option groups, verbatim, images removed
    command: str                         # "" | "resume" | "fork" | "review"
    thread: str | None                   # the id named by `exec resume <id>`, else None
    cwd: str | None                      # the last -C/--cd value, as given
    blocker: str | None                  # why this job cannot be resumed elsewhere, else None

    @property
    def resumable(self) -> bool:
        return self.blocker is None

    def resume_argv(self, exe: str, thread: str | None, *, drop_model: bool = False,
                    extra: list | tuple = ()) -> list:
        """The relaunch: same options, ``resume <thread> CONTINUE_PROMPT``. Never --last, never TUI.

        ``drop_model`` removes the user's -m/--model so ``extra`` (inserted right after ``exec``)
        can name the paid seat's model without clap rejecting a second --model.
        """
        if self.blocker is not None:
            raise AcctswError(f"codex exec: this job can't be resumed — {self.blocker}")
        if not thread:
            raise AcctswError("codex exec: can't resume a job without its session id")
        keep = [g for g in self.opts if not (drop_model and _name(g[0]) in _MODEL)]
        root = [g for g in self.root if not (drop_model and _name(g[0]) in _MODEL)]
        return [exe, *_flat(root), "exec", *extra, *_flat(keep), "resume", thread,
                CONTINUE_PROMPT]


def _flat(groups) -> list:
    return [tok for group in groups for tok in group]


def _name(tok: str) -> str:
    """The option name of a token: ``--model=x`` → ``--model``, ``-mgpt`` → ``-m``."""
    if tok.startswith("--"):
        return tok.split("=", 1)[0]
    return tok[:2]


def _take(args: list, i: int, value_opts: frozenset, flag_opts: frozenset):
    """Consume the option at ``args[i]``. Returns (group, next index, kind) where kind is
    "value" | "flag" | "image" | "help" | None (unknown)."""
    tok = args[i]
    name = _name(tok)
    attached = tok != name   # --opt=value, -Xvalue or -X=value
    if name in _IMAGES:
        group = [tok]
        i += 1
        if not attached:
            # clap: num_args = 1.., greedy — every following value up to the next option.
            while i < len(args) and (args[i] == "-" or not args[i].startswith("-")):
                group.append(args[i])
                i += 1
        return tuple(group), i, "image"
    if tok in _HELP:
        return (tok,), i + 1, "help"
    if name in value_opts:
        if attached:
            return (tok,), i + 1, "value"
        if i + 1 >= len(args):
            return None, i + 1, None
        return (tok, args[i + 1]), i + 2, "value"
    if tok in flag_opts:
        return (tok,), i + 1, "flag"
    return None, i + 1, None


def _value(group: tuple) -> str:
    if len(group) == 2:
        return group[1]
    tok = group[0]
    if tok.startswith("--"):
        return tok.split("=", 1)[1]
    rest = tok[2:]
    return rest[1:] if rest.startswith("=") else rest


def parse(args: list) -> ExecJob | None:
    """None unless ``args`` (codex's argv, without the binary) is an exec invocation."""
    root: list = []
    cwd = None
    blocker = None
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--" or tok == "-" or not tok.startswith("-"):
            break
        group, i, kind = _take(args, i, _ROOT_VALUE, _ROOT_FLAGS)
        if kind == "help":
            return None   # `codex -h …` prints help; not a session at all
        if kind is None:
            # A root option we don't know may or may not take a value, so the subcommand can't be
            # located exactly. A literal `exec` word still marks a job that must never get the
            # TUI resume; run it unhopped rather than guess its argv.
            rest = args[i:args.index("--", i)] if "--" in args[i:] else args[i:]
            if "exec" not in rest:
                return None
            return ExecJob(root=(), opts=(), command="", thread=None, cwd=None,
                           blocker=f"it uses an option acctsw does not know ({tok})")
        if kind == "image":
            continue
        if _name(group[0]) in _CWD:
            cwd = _value(group)
        if group[0] == "--worktree":
            blocker = "it uses --worktree, which codex cannot resume"
        root.append(group)
    if i >= len(args) or args[i] not in _EXEC_NAMES:
        return None
    i += 1

    opts: list = []
    command = ""
    positionals: list = []
    last = False
    after_dashes = False
    while i < len(args):
        tok = args[i]
        if after_dashes or tok == "-" or not tok.startswith("-"):
            if not command and not positionals and not after_dashes and tok in (
                    "resume", "fork", "review"):
                command = tok
            else:
                positionals.append(tok)
            i += 1
            continue
        if tok == "--":
            after_dashes = True
            i += 1
            continue
        if command in ("resume", "fork") and tok in ("--last", "--all"):
            last = last or tok == "--last"
            i += 1
            continue
        if command in ("resume", "fork") and _name(tok) in _IMAGES:
            i += 1 if tok != _name(tok) else 2   # subcommand images take exactly one value
            continue
        group, i, kind = _take(args, i, _EXEC_VALUE, _EXEC_FLAGS)
        if kind is None:
            blocker = blocker or (f"its {tok} option has no value" if _name(tok) in _EXEC_VALUE
                                  else f"it uses an option acctsw does not know ({tok})")
            continue
        if kind == "help":
            blocker = blocker or "it only prints help"
            continue
        if kind == "image":
            continue
        if _name(group[0]) in _CWD:
            cwd = _value(group)
        if group[0] == "--worktree":
            blocker = blocker or "it uses --worktree, which codex cannot resume"
        if group[0] == "--ephemeral":
            blocker = blocker or "it uses --ephemeral, so codex keeps no session to resume"
        opts.append(group)

    thread = None
    if command == "review":
        blocker = "a codex exec review cannot be resumed"
    elif command == "resume" and not last:
        if positionals:
            thread = positionals[0]
        else:
            blocker = blocker or "it does not name the session it resumes"
    elif command == "fork" and not positionals:
        blocker = blocker or "it does not name the session it forks"
    return ExecJob(root=tuple(root), opts=tuple(opts), command=command, thread=thread, cwd=cwd,
                   blocker=blocker)


def is_exec_meta(meta: dict) -> bool:
    """A rollout ``session_meta`` written by ``codex exec`` (structured, not output text)."""
    return meta.get("originator") == "codex_exec" or meta.get("source") == "exec"
