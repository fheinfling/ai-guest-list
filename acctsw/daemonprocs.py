"""Kernel process records for daemon maintenance; never infer identity from CLI prose.

Darwin's libproc gives epoch start seconds AND microseconds without locale, timezone or DST
ambiguity. KERN_PROCARGS2 retains argv[0]'s original spelling (including spaces and the old
codex-homes index), whereas proc_pidpath resolves that index and would hide legacy orphans.
Keep both: the spelling classifies orphans, and either path pins releases during GC. Missing
details stay local to a record; enumeration or kernel topology failures abort the snapshot.
"""
from __future__ import annotations

import ctypes
import errno
import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Process:
    pid: int
    uid: int
    argv: tuple[str, ...]
    executable: Path | None
    start: tuple[int, int]
    ppid: int | None = None
    has_tty: bool | None = None
    state: str = 'unknown'

    @property
    def incomplete(self) -> bool:
        # Foreign/dead records intentionally contribute topology only.
        return (self.uid == os.getuid() and not self.dead and
                (self.executable is None or not self.argv))

    @property
    def dead(self) -> bool:
        return self.state in ('exiting', 'zombie')


@dataclass(frozen=True)
class ProcessState:
    uid: int
    ppid: int
    has_tty: bool
    start: tuple[int, int]
    state: str

    @property
    def dead(self) -> bool:
        return self.state in ('exiting', 'zombie')


# Darwin LP64 sys/sysctl.h kinfo_proc / sys/proc.h extern_proc. These are ABI offsets,
# not proc_bsdinfo.flags (which uses different flags!). Verified with sizeof/offsetof
# against MacOSX26.5.sdk: sizeof(kinfo_proc)=648, sizeof(extern_proc)=296.
SZOMB = 5
P_WEXIT = 0x00002000


class _KinfoProc(ctypes.Structure):
    _fields_ = [
        ('seconds', ctypes.c_int64), ('microseconds', ctypes.c_int32),
        ('_pad0', ctypes.c_byte * 20),
        ('flags', ctypes.c_uint32), ('status', ctypes.c_uint8),
        ('_pad1', ctypes.c_byte * 3), ('pid', ctypes.c_int32),
        ('_pad2', ctypes.c_byte * 376), ('uid', ctypes.c_uint32),
        ('_pad3', ctypes.c_byte * 136), ('ppid', ctypes.c_int32),
        ('_pad4', ctypes.c_byte * 8), ('tdev', ctypes.c_int32),
        ('_pad5', ctypes.c_byte * 36), ('eflags', ctypes.c_int32),
        ('_pad6', ctypes.c_byte * 32),
    ]


def _kinfo(pid: int) -> _KinfoProc:
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        raise OSError(errno.ENOTSUP, 'unsupported kinfo_proc ABI')
    info = _KinfoProc()
    size = ctypes.c_size_t(ctypes.sizeof(info))
    mib = (ctypes.c_int * 4)(1, 14, 1, pid)  # CTL_KERN, KERN_PROC, KERN_PROC_PID
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.sysctl(mib, 4, ctypes.byref(info), ctypes.byref(size), None, 0):
        raise OSError(ctypes.get_errno(), 'KERN_PROC_PID unavailable')
    if size.value != ctypes.sizeof(info) or info.pid != pid:
        raise OSError(errno.ESRCH, 'missing/incompatible kinfo_proc')
    return info


