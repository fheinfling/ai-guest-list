"""Review regressions: isolated contexts, fake HTTP and reserved fixture identities only."""
import json
import threading
import io
import urllib.error
from email.message import Message

import pytest

from acctsw import accounts, claudehome, claudesync, identity, usage
from acctsw.state import State
from test_claude_convergence import A, B, blob, profile, seat, sync


@pytest.fixture
def ticks(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(claudesync, 'monotonic', lambda: clock[0], raising=False)
    return clock


@pytest.mark.parametrize('failure', [429, 401, 503, 0, 'timeout'])
@pytest.mark.parametrize('direction', ['stock', 'home'])
def test_unknown_refresh_retries_unchanged_bytes_then_converges(ctx, profile, ticks,
                                                             monkeypatch, failure, direction):
    old = seat(ctx, profile)
    sync(ctx)
    fresh = blob('fake-retry-refresh', 50)  # changed copy wins even with an earlier expiry
    source = ctx.cred['claude'] if direction == 'stock' else ctx.claude_cred(A)
    source.set_live(fresh)
    calls = []
    def get(*_):
        assert not ctx._store_lock_held()
        calls.append(ticks[0])
        if len(calls) == 1:
            if failure == 'timeout':
                raise TimeoutError('fake private transport failure')
            return failure, ''
        return 200, json.dumps({'account': {'email': A.upper()}})
    monkeypatch.setattr(usage, '_default_get', get)
    claudesync.converge(ctx)
    assert ctx.snapshot_get('claude', A) == old
    assert (ctx.claude_cred(A) if direction == 'stock' else ctx.cred['claude']).get_live() == old
    ticks[0] = 2.9
    claudesync.converge(ctx)
    assert len(calls) == 1
    ticks[0] = 3
    claudesync.converge(ctx)
    assert calls == [0, 3]
    assert ctx.cred['claude'].get_live() == ctx.claude_cred(A).get_live() == fresh
    assert ctx.snapshot_get('claude', A) == fresh
    work = []
    real_lock, real_snapshot = ctx.locked, ctx.snapshot_get
    def locked(*args, **kwargs):
        work.append('lock')
        return real_lock(*args, **kwargs)
    def snapshot(*args):
        work.append('snapshot')
        return real_snapshot(*args)
    monkeypatch.setattr(ctx, 'locked', locked)
    monkeypatch.setattr(ctx, 'snapshot_get', snapshot)
    ticks[0] = 900
    claudesync.converge(ctx)
    assert len(calls) == 2
    assert work == []


def test_dead_owner_backs_off_to_five_minutes_without_linking(ctx, profile, ticks, monkeypatch):
    old = seat(ctx, profile)
    sync(ctx)
    ctx.cred['claude'].set_live(blob('fake-dead-owner', 900))
    calls = []
    def get(*_):
        calls.append(ticks[0])
        return 401, ''
    monkeypatch.setattr(usage, '_default_get', get)
    for tick in range(0, 1501, 3):
        ticks[0] = tick
        claudesync.converge(ctx)
    assert calls == [0, 3, 9, 21, 45, 93, 189, 381, 681, 981, 1281]
    assert ctx.claude_cred(A).get_live() == ctx.snapshot_get('claude', A) == old
    assert not ctx.load_state().get_seat('claude', A).get('auth_error')


def test_changed_bytes_reset_unknown_backoff(ctx, profile, ticks):
    seat(ctx, profile)
    sync(ctx)
    ctx.cred['claude'].set_live(blob('fake-dead', 900))
    claudesync.converge(ctx)
    ticks[0] = 1
    fresh = blob('fake-replacement', 50)
    profile[0]['fake-replacement'] = A
    ctx.cred['claude'].set_live(fresh)
    claudesync.converge(ctx)
    assert ctx.claude_cred(A).get_live() == fresh


def test_replaced_unknown_item_starts_a_fresh_retry_schedule(ctx, profile, ticks):
    old = seat(ctx, profile)
    sync(ctx)
    ctx.cred['claude'].set_live(blob('fake-first-dead', 900))
    claudesync.converge(ctx)
    ticks[0] = 3
    claudesync.converge(ctx)
    fresh = blob('fake-next-unknown', 50)
    ticks[0] = 4
    ctx.cred['claude'].set_live(fresh)
    claudesync.converge(ctx)
    count = len(profile[1])
    profile[0]['fake-next-unknown'] = A
    ticks[0] = 6
    claudesync.converge(ctx)
    assert len(profile[1]) == count
    assert ctx.snapshot_get('claude', A) == old
    ticks[0] = 7
    claudesync.converge(ctx)
    assert len(profile[1]) == count + 1
    assert ctx.claude_cred(A).get_live() == ctx.snapshot_get('claude', A) == fresh


@pytest.mark.parametrize('retry_after,deadline', [
    ('45', 45), ('600', 600), ('Thu, 01 Jan 1970 00:00:45 GMT', 45),
    ('0', 3), ('-1', 3), ('nan', 3), ('inf', 3), ('fake-invalid-date', 3),
    ('Wed, 31 Dec 1969 23:59:59 GMT', 3)])
def test_transport_retry_after_defers_owner_retry(ctx, profile, ticks, monkeypatch,
                                                retry_after, deadline):
    import time
    seat(ctx, profile)
    sync(ctx)
    fresh = blob('fake-rate-limited', 900)
    ctx.cred['claude'].set_live(fresh)
    profile[0]['fake-rate-limited'] = A
    calls = []
    class Opener:
        def open(self, *_args, **_kwargs):
            calls.append(ticks[0])
            headers = Message()
            headers['Retry-After'] = retry_after
            raise urllib.error.HTTPError('https://example.test', 429, '', headers, io.BytesIO(b''))
    monkeypatch.setattr(time, 'time', lambda: 0)
    monkeypatch.setattr(usage.urllib.request, 'build_opener', lambda *_: Opener())
    monkeypatch.setattr(usage, '_default_get', ORIGINAL_GET)
    claudesync.converge(ctx)
    ticks[0] = deadline - 1
    claudesync.converge(ctx)
    assert calls == [0]
    ticks[0] = deadline
    # Switch the fake transport to success while keeping the existing retry deadline.
    monkeypatch.setattr(usage, '_default_get', lambda *_:
        (200, json.dumps({'account': {'email': A}})))
    claudesync.converge(ctx)
    assert ctx.claude_cred(A).get_live() == fresh


ORIGINAL_GET = usage._default_get


@pytest.mark.parametrize('retry_after,expected', [
    (None, None), ('fake-invalid-date', None), ('-1', None), ('nan', None),
    ('inf', None), ('0', 0.0), ('0.5', 0.5), ('45', 45.0)])
def test_transport_keeps_success_body_and_validates_retry_metadata(ctx, profile, monkeypatch,
                                                                retry_after, expected):
    value = seat(ctx, profile)
    headers = Message()
    if retry_after is not None:
        headers['Retry-After'] = retry_after
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def read(self):
            # Invalid UTF-8 in an unused field must not discard a valid account.email.
            return b'{"account":{"email":"seat-a@example.test"},"unused":"\xff"}'
    Response.headers = headers
    class Opener:
        def open(self, *_args, **_kwargs):
            return Response()
    monkeypatch.setattr(usage.urllib.request, 'build_opener', lambda *_: Opener())
    reply = ORIGINAL_GET('https://api.anthropic.com/api/oauth/profile', {}, 10)
    assert reply[0] == 200
    assert reply.retry_after == expected
    answer = identity.claude_live_identity(ctx, get=ORIGINAL_GET)
    assert answer.email == A
    assert answer.blob == value


def test_newest_empty_is_unknown():
    assert claudesync.newest([]) is None


def test_converge_saves_once_for_quarantine_mirror_and_snapshot(ctx, profile, monkeypatch):
    original = seat(ctx, profile)
    seat(ctx, profile, B, 'fake-b')
    sync(ctx)
    saves = []
    real = State.save
    def save(state):
        assert ctx._store_lock_held()
        saves.append(True)
        return real(state)
    monkeypatch.setattr(State, 'save', save)
    fresh = blob('fake-foreign-refresh', 900)
    profile[0]['fake-foreign-refresh'] = B
    ctx.claude_cred(A).set_live(fresh)
    claudesync.converge(ctx)
    assert len(saves) == 1
    state = ctx.load_state()
    assert state.get_seat('claude', A)['auth_error']
    assert (ctx.claude_home(A) / '.acctsw-quarantined').exists()
    assert ctx.cred['claude'].get_live() == ctx.claude_cred(B).get_live() == fresh
    assert ctx.snapshot_get('claude', B) == fresh
    assert ctx.snapshot_get('claude', A) == original
    assert state.data['tools']['claude']['captured_stock_digest'] == accounts._claude_digest(fresh)


def test_quarantine_is_saved_if_later_propagation_fails(ctx, profile, monkeypatch):
    seat(ctx, profile)
    seat(ctx, profile, B, 'fake-b')
    sync(ctx)
    fresh = blob('fake-foreign-failed-write', 900)
    profile[0]['fake-foreign-failed-write'] = B
    ctx.claude_cred(A).set_live(fresh)
    def fail(*_):
        raise OSError('fake write failure')
    monkeypatch.setattr(ctx.keychain, 'set_raw', fail)
    claudesync.converge(ctx)
    assert ctx.load_state().get_seat('claude', A)['auth_error']
    assert (ctx.claude_home(A) / '.acctsw-quarantined').exists()


def test_cached_identity_reads_both_maps_under_tracker_lock(ctx):
    t = claudesync.tracker(ctx)
    local = threading.local()
    class Lock:
        def __enter__(self):
            local.held = True
        def __exit__(self, *_):
            local.held = False
    class Checked(dict):
        def get(self, key, *args):
            assert getattr(local, 'held', False), 'cache read requires tracker lock'
            return super().get(key, *args)
    t.lock = Lock()
    t.proofs = Checked()
    t.seen = Checked({None: claudesync.Seen(None, None)})
    assert claudesync.cached_identity(ctx) == identity.ClaudeLiveIdentity(None, None)


@pytest.mark.parametrize('operation', ['add', 'reconcile'])
def test_seat_matching_uses_same_unicode_casefold_as_convergence(ctx, profile, operation):
    email, owner = 'seat-straße@example.test', 'SEAT-STRASSE@EXAMPLE.TEST'
    seat(ctx, profile, email, 'fake-unicode-seat')
    fresh = blob('fake-unicode-login', 900)
    profile[0]['fake-unicode-login'] = owner
    ctx.cred['claude'].set_live(fresh)
    if operation == 'add':
        added = accounts.add(ctx, ctx.load_state(), 'claude')
        assert added['email'] == email
    else:
        claudesync.converge(ctx)
        assert accounts.reconcile_claude(ctx, ctx.load_state(),
            live_identity=identity.ClaudeLiveIdentity(fresh, owner)) == email
    assert list(ctx.load_state().accounts('claude')) == [email]
    assert ctx.snapshot_get('claude', email) == fresh
    assert not ctx.load_state().get_seat('claude', email).get('auth_error')


@pytest.mark.parametrize('direction', ['stock', 'home'])
def test_format_only_rewrite_propagates_once_without_flags(ctx, profile, monkeypatch, direction):
    old = seat(ctx, profile)
    sync(ctx)
    profile[1].clear()
    data = json.loads(old)
    data['claudeAiOauth']['subscriptionTypeSource'] = 'fake-profile'
    fresh = json.dumps(data, indent=2)
    source = ctx.cred['claude'] if direction == 'stock' else ctx.claude_cred(A)
    source.set_live(fresh)
    writes = []
    real = ctx.keychain.set_raw
    def write(*args):
        if args[0] in (ctx.cred['claude'].service, ctx.claude_cred(A).service):
            writes.append(True)
        return real(*args)
    monkeypatch.setattr(ctx.keychain, 'set_raw', write)
    for _ in range(5):
        sync(ctx)
    assert writes == [True]
    assert profile[1] == ['fake-a']
    assert ctx.cred['claude'].get_live() == ctx.claude_cred(A).get_live() == fresh
    assert ctx.snapshot_get('claude', A) == fresh
    assert not ctx.load_state().get_seat('claude', A).get('auth_error')
    assert not (ctx.claude_home(A) / '.acctsw-quarantined').exists()
    assert not (ctx.claude_home(A) / '.acctsw-unverified').exists()
