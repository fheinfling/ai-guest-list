"""Installer maintenance uses fake runners only; never invoke an installed tool."""
import json
import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from acctsw import toolupdate as U
from acctsw.util import iso


@pytest.mark.parametrize('tool,path,expected', [
    ('codex', '/opt/homebrew/Caskroom/codex/0.169.0/bin/codex', ('brew_cask', 'codex')),
    ('claude', '/usr/local/Caskroom/claude-code@latest/2.1.0/claude', ('brew_cask', 'claude-code@latest')),
    ('claude', '/opt/homebrew/Cellar/claude-code/2.1.0/bin/claude', ('brew_formula', 'claude-code')),
    ('codex', '/usr/local/Cellar/codex/0.169.0/bin/codex', ('brew_formula', 'codex')),
    ('claude', '/custom/lib/node_modules/@anthropic-ai/claude-code/cli.js', ('npm', '@anthropic-ai/claude-code')),
    ('codex', '/custom/lib/node_modules/@openai/codex/bin/codex.js', ('npm', '@openai/codex')),
    ('claude', '/Users/test/.local/share/claude/versions/2.1.0', ('native', None)),
    ('codex', '/custom/bin/codex', None),
    ('codex', '/custom/Caskroom/codex/1/bin/codex', None),
    ('claude', '/custom/node_modules/@anthropic-ai/claude-code-evil/cli.js', None),
])
def test_install_layouts_resolve_symlinks(tool, path, expected):
    assert U.install_method(tool, '/entry', realpath=lambda _: path, listdir=lambda _: []) == expected


@pytest.mark.parametrize('cask', ['claude-code', 'claude-code@latest'])
def test_copied_homebrew_claude_uses_caskroom_listing(cask):
    seen = []
    def listing(path):
        seen.append(str(path))
        return ['unrelated', cask]
    assert U.install_method('claude', '/opt/homebrew/bin/claude', listdir=listing,
                            realpath=str) == ('brew_cask', cask)
    assert seen == ['/opt/homebrew/Caskroom']


class Runner:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []
    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        result = self.outputs.pop(0)
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(returncode=0, stdout=result, stderr='')


def brew_result(kind='casks', name='codex', auto=False):
    return json.dumps({kind: [{'name': name, 'installed_versions': ['1.0'],
                              'current_version': '2.0', 'auto_updates': auto}]})


@pytest.mark.parametrize('kind,flag,path', [
    ('casks', '--cask', '/opt/homebrew/Caskroom/codex/1.0/bin/codex'),
    ('formulae', '--formula', '/usr/local/Cellar/codex/1.0/bin/codex'),
])
@pytest.mark.parametrize('auto', [False, True])
def test_brew_check_and_upgrade_argv_env(kind, flag, path, auto, monkeypatch):
    monkeypatch.setenv('PYTHONHOME', '/frozen')
    monkeypatch.setenv('HOMEBREW_NO_INSTALL_CLEANUP', '1')
    runner = Runner(brew_result(kind, auto=auto), '')
    deps = dict(runner=runner, which=lambda name: f'/tools/{name}', realpath=lambda _: path)
    found = U.check('codex', **deps)
    assert (found.installed, found.latest) == ('1.0', '2.0')
    assert U.upgrade('codex', checked=found, **deps) is True
    greedy = ['--greedy'] if flag == '--cask' else []
    assert runner.calls[0][0] == ['/tools/brew', 'outdated', flag, *greedy, '--json=v2', 'codex']
    upgrade_greedy = ['--greedy'] if flag == '--cask' and auto else []
    assert runner.calls[1][0] == ['/tools/brew', 'upgrade', flag, *upgrade_greedy, 'codex']
    for (_, kw), timeout in zip(runner.calls, [60, 900]):
        assert kw['timeout'] == timeout and kw['stdin'] == subprocess.DEVNULL
        assert kw['env']['NONINTERACTIVE'] == kw['env']['HOMEBREW_NO_ENV_HINTS'] == '1'
        assert 'PYTHONHOME' not in kw['env'] and 'HOMEBREW_NO_INSTALL_CLEANUP' not in kw['env']
        assert not kw.get('shell', False)


