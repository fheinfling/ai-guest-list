"""Lifecycle decoding and supervisor rescue; all rescue signals/tables are injected."""
import ctypes
import errno
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from acctsw import daemonprocs as D


def supervisor(**changes):
    p = D.Process(101, os.getuid(), ('/some path/python3.14', '-P', '-m', 'acctsw', 'run', 'codex'),
                  Path('/some path/python3.14'), (10, 20), 1, False, 'running')
    return replace(p, **changes)


def child(**changes):
    p = D.Process(102, os.getuid(), (), Path(), (11, 20), 101, True, 'exiting')
    return replace(p, **changes)


@pytest.mark.parametrize('state', ['exiting', 'zombie'])
@pytest.mark.parametrize('fix', [False, True])
def test_rescue_requires_two_samples_and_rechecks_before_signal(state, fix):
    clock, samples, signals = [0], [], []
    table = [supervisor(), child(state=state)]
    def snapshot():
        samples.append(clock[0])
        return table
    def sleep(delay):
        assert delay >= 1
        clock[0] += delay
    assert D.wedged_supervisors(fix=fix, list_procs=snapshot,
                               sleep=sleep, kill=lambda *a: signals.append(a)) == ([101], [101] if fix else [])
    assert samples == ([0, 1, 1] if fix else [0, 1])
    assert signals == ([(101, signal.SIGKILL)] if fix else [])


@pytest.mark.parametrize('table', [
    [supervisor(uid=os.getuid() + 1), child()],
    [supervisor(ppid=42), child()],
    [supervisor(has_tty=True), child()],
    [supervisor(has_tty=None), child()],
    [supervisor(state='unknown'), child()],
    [supervisor(argv=()), child()],
    [supervisor(argv=('/bin/sh', '-m', 'acctsw', 'run', 'codex')), child()],
    [supervisor(argv=('python3', '-c', 'pass', '-m', 'acctsw', 'run', 'codex')), child()],
    [supervisor(argv=('python3', 'script.py', '-m', 'acctsw', 'run', 'codex')), child()],
    [supervisor(argv=('python3', '-m acctsw run codex')), child()],
    [supervisor(argv=('python3', '-m', 'other', 'run', 'codex')), child()],
    [supervisor(), child(state='running')],
    [supervisor(), child(state='unknown')],
    [supervisor(), child(ppid=999)],
    [supervisor(), child(), child(pid=103, state='running')],
    [supervisor(), child(), child(pid=103, uid=os.getuid() + 1, state='running')],
    [supervisor()],
])
def test_rescue_vetoes_unproven_supervisors(table):
    signals, sleeps = [], []
    assert D.wedged_supervisors(fix=True, list_procs=lambda: table,
                               sleep=sleeps.append, kill=lambda *a: signals.append(a)) == ([], [])
    assert signals == sleeps == []


@pytest.mark.parametrize('changed', [
    [supervisor(start=(20, 20)), child()],  # recycled supervisor PID
    [supervisor(), child(start=(21, 20))],  # recycled child PID
    [supervisor(), child(state='running')],
    [supervisor(has_tty=True), child()],
    [supervisor(ppid=42), child()],
    [],
])
@pytest.mark.parametrize('change_at', [1, 2])
def test_rescue_vetoes_races(changed, change_at):
    snapshots = iter([[supervisor(), child()]] * change_at + [changed])
    signals = []
    found, sent = D.wedged_supervisors(fix=True, list_procs=lambda: next(snapshots),
                                     sleep=lambda _: None, kill=lambda *a: signals.append(a))
    assert sent == signals == []
    if change_at == 1:
        assert found == []


def test_rescue_fails_closed_on_inspection_error():
    def fail():
        raise OSError('denied')
    assert D.wedged_supervisors(fix=True, list_procs=fail,
                               kill=lambda *_: pytest.fail('unexpected signal')) == ([], [])


@pytest.mark.parametrize('status, flags, expected', [(3, D.P_WEXIT, 'exiting'), (5, 0, 'zombie'),
                                                    (3, 0, 'running')])
def test_darwin_kernel_state_decoding(monkeypatch, status, flags, expected):
    info = D._KinfoProc(seconds=42, microseconds=123, flags=flags, status=status,
                        pid=101, uid=os.getuid(), ppid=1, tdev=-1)
    monkeypatch.setattr(D.sys, 'platform', 'darwin')
    monkeypatch.setattr(D, '_kinfo', lambda pid: info)
    record = D.process_state(101)
    assert record == D.ProcessState(os.getuid(), 1, False, (42, 123), expected)
    assert record.dead == (expected != 'running')


