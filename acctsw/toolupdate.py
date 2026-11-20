"""Quiet installer maintenance. All commands, path resolution and time are injectable.

Only recognized layouts are managed; never invoke a CLI itself, guess its installer, or hold
our store lock while running an installer. The six-hour reservation also covers failed checks.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from . import context, procenv
from .util import iso, now, parse_iso

CHECK_INTERVAL_S = 6 * 3600
CHECK_TIMEOUT_S = 60
UPGRADE_TIMEOUT_S = 15 * 60
_PACKAGES = {'claude': '@anthropic-ai/claude-code', 'codex': '@openai/codex'}
_BREW = re.compile(r'^(/opt/homebrew|/usr/local)/(Caskroom|Cellar)/([A-Za-z0-9][A-Za-z0-9@+_.-]*)/')
_VERSION = re.compile(r'\d+(?:\.\d+)+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?')


class ToolUpdateError(Exception):
    pass


def _package_root(tool, resolved):
    marker = '/node_modules/' + _PACKAGES[tool]
    before, sep, after = resolved.partition(marker + '/')
    return Path(before + marker) if sep else None


def install_method(tool, binary_path, *, realpath=os.path.realpath, listdir=os.listdir):
    """Identify the installer from disk layout, without running any command."""
    if tool not in _PACKAGES or not binary_path:
        return None
    resolved = str(realpath(binary_path))
    match = _BREW.match(resolved)
    if match:
        return ('brew_cask' if match[2] == 'Caskroom' else 'brew_formula', match[3])
    if _package_root(tool, resolved):
        return 'npm', _PACKAGES[tool]
    if tool == 'claude':
        if re.search(r'/\.local/share/claude/versions/[^/]+$', resolved):
            return 'native', None
        # Some Claude casks copy a Mach-O into bin rather than linking into Caskroom.
        for prefix in ('/opt/homebrew', '/usr/local'):
            if resolved == prefix + '/bin/claude':
                try:
                    entries = listdir(prefix + '/Caskroom')
                except OSError:
                    return None
                matches = [name for name in ('claude-code', 'claude-code@latest') if name in entries]
                # Two installations at once are ambiguous: do not upgrade an arbitrary one.
                return ('brew_cask', matches[0]) if len(matches) == 1 else None
    return None


def path_version(resolved):
    match = re.search(r'/(?:Caskroom|Cellar)/[^/]+/([^/]+)', str(resolved))
    version = match[1] if match else Path(resolved).name
    return version if _VERSION.fullmatch(version) else None


def _npm_version_key(version):
    release, _, _build = version.partition('+')
    core, _, prerelease = release.partition('-')
    numbers = [int(n) for n in core.split('.')]
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers.pop()
    pre = tuple((0, int(part)) if part.isdigit() else (1, part)
                for part in prerelease.split('.')) if prerelease else ()
    return tuple(numbers), not bool(prerelease), pre


def _brew_installed_version(resolved, method, listdir):
    version = path_version(resolved)
    if version or method[0] != 'brew_cask':
        return version
    # A copied cask executable has no version in its realpath. Read just its version dirs.
    prefix = next((p for p in ('/opt/homebrew', '/usr/local')
                   if resolved.startswith(p + '/')), None)
    if prefix:
        try:
            versions = [v for v in listdir(prefix + '/Caskroom/' + method[1]) if _VERSION.fullmatch(v)]
        except OSError:
            return None
        return versions[0] if len(versions) == 1 else None
    return None


@dataclass(frozen=True)
class Checked:
    installed: str | None
    latest: str | None
    method: tuple[str, str | None]
    auto_updates: bool = False

    @property
    def available(self):
        if not self.latest or not self.installed:
            return False
        if self.method[0] == 'npm':
            return _npm_version_key(self.latest) > _npm_version_key(self.installed)
        # Brew has already decided this package is outdated.
        return self.latest != self.installed


def _run(argv, timeout, runner):
    env = procenv.harden_env()
    procenv.shed_seat_env(env)
    env.pop('HOMEBREW_NO_INSTALL_CLEANUP', None)
    env.update(HOMEBREW_NO_ENV_HINTS='1', NONINTERACTIVE='1')
    result = runner(argv, capture_output=True, text=True, encoding='utf-8', errors='replace',
                    timeout=timeout, stdin=subprocess.DEVNULL, env=env)
    if result.returncode != 0:
        # Avoid persisting arbitrary installer output (which may contain environment secrets).
        raise ToolUpdateError(f'{Path(argv[0]).name} exited with status {result.returncode}')
    return result.stdout


def check(tool, *, binary_path=None, runner=subprocess.run, which=context.which_tool,
          realpath=os.path.realpath, listdir=os.listdir):
    binary = binary_path or which(tool)
    method = install_method(tool, binary, realpath=realpath, listdir=listdir)
    if method is None or method[0] == 'native':
        return None
    kind, package = method
    installer = which('npm' if kind == 'npm' else 'brew')
    if not installer:
        raise ToolUpdateError('installer unavailable')
    resolved = str(realpath(binary))
    if kind == 'npm':
        root = _package_root(tool, resolved)
        try:
            metadata = json.loads((root / 'package.json').read_text(encoding='utf-8'))
            installed = metadata.get('version') if isinstance(metadata, dict) else None
        except (OSError, ValueError) as exc:
            raise ToolUpdateError('cannot read installed package version') from exc
        latest = _run([installer, 'view', package, 'version'], CHECK_TIMEOUT_S, runner).strip()
        if not isinstance(installed, str) or not _VERSION.fullmatch(installed) or not _VERSION.fullmatch(latest):
            raise ToolUpdateError('invalid npm version data')
        return Checked(installed, latest, method)
    cask = kind == 'brew_cask'
    flag = '--cask' if cask else '--formula'
    raw = _run([installer, 'outdated', flag, *(['--greedy'] if cask else []),
                '--json=v2', package], CHECK_TIMEOUT_S, runner)
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ToolUpdateError('invalid brew JSON') from exc
    rows = data.get('casks' if cask else 'formulae') if isinstance(data, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ToolUpdateError('invalid brew outdated data')
    if not rows:
        version = _brew_installed_version(resolved, method, listdir)
        return Checked(version, version, method)
    matches = [row for row in rows if row.get('name') == package]
    if len(matches) != 1 or len(rows) != 1:
        raise ToolUpdateError('unexpected brew package')
    row = matches[0]
    installed = row.get('installed_versions')
    latest = row.get('current_version')
    if (not isinstance(installed, list) or not installed
            or any(not isinstance(v, str) or not v.strip() for v in installed)
            or not isinstance(latest, str) or not latest.strip()
            or not isinstance(row.get('auto_updates', False), bool)):
        raise ToolUpdateError('invalid brew version data')
    current = path_version(resolved)
    current = current if current in installed else installed[-1]
    return Checked(current, latest, method, row.get('auto_updates', False))


def upgrade(tool, *, checked=None, binary_path=None, runner=subprocess.run,
            which=context.which_tool, realpath=os.path.realpath, listdir=os.listdir):
    method = install_method(tool, binary_path or which(tool), realpath=realpath, listdir=listdir)
    if method is None or method[0] == 'native':
        return False
    if checked is not None and checked.method != method:
        raise ToolUpdateError('installer changed since check')
    kind, package = method
    installer = which('npm' if kind == 'npm' else 'brew')
    if not installer:
        raise ToolUpdateError('installer unavailable')
    if kind == 'npm':
        argv = [installer, 'install', '-g', package + '@latest']
    else:
        greedy = ['--greedy'] if kind == 'brew_cask' and checked and checked.auto_updates else []
        argv = [installer, 'upgrade', '--cask' if kind == 'brew_cask' else '--formula', *greedy, package]
    _run(argv, UPGRADE_TIMEOUT_S, runner)
    return True



def installation_in_progress(data, tool, *, clock=now):
    """An installer may have replaced its entrypoint before finishing the rest of its files."""
    records = data.get('tool_updates')
    record = records.get(tool) if isinstance(records, dict) else None
    stamp = record.get('installing_until') if isinstance(record, dict) else None
    until = parse_iso(stamp) if isinstance(stamp, str) else None
    return until is not None and clock() < until

def run_updates(ctx, *, runner=subprocess.run, clock=now, which=context.which_tool,
                realpath=os.path.realpath, listdir=os.listdir, notify=None):
    """Reserve under the store lock, then check/install outside it. Failures stay in state only."""
    for tool in ('claude', 'codex'):
        at = clock()
        with ctx.locked():
            state = ctx.load_state()
            if not state.settings().get('update_tools', True):
                return
            records = state.data.get('tool_updates')
            records = records if isinstance(records, dict) else {}
            previous = records.get(tool)
            previous = previous if isinstance(previous, dict) else {}
            stamp = previous.get('checked_at')
            checked_at = parse_iso(stamp) if isinstance(stamp, str) else None
            if checked_at and (at - checked_at).total_seconds() < CHECK_INTERVAL_S:
                continue
            record = {key: previous.get(key) for key in
                      ('installed', 'latest', 'method', 'error', 'updated_at')}
            record.update(checked_at=iso(at), error=None)
            records[tool] = record
            state.data['tool_updates'] = records
            state.save()
        upgraded = False
        try:
            binary = which(tool)
            method = install_method(tool, binary, realpath=realpath, listdir=listdir)
            record.update(method=method[0] if method else None)
            found = check(tool, binary_path=binary, runner=runner, which=which,
                          realpath=realpath, listdir=listdir) if binary else None
            if found:
                record.update(installed=found.installed, latest=found.latest)
                if found.available:
                    # A settings click during a slow check takes effect before installation.
                    with ctx.locked():
                        fresh = ctx.load_state()
                        enabled = fresh.settings().get('update_tools', True)
                        if enabled:
                            # A short lease survives an app crash without blocking sessions forever.
                            fresh.data['tool_updates'][tool]['installing_until'] = iso(
                                clock() + timedelta(seconds=UPGRADE_TIMEOUT_S + 60))
                            fresh.save()
                    if enabled:
                        upgraded = upgrade(tool, checked=found, binary_path=binary, runner=runner,
                                           which=which, realpath=realpath, listdir=listdir)
                        if upgraded:
                            record.update(installed=found.latest, updated_at=iso(clock()))
        except Exception as exc:
            record['error'] = (str(exc) if isinstance(exc, ToolUpdateError)
                               else f'{type(exc).__name__} during tool maintenance')
        with ctx.locked():
            state = ctx.load_state()
            state.data['tool_updates'][tool] = record
            state.save()
        if upgraded and notify:
            try:
                notify(f'{"claude code" if tool == "claude" else tool} updated to {record["installed"]}')
            except Exception:
                pass  # A notification failure must not repeat a successful installation.


_WORKER_LOCK = threading.Lock()


def start_updates(ctx, *, notify=None, thread_factory=threading.Thread, worker=run_updates):
    """Timer entry point: one daemon worker, reserved before launch, never from usage polling."""
    if not _WORKER_LOCK.acquire(blocking=False):
        return False
    try:
        if not ctx.load_state().settings().get('update_tools', True):
            _WORKER_LOCK.release()
            return False
        def work():
            try:
                worker(ctx, notify=notify)
            except Exception:
                pass  # Maintenance never produces an alert, even when the store is unavailable.
            finally:
                _WORKER_LOCK.release()
        thread_factory(target=work, daemon=True).start()
        return True
    except Exception:
        _WORKER_LOCK.release()
        return False