def test_npm_checks_installed_package_json_and_uses_its_installer(tmp_path):
    root = tmp_path / 'node_modules' / '@openai' / 'codex'
    (root / 'bin').mkdir(parents=True)
    (root / 'package.json').write_text(json.dumps({'version': '1.0.0'}))
    runner = Runner('2.0.0\n', '')
    deps = dict(runner=runner, which=lambda name: f'/tools/{name}',
                realpath=lambda _: str(root / 'bin' / 'codex.js'))
    found = U.check('codex', **deps)
    assert (found.installed, found.latest) == ('1.0.0', '2.0.0')
    assert U.upgrade('codex', checked=found, **deps)
    assert runner.calls[0][0] == ['/tools/npm', 'view', '@openai/codex', 'version']
    assert runner.calls[1][0] == ['/tools/npm', 'install', '-g', '@openai/codex@latest']


@pytest.mark.parametrize('payload', ['bad json', 'null', '[]', '{}', '{"casks":null}',
    '{"casks":[null]}', '{"casks":[{"name":"codex","installed_versions":[],"current_version":"2"}]}'])
def test_malformed_brew_check_fails_closed(payload):
    with pytest.raises(U.ToolUpdateError):
        U.check('codex', runner=Runner(payload), which=lambda _: '/fake',
                realpath=lambda _: '/opt/homebrew/Caskroom/codex/1/bin/codex')


def test_current_brew_and_unknown_native_do_not_upgrade():
    found = U.check('codex', runner=Runner('{"casks":[]}'), which=lambda _: '/fake',
                    realpath=lambda _: '/opt/homebrew/Caskroom/codex/1.0/bin/codex')
    assert found.installed == found.latest == '1.0'
    def never(*a, **k):
        pytest.fail('ran a command for an unmanaged tool')
    for tool, path in [('codex', '/custom/codex'),
                       ('claude', '/Users/a/.local/share/claude/versions/1.0')]:
        assert U.check(tool, runner=never, which=lambda _: path, realpath=str) is None
        assert U.upgrade(tool, runner=never, which=lambda _: path, realpath=str) is False


def test_maintenance_throttles_persists_and_preserves_other_state(ctx):
    stamp = [datetime(2026, 11, 20, tzinfo=timezone.utc)]
    runner = Runner(brew_result(), '')
    def which(name):
        return {'codex': '/opt/homebrew/bin/codex', 'brew': '/fake/brew'}.get(name)
    deps = dict(runner=runner, clock=lambda: stamp[0], which=which,
                realpath=lambda _: '/opt/homebrew/Caskroom/codex/1.0/bin/codex')
    notices = []
    U.run_updates(ctx, notify=notices.append, **deps)
    record = ctx.load_state().data['tool_updates']['codex']
    assert record == {'checked_at': iso(stamp[0]), 'installed': '2.0', 'latest': '2.0',
                      'method': 'brew_cask', 'error': None, 'updated_at': iso(stamp[0])}
    assert notices == ['codex updated to 2.0']
    U.run_updates(ctx, notify=notices.append, **deps)
    assert len(runner.calls) == 2
    stamp[0] += timedelta(hours=6)
    runner.outputs.append('{"casks":[]}')
    U.run_updates(ctx, **deps)
    assert len(runner.calls) == 3
    assert ctx.load_state().data['tool_updates']['codex']['updated_at'] == record['updated_at']


@pytest.mark.parametrize('stage', ['check', 'upgrade'])
def test_maintenance_records_failure_quietly_and_throttles(ctx, stage):
    outputs = [OSError('offline')] if stage == 'check' else [brew_result(), subprocess.TimeoutExpired('fake', 900)]
    runner = Runner(*outputs)
    deps = dict(runner=runner, which=lambda n: '/fake/' + n if n != 'claude' else None,
                realpath=lambda _: '/opt/homebrew/Caskroom/codex/1/bin/codex')
    notices = []
    U.run_updates(ctx, notify=notices.append, **deps)
    U.run_updates(ctx, notify=notices.append, **deps)
    record = ctx.load_state().data['tool_updates']['codex']
    assert record['error'] and record['updated_at'] is None
    assert len(runner.calls) == len(outputs) and notices == []


def test_setting_off_does_no_work_and_is_rechecked_before_upgrade(ctx):
    state = ctx.load_state()
    assert state.settings()['update_tools'] is True
    state.set_setting('update_tools', False)
    state.save()
    def never(*a, **kw):
        pytest.fail('setting off still resolved/ran a tool')
    U.run_updates(ctx, runner=never, which=never)

    state.set_setting('update_tools', True)
    state.save()
    def check_then_disable(*a, **kw):
        with ctx.locked():
            fresh = ctx.load_state()
            fresh.set_setting('update_tools', False)
            fresh.set_setting('theme', 'dark')
            fresh.save()
        return SimpleNamespace(returncode=0, stdout=brew_result(), stderr='')
    U.run_updates(ctx, runner=check_then_disable,
                  which=lambda n: '/fake/' + n if n != 'claude' else None,
                  realpath=lambda _: '/opt/homebrew/Caskroom/codex/1/bin/codex')
    assert ctx.load_state().settings()['theme'] == 'dark'
    assert ctx.load_state().data['tool_updates']['codex']['updated_at'] is None


