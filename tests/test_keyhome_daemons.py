"""BYOK homes share ch/, but must never share a daemon or become false orphans."""
import json
import os
import signal
from pathlib import Path

import pytest

from acctsw import codexhome, daemonprocs, keyhome, keyseats, providers, session
from acctsw.util import write_json


def _seat(ctx, harness='codex'):
    return keyseats.add(ctx, providers.get_provider(
        'openai' if harness == 'codex' else 'anthropic'), 'private-test-credential',
        label='Work', model='test-model', get=lambda *args: (200, '{}'))


def _home(ctx, seat, pin=None):
    return codexhome.home_dir(
        codexhome.key_home_identity(seat['harness'], seat['id'], pin), ctx._homes_root)


def _proc(home, pid=90001):
    path = home / 'packages/app-server-daemon/releases/current-v/codex'
    return daemonprocs.Process(pid, os.getuid(), (str(path),), path, (1, 0))


def _pin(ctx, seat, monkeypatch, *, pid=101, pin='terminal-one'):
    monkeypatch.setattr(session.os, 'getpid', lambda: pid)
    monkeypatch.setattr(session, '_alive', lambda pid: True)
    monkeypatch.setattr(session, '_proc_start', lambda pid: 'Stable Start')
    session.mark_session(ctx.data_dir, seat['harness'], 'subscription@example.com',
                         pin=pin, key_seat=seat)
    return _home(ctx, seat, pin)


def _no_signal(*args):
    pytest.fail('must not signal a protected daemon')


@pytest.mark.parametrize('harness', ['codex', 'claude'])
def test_registered_key_home_never_orphan_or_signalled(ctx, harness):
    seat = _seat(ctx, harness)
    home = keyhome.prepare(ctx, seat['id']).home
    processes = lambda: [_proc(home)]
    assert home.name in codexhome._seat_ids(ctx._homes_root)
    assert codexhome.orphan_daemons(ctx._homes_root, list_procs=processes) == []
    assert codexhome.reap_orphan_daemons(
        ctx._homes_root, list_procs=processes, kill=_no_signal) == []


@pytest.mark.parametrize('harness', ['codex', 'claude'])
@pytest.mark.parametrize('expired', ['dead', 'reused'])
def test_live_pin_protected_but_expired_pin_reaped(ctx, monkeypatch, harness, expired):
    seat = _seat(ctx, harness)
    home = _pin(ctx, seat, monkeypatch)
    # The pin's own metadata protects it even if its key seat is subsequently removed.
    state = ctx.load_state()
    state.data['keys'].clear()
    state.save()
    processes = [_proc(home)]
    snapshot = lambda: processes[:]
    assert codexhome.orphan_daemons(ctx._homes_root, list_procs=snapshot) == []
    assert codexhome.reap_orphan_daemons(
        ctx._homes_root, list_procs=snapshot, kill=_no_signal) == []
    if expired == 'dead':
        monkeypatch.setattr(session, '_alive', lambda pid: False)
    else:
        monkeypatch.setattr(session, '_proc_start', lambda pid: 'Different Start')
    assert codexhome.orphan_daemons(ctx._homes_root, list_procs=snapshot) == [(processes[0], home)]
    signals = []

    def kill(pid, sig):
        signals.append((pid, sig))
        processes.clear()

    assert codexhome.reap_orphan_daemons(
        ctx._homes_root, list_procs=snapshot, kill=kill, sleep=lambda _: None) == [90001]
    assert signals == [(90001, signal.SIGTERM)]


def test_multiple_live_pins_survive_legacy_mirror_replacement(ctx, monkeypatch):
    seat = _seat(ctx)
    homes = [_pin(ctx, seat, monkeypatch, pid=pid, pin=f'pin-{pid}') for pid in (101, 202)]
    processes = lambda: [_proc(home, pid) for pid, home in enumerate(homes, 90001)]
    assert codexhome.orphan_daemons(ctx._homes_root, list_procs=processes) == []
    assert codexhome.reap_orphan_daemons(
        ctx._homes_root, list_procs=processes, kill=_no_signal) == []


@pytest.mark.parametrize('keys', [None, [], 'bad', 7, {'seat': None}, {'seat': {}},
                                 {'seat': {'id': 'seat', 'harness': None}},
                                 {'seat': {'id': 7, 'harness': 'codex'}},
                                 {'seat': {'id': 'different', 'harness': 'codex'}}])
def test_malformed_key_registry_never_widens_kill_set(ctx, keys):
    state = ctx.load_state()
    state.data['keys'] = keys
    state.save()
    processes = lambda: [_proc(ctx._homes_root / 'unknown')]
    assert codexhome.orphan_daemons(ctx._homes_root, list_procs=processes) == []
    assert codexhome.reap_orphan_daemons(
        ctx._homes_root, list_procs=processes, kill=_no_signal) == []