def test_darwin_revoked_terminal_counts_as_no_tty(monkeypatch):
    # A closed tab leaves P_CONTROLT (0x2) and EPROC_CTTY set on the session while the device is
    # NODEV. ps(1) prints "??" for it, and so must we, or no orphaned supervisor is ever rescued.
    info = D._KinfoProc(seconds=1, microseconds=2, flags=0x4006, status=2, pid=101,
                        uid=os.getuid(), ppid=1, tdev=-1, eflags=1)
    monkeypatch.setattr(D.sys, 'platform', 'darwin')
    monkeypatch.setattr(D, '_kinfo', lambda pid: info)
    assert D.process_state(101).has_tty is False
    info.tdev = 0x10000004
    assert D.process_state(101).has_tty is True


def test_darwin_kinfo_lp64_layout():
    assert ctypes.sizeof(D._KinfoProc) == 648
    assert {name: getattr(D._KinfoProc, name).offset for name in
            ('seconds', 'flags', 'status', 'pid', 'uid', 'ppid', 'tdev', 'eflags')} == {
                'seconds': 0, 'flags': 32, 'status': 36, 'pid': 40, 'uid': 420,
                'ppid': 560, 'tdev': 572, 'eflags': 612}


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin kernel ABI')
def test_darwin_kernel_state_of_own_process():
    record = D.process_state(os.getpid())
    assert record is not None
    assert record.uid == os.getuid() and record.ppid == os.getppid()
    assert record.state == 'running'
    assert record.start == D.process_start(os.getpid())


@pytest.mark.parametrize('code, expected', [('Z', 'zombie'), ('X', 'exiting'), ('R', 'running')])
def test_linux_kernel_state_decoding(monkeypatch, code, expected):
    fields = [code, '1', '101', '101', '0'] + ['0'] * 14 + ['123']
    monkeypatch.setattr(D.sys, 'platform', 'linux')
    monkeypatch.setattr(Path, 'read_text', lambda p: '101 (name with ) spaces) ' + ' '.join(fields))
    monkeypatch.setattr(Path, 'stat', lambda p: type('Info', (), {'st_uid': os.getuid()})())
    monkeypatch.setattr(D, '_linux_start', lambda f: (42, int(f[19])))
    assert D.process_state(101) == D.ProcessState(os.getuid(), 1, False, (42, 123), expected)


def test_process_snapshot_retains_exiting_and_foreign_children(monkeypatch):
    states = {
        101: D.ProcessState(os.getuid(), 1, False, (10, 20), 'running'),
        102: D.ProcessState(os.getuid(), 101, True, (11, 20), 'exiting'),
        103: D.ProcessState(os.getuid(), 101, True, (12, 20), 'zombie'),
        104: D.ProcessState(os.getuid() + 1, 101, False, (13, 20), 'running'),
    }
    class Lib:
        def proc_listpids(self, kind, uid, buf, size):
            assert kind == 1  # foreign direct children cannot disappear from this snapshot
            if buf is not None:
                for i, pid in enumerate(states):
                    buf[i] = pid
            return len(states) * 4
        def proc_pidpath(self, pid, buf, size):
            assert pid == 101
            buf.value = b'/bin/python3'
            return len(buf.value)
    monkeypatch.setattr(D.sys, 'platform', 'darwin')
    monkeypatch.setattr(D, '_libproc', Lib)
    monkeypatch.setattr(D, 'process_state', states.get)
    def argv(pid):
        assert pid == 101  # exits can lack argv/executable entirely
        return supervisor().argv
    monkeypatch.setattr(D, '_argv', argv)
    table = D.list_processes()
    assert [p.state for p in table] == ['running', 'exiting', 'zombie', 'running']
    assert [p.ppid for p in table] == [1, 101, 101, 101]
    assert D._wedged(table) == {}  # the foreign live child vetoes rescue
    assert set(D._wedged(table[:3])) == {101}