def process_state(pid: int) -> ProcessState | None:
    """Kernel lifecycle/topology; unknown never authorises healing or a signal."""
    try:
        if pid <= 0:
            return None
        if sys.platform == 'darwin':
            info = _kinfo(pid)
            state = ('zombie' if info.status == SZOMB else
                     'exiting' if info.flags & P_WEXIT else 'running')
            # The controlling tty's device, exactly what ps(1) prints (NODEV → "??"). P_CONTROLT and
            # EPROC_CTTY describe the SESSION and stay set after the terminal is revoked — a closed
            # tab leaves them on — so trusting them would hide every orphaned supervisor.
            return ProcessState(info.uid, info.ppid, info.tdev != -1,
                                (info.seconds, info.microseconds), state)
        proc = Path('/proc') / str(pid)
        fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
        start = _linux_start(fields)
        state = 'zombie' if fields[0] == 'Z' else 'exiting' if fields[0] in ('X', 'x') else 'running'
        return ProcessState(proc.stat().st_uid, int(fields[1]), int(fields[4]) != 0, start, state)
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def _linux_start(fields) -> tuple[int, int]:
    boot = next(int(line.split()[1]) for line in Path('/proc/stat').read_text().splitlines()
                if line.startswith('btime '))
    ticks = int(fields[19])
    hz = os.sysconf('SC_CLK_TCK')
    return boot + ticks // hz, (ticks % hz) * 1_000_000 // hz


def alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True                       # EPERM/unknown: not evidence of death
    return True


def _libproc():
    return ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)


def process_start(pid: int) -> tuple[int, int] | None:
    """None means unknown, never proof of PID reuse. Linux fallback is for tests/CLI users."""
    try:
        if sys.platform == 'darwin':
            # kinfo_proc, not proc_pidinfo: PROC_PIDTBSDINFO intermittently fails for a live
            # process whose executable was deleted — exactly a daemon whose release was swapped out.
            info = _kinfo(pid)
            return info.seconds, info.microseconds
        fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
        return _linux_start(fields)
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def _argv(pid: int) -> tuple[str, ...]:
    """Read only argc/argv from KERN_PROCARGS2; the trailing environment can contain secrets."""
    libc = ctypes.CDLL(None, use_errno=True)
    mib = (ctypes.c_int * 3)(1, 49, pid)   # CTL_KERN, KERN_PROCARGS2
    size = ctypes.c_size_t(0)
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0):
        raise OSError(ctypes.get_errno(), 'KERN_PROCARGS2 unavailable')
    buf = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0):
        raise OSError(ctypes.get_errno(), 'KERN_PROCARGS2 unavailable')
    argc = ctypes.c_int.from_buffer(buf).value
    raw = buf.raw[:size.value]
    offset = raw.index(b'\0', ctypes.sizeof(ctypes.c_int)) + 1  # skip executable path
    while offset < len(raw) and raw[offset] == 0:
        offset += 1
    parts = raw[offset:].split(b'\0', argc)
    if argc <= 0 or len(parts) <= argc:
        raise OSError(errno.EIO, 'incomplete argv')
    return tuple(os.fsdecode(p) for p in parts[:argc])