@pytest.mark.parametrize('broken', ['json', 'pid', 'pin', 'key_seat', 'unreadable', 'directory'])
def test_uncertain_pin_registry_defers_reaping_repeatedly(ctx, monkeypatch, broken):
    seat = _seat(ctx)
    home = _pin(ctx, seat, monkeypatch)
    path = ctx.data_dir / 'session-codex-101.json'
    if broken == 'json':
        path.write_text('{')
    elif broken in ('pid', 'pin', 'key_seat'):
        data = json.loads(path.read_text())
        data[broken] = None
        write_json(path, data)
    elif broken == 'unreadable':
        original = Path.open

        def denied(self, *args, **kwargs):
            if self == path:
                raise PermissionError('unreadable heartbeat')
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, 'open', denied)
    else:
        original = Path.iterdir

        def denied(self):
            if self == ctx.data_dir:
                raise PermissionError('unreadable registry')
            return original(self)

        monkeypatch.setattr(Path, 'iterdir', denied)
    processes = lambda: [_proc(home), _proc(ctx._homes_root / 'unknown', 90002)]
    for _ in range(2):
        assert codexhome.orphan_daemons(ctx._homes_root, list_procs=processes) == []
        assert codexhome.reap_orphan_daemons(
            ctx._homes_root, list_procs=processes, kill=_no_signal) == []
    assert path.exists()


def _releases(home):
    package = home / 'packages/app-server-daemon'
    for version in ('current-v', 'old'):
        release = package / 'releases' / version
        release.mkdir(parents=True)
        (release / 'codex').write_bytes(b'12345')
    (package / 'current').symlink_to('releases/current-v')
    return package


@pytest.mark.parametrize('harness', ['codex', 'claude'])
def test_report_and_gc_cover_key_and_retained_pin_homes(ctx, monkeypatch, harness):
    seat = _seat(ctx, harness)
    homes = [_home(ctx, seat), _pin(ctx, seat, monkeypatch)]
    packages = [_releases(home) for home in homes]
    processes = [_proc(home, pid) for pid, home in enumerate(homes, 90001)]
    monkeypatch.setattr(daemonprocs, 'list_processes', lambda: processes[:])
    monkeypatch.setattr(daemonprocs, 'wedged_supervisors', lambda **kw: ([], []))
    reaper = codexhome.reap_orphan_daemons
    monkeypatch.setattr(codexhome, 'reap_orphan_daemons', lambda root: reaper(
        root, list_procs=lambda: processes[:], kill=_no_signal))
    gc = codexhome.gc_daemon_releases
    monkeypatch.setattr(codexhome, 'gc_daemon_releases', lambda home, **kw: gc(
        home, list_procs=lambda: processes[:], **kw))
    report = codexhome.daemon_report(ctx)
    assert {row['home_id'] for row in report['seats']} == {home.name for home in homes}
    assert len(report['seats']) == 2  # the legacy heartbeat must not duplicate the pin
    assert report['orphan_pids'] == []
    report = codexhome.daemon_report(ctx, fix=True)
    assert report['reaper'] == 'session-live'
    assert all(row['gc'] == 'session-live' and row['bytes_freed'] == 0 for row in report['seats'])
    session.clear_session(ctx.data_dir, harness)
    processes.clear()
    # After the heartbeat disappears, the pinned home still receives release maintenance.
    report = codexhome.daemon_report(ctx, fix=True)
    assert {row['home_id'] for row in report['seats']} == {home.name for home in homes}
    assert all(row['gc'] == 'checked' and row['bytes_freed'] == 5 for row in report['seats'])
    for package in packages:
        assert (package / 'releases/current-v/codex').read_bytes() == b'12345'
        assert not (package / 'releases/old').exists()


@pytest.mark.parametrize('harness', ['codex', 'claude'])
@pytest.mark.parametrize('pin', [None, 'terminal-one'])
def test_prepare_never_creates_links_or_promotes_daemon_runtime(ctx, harness, pin):
    seat = _seat(ctx, harness)
    for name in codexhome.DAEMON_RUNTIME:
        entry = ctx._codex_real / name
        entry.mkdir(parents=True)
        (entry / 'shared').write_text('shared')
    home = keyhome.prepare(ctx, seat['id'], pin=pin).home
    for name in codexhome.DAEMON_RUNTIME:
        assert not (home / name).exists()
        assert not (home / name).is_symlink()
        entry = home / name
        entry.mkdir()
        (entry / 'private').write_text('private')
    keyhome.prepare(ctx, seat['id'], pin=pin)
    for name in codexhome.DAEMON_RUNTIME:
        assert (home / name / 'private').read_text() == 'private'
        assert not (home / name / 'shared').exists()
        assert (ctx._codex_real / name / 'shared').read_text() == 'shared'
        assert not (ctx._codex_real / name / 'private').exists()
    assert not any(path.is_symlink() for path in home.rglob('*'))