def test_rescue_reports_sent_signals_if_later_inspection_fails():
    table = [supervisor(), child(), supervisor(pid=201), child(pid=202, ppid=201)]
    calls, signals = [0], []
    def snapshot():
        calls[0] += 1
        if calls[0] == 4:
            raise OSError('inspection became unavailable')
        return table
    assert D.wedged_supervisors(fix=True, list_procs=snapshot, sleep=lambda _: None,
                               kill=lambda *a: signals.append(a)) == ([101, 201], [101])
    assert signals == [(101, signal.SIGKILL)]


def install_darwin_table(monkeypatch, records):
    """Inject kernel calls, including ENOENT for replaced/deleted executable paths."""
    class Lib:
        def proc_listpids(self, kind, uid, buf, size):
            if buf is not None:
                for i, record in enumerate(records):
                    buf[i] = record.pid
            return len(records) * 4

        def proc_pidpath(self, pid, buf, size):
            record = next(p for p in records if p.pid == pid)
            if record.executable is None:
                ctypes.set_errno(errno.ENOENT)
                return 0
            buf.value = os.fsencode(record.executable)
            return len(buf.value)

    def state(pid):
        record = next(p for p in records if p.pid == pid)
        return D.ProcessState(record.uid, record.ppid, record.has_tty, record.start, record.state)

    def argv(pid):
        record = next(p for p in records if p.pid == pid)
        if not record.argv:
            raise OSError(errno.EPERM, 'KERN_PROCARGS2 unavailable')
        return record.argv

    monkeypatch.setattr(D.sys, 'platform', 'darwin')
    monkeypatch.setattr(D, '_libproc', Lib)
    monkeypatch.setattr(D, 'process_state', state)
    monkeypatch.setattr(D, '_argv', argv)


def test_partial_snapshot_still_rescues_deleted_supervisor(monkeypatch):
    records = [supervisor(executable=None), child(), supervisor(pid=103, argv=()),
               supervisor(pid=104, argv=(), executable=None)]
    install_darwin_table(monkeypatch, records)
    table = D.list_processes()
    assert [p.incomplete for p in table] == [True, False, True, True]
    assert table[0].executable is None and table[0].argv == supervisor().argv
    assert table[2].executable == supervisor().executable and table[2].argv == ()
    signals = []
    assert D.wedged_supervisors(fix=True, sleep=lambda _: None,
                               kill=lambda *a: signals.append(a)) == ([101], [101])
    assert signals == [(101, signal.SIGKILL)]


def test_rescue_ignores_changes_to_unneeded_executable(monkeypatch):
    tables = iter([[supervisor(), child()], [supervisor(executable=None), child()],
                   [supervisor(), child()]])
    signals = []
    assert D.wedged_supervisors(fix=True, list_procs=lambda: next(tables), sleep=lambda _: None,
                               kill=lambda *a: signals.append(a)) == ([101], [101])
    assert signals == [(101, signal.SIGKILL)]


@pytest.mark.parametrize('failure', ['enumeration', 'topology'])
def test_snapshot_still_fails_closed_on_kernel_errors(monkeypatch, failure):
    install_darwin_table(monkeypatch, [supervisor()])
    monkeypatch.setattr(D, 'alive', lambda _: True)
    if failure == 'topology':
        monkeypatch.setattr(D, 'process_state', lambda _: None)
    else:
        monkeypatch.setattr(D._libproc, 'proc_listpids', lambda *args: 0)
    with pytest.raises(OSError):
        D.list_processes()


@pytest.mark.skipif(sys.platform not in ('darwin', 'linux'), reason='native process inspection')
def test_live_snapshot_retains_child_with_deleted_executable(tmp_path):
    executable = tmp_path / 'sleep with spaces'
    shutil.copy('/bin/sleep', executable)
    if sys.platform == 'darwin':
        # A relocated Apple platform binary can be killed at launch; sign only our tmp copy.
        subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(executable)],
                       check=True, capture_output=True)
    process = subprocess.Popen([str(executable), '60'])
    try:
        # Popen returns after exec, before dyld finishes opening the Mach-O. Allow startup
        # before removing its pathname so this tests a deleted *running* executable.
        time.sleep(0.1)
        assert process.poll() is None
        executable.unlink()
        record = next(p for p in D.list_processes() if p.pid == process.pid)
        assert record.executable is None
        assert record.argv == (str(executable), '60')
        assert record.incomplete
        assert record.start == D.process_start(process.pid)
    finally:
        process.kill()
        process.wait(timeout=5)