def list_processes() -> list[Process]:
    """Our uid's details plus foreign topology; fail closed on enumeration/topology errors.

    Foreign children must remain visible: an acctsw child which changed uid still vetoes rescue.
    Never inspect their argv/executable, nor classify them as owned maintenance candidates.
    Unreadable executable/argv become None/() independently; consumers decide what they need.
    """
    result = []
    if sys.platform == 'darwin':
        lib = _libproc()
        size = lib.proc_listpids(1, 0, None, 0)  # PROC_ALL_PIDS
        if size <= 0:
            raise OSError(errno.EIO, 'proc_listpids unavailable')
        buf = (ctypes.c_int * (size // 4 + 1024))()
        size = lib.proc_listpids(1, 0, buf, ctypes.sizeof(buf))
        if size <= 0 or size == ctypes.sizeof(buf):
            raise OSError(errno.EIO, 'incomplete process list')
        pids = [pid for pid in buf[:size // 4] if pid > 0]
    else:
        pids = [int(p.name) for p in Path('/proc').iterdir() if p.name.isdigit()]
    for pid in pids:
        try:
            state = process_state(pid)
            if state is None:
                raise OSError(errno.EIO, 'process state unavailable')
            if state.uid != os.getuid():
                result.append(Process(pid, state.uid, (), None, state.start,
                                      state.ppid, state.has_tty, state.state))
                continue
            # Exiting processes can have no executable/argv/fds left. Keep their topology for
            # supervisor rescue; neither GC nor orphan classification needs their executable.
            argv, executable = (), None
            if not state.dead and sys.platform == 'darwin':
                try:
                    argv = _argv(pid)
                except (OSError, ValueError):
                    pass
                path = ctypes.create_string_buffer(4096)
                try:
                    if lib.proc_pidpath(pid, path, len(path)) > 0 and path.value:
                        executable = Path(os.fsdecode(path.value))
                except OSError:
                    pass
            elif not state.dead:
                proc = Path('/proc') / str(pid)
                try:
                    argv = tuple(os.fsdecode(p) for p in (proc / 'cmdline').read_bytes().split(b'\0') if p)
                except OSError:
                    pass
                try:
                    executable = (proc / 'exe').resolve(strict=True)
                except OSError:
                    pass
            fresh = process_state(pid)
            if fresh is None or fresh.start != state.start:
                raise OSError(errno.EIO, 'process changed during inspection')
            result.append(Process(pid, fresh.uid, argv, executable, fresh.start,
                                  fresh.ppid, fresh.has_tty, fresh.state))
        except (OSError, ValueError):
            if alive(pid):
                raise OSError(errno.EIO, 'live process could not be inspected')
    return result


def _supervisor(proc: Process) -> bool:
    if (proc.uid != os.getuid() or proc.ppid != 1 or proc.has_tty is not False or
            proc.state != 'running' or not proc.argv):
        return False
    if not re.fullmatch(r'python(?:[0-9]+(?:\.[0-9]+)*)?t?', Path(proc.argv[0]).name.lower()):
        return False
    # Recognise Python's module invocation, never '-c' code or a script's argument list.
    args = list(proc.argv[1:])
    while args and args[0] in ('-P', '-I', '-s', '-S', '-u', '-E', '-B', '-q', '-O', '-OO'):
        args.pop(0)
    return len(args) >= 4 and args[:3] == ['-m', 'acctsw', 'run']


def _wedged(table: list[Process]) -> dict[int, tuple[Process, set[tuple]]]:
    result = {}
    for proc in table:
        if not _supervisor(proc):
            continue
        children = [p for p in table if p.ppid == proc.pid]
        if children and all(p.uid == proc.uid and p.dead for p in children):
            result[proc.pid] = (proc, {(p.pid, p.start) for p in children})
    return result


def _supervisor_identity(proc: Process) -> tuple:
    return (proc.pid, proc.uid, proc.start, proc.argv, proc.ppid, proc.has_tty, proc.state)


def wedged_supervisors(*, fix=False, list_procs=None, kill=os.kill,
                       sleep=time.sleep) -> tuple[list[int], list[int]]:
    """Confirm orphaned, tty-less acctsw supervisors twice, >=1s apart; optionally KILL.

    Any live/unknown direct child vetoes rescue. Require the same supervisor AND at least one
    same exiting child across samples. Recheck all predicates immediately before each signal.
    Darwin has no pidfd: start-time identity and a recheck right before the signal mitigate,
    but cannot eliminate, the residual PID-reuse TOCTOU between identity recheck and kill.
    The return distinguishes proven wedges from signals sent, not claimed process deaths.
    """
    list_procs = list_procs or list_processes
    try:
        first = _wedged(list_procs())
        if not first:
            return [], []
        sleep(1.0)
        second = _wedged(list_procs())
        confirmed = {pid: pair for pid, pair in second.items()
                     if pid in first and
                     _supervisor_identity(pair[0]) == _supervisor_identity(first[pid][0]) and
                     pair[1] & first[pid][1]}
    except OSError:
        return [], []
    signalled = []
    if fix:
        for pid, (proc, children) in confirmed.items():
            try:
                fresh = _wedged(list_procs()).get(pid)
            except OSError:
                break  # Defer remaining rescues, but still report any signals already sent.
            if (fresh and _supervisor_identity(fresh[0]) == _supervisor_identity(proc) and
                    fresh[1] & children & first[pid][1]):
                try:
                    kill(pid, signal.SIGKILL)
                    signalled.append(pid)
                except OSError:
                    pass
    return sorted(confirmed), signalled