def test_scheduler_has_one_worker_and_releases_reservation_on_failure(ctx):
    queued, notices = [], []
    class Thread:
        def __init__(self, *, target, daemon):
            assert daemon
            queued.append(target)
        def start(self):
            pass
    def failed(*a, **kw):
        raise OSError('quiet failure')
    assert U.start_updates(ctx, thread_factory=Thread, worker=failed, notify=notices.append)
    assert not U.start_updates(ctx, thread_factory=Thread, worker=failed)
    queued.pop()()
    assert U.start_updates(ctx, thread_factory=Thread, worker=lambda *a, **k: None)
    queued.pop()()
    state = ctx.load_state()
    state.set_setting('update_tools', False)
    state.save()
    assert not U.start_updates(ctx, thread_factory=Thread, worker=failed)
    assert queued == notices == []


def test_maintenance_never_holds_lock_across_an_installer(ctx):
    class UnlockedRunner(Runner):
        def __call__(self, *a, **kw):
            assert not ctx._store_lock_held()
            return super().__call__(*a, **kw)
    runner = UnlockedRunner(brew_result(), '')
    U.run_updates(ctx, runner=runner, which=lambda n: '/fake/' + n if n != 'claude' else None,
                  realpath=lambda _: '/opt/homebrew/Caskroom/codex/1/bin/codex')
    assert len(runner.calls) == 2


@pytest.mark.parametrize('installed,latest,wanted', [
    ('2.0.0-beta.1', '1.9.0', False), ('2.0.0', '2.0.0-beta.1', False),
    ('2.0.0-beta.1', '2.0.0', True), ('2.0.0-beta.2', '2.0.0-beta.10', True),
    ('1.0.0+build1', '1.0.0+build2', False), ('1.0', '1.0.0', False),
])
def test_npm_latest_never_downgrades_a_newer_or_prerelease_install(installed, latest, wanted):
    assert U.Checked(installed, latest, ('npm', '@openai/codex')).available is wanted


def test_copied_cask_up_to_date_version_comes_from_caskroom_not_cli():
    def listing(path):
        return ['claude-code@latest'] if str(path).endswith('/Caskroom') else ['2.1.0', '.metadata']
    found = U.check('claude', runner=Runner('{"casks":[]}'), which=lambda n: '/opt/homebrew/bin/' + n,
                    realpath=str, listdir=listing)
    assert found.installed == found.latest == '2.1.0'


def test_caskroom_ambiguity_and_missing_installer_fail_closed():
    assert U.install_method('claude', '/opt/homebrew/bin/claude', realpath=str,
                            listdir=lambda _: ['claude-code', 'claude-code@latest']) is None
    with pytest.raises(U.ToolUpdateError, match='installer unavailable'):
        U.check('codex', binary_path='/opt/homebrew/Caskroom/codex/1.0/bin/codex',
                which=lambda _: None, realpath=str)


def test_install_lease_surrounds_upgrade_and_is_cleared_on_completion(ctx):
    class LeaseRunner(Runner):
        def __call__(self, argv, **kw):
            if 'upgrade' in argv:
                assert U.installation_in_progress(ctx.load_state().data, 'codex')
            return super().__call__(argv, **kw)
    runner = LeaseRunner(brew_result(), '')
    U.run_updates(ctx, runner=runner, which=lambda n: '/fake/' + n if n != 'claude' else None,
                  realpath=lambda _: '/opt/homebrew/Caskroom/codex/1/bin/codex')
    assert not U.installation_in_progress(ctx.load_state().data, 'codex')


def test_abandoned_install_lease_expires():
    stamp = datetime(2026, 11, 20, tzinfo=timezone.utc)
    data = {'tool_updates': {'codex': {'installing_until': iso(stamp + timedelta(seconds=1))}}}
    assert U.installation_in_progress(data, 'codex', clock=lambda: stamp)
    assert not U.installation_in_progress(data, 'codex', clock=lambda: stamp + timedelta(seconds=2))
