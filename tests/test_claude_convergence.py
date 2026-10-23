"""Owner-proven convergence and migration using fake tokens and reserved test addresses only."""
import json
from contextlib import contextmanager

import pytest

from acctsw import accounts, bridge, claudehome, identity, launcher, usage
from acctsw.errors import CredentialIdentityMismatch, MissingSnapshot
from acctsw.keychain import KeychainError

A, B = 'seat-a@example.test', 'seat-b@example.test'


def blob(token, expires=100, refresh=None):
    return json.dumps({'claudeAiOauth': {'accessToken': token,
        'refreshToken': refresh or 'fake-refresh-' + token, 'expiresAt': expires,
        'scopes': ['user:inference'], 'subscriptionType': 'max'}})


@pytest.fixture
def profile(ctx, monkeypatch):
    owners, calls = {}, []
    ctx._fake_profile_calls = calls
    ctx._real_usage_refresh = usage.refresh_live
    monkeypatch.setattr(usage, 'claude_user_agent', lambda *_: 'claude-code/fake')
    def get(url, headers, timeout):
        if getattr(ctx, '_store_lock_held', lambda: False)():
            pytest.fail('profile ran under store lock')
        assert url == 'https://api.anthropic.com/api/oauth/profile'
        assert timeout == 10.0
        token = headers['Authorization'].removeprefix('Bearer ')
        calls.append(token)
        owner = owners.get(token)
        return (200, json.dumps({'account': {'email': owner}})) if owner else (401, 'fake expired')
    monkeypatch.setattr(usage, '_default_get', get)
    monkeypatch.setattr(usage, 'refresh_live', lambda *_a, **_kw: {'claude': {}})
    if hasattr(identity, 'claude_status_email'):
        # Old-branch fail-first copies must never invoke a real CLI/Keychain.
        monkeypatch.setattr(identity, 'claude_status_email', lambda *_a, **_kw: A)
    monkeypatch.setattr(usage, '_default_post', lambda *_: pytest.fail('no refresh grant allowed'))
    return owners, calls


def seat(ctx, profile, email=A, token='fake-a', expires=100):
    profile[0][token] = email
    value = blob(token, expires)
    ctx.cred['claude'].set_live(value)
    accounts.add(ctx, ctx.load_state(), 'claude', email=email)
    return value


def sync(ctx, **kwargs):
    # Use an existing public boundary, so this behavioral suite also runs on the old branch.
    result = bridge.handle(ctx, {'action': 'usage', 'tool': 'claude', **kwargs})
    assert result.get('ok') is True, result
    assert ctx._fake_profile_calls, 'ownership must be established through the profile endpoint'


@pytest.mark.parametrize('direction', ['stock', 'home'])
def test_changed_same_account_item_converges_both_directions_and_snapshot(ctx, profile, direction):
    old = seat(ctx, profile)
    other = seat(ctx, profile, B, 'fake-b')
    ctx.cred['claude'].set_live(old)
    sync(ctx)
    profile[1].clear()
    fresh = blob('fake-a-new', 200)
    profile[0]['fake-a-new'] = A
    source = ctx.cred['claude'] if direction == 'stock' else ctx.claude_cred(A)
    source.set_live(fresh)
    sync(ctx)
    assert profile[1] == ['fake-a-new']
    assert ctx.cred['claude'].get_live() == fresh
    assert ctx.claude_cred(A).get_live() == fresh
    assert ctx.snapshot_get('claude', A) == fresh
    assert ctx.claude_cred(B).get_live() == other
    assert ctx.snapshot_get('claude', B) == other
    sync(ctx)
    assert profile[1] == ['fake-a-new']  # propagating does not trigger a second HTTP call


def test_only_changed_copy_wins_even_if_new_login_has_earlier_expiry(ctx, profile):
    seat(ctx, profile)
    sync(ctx)
    fresh = blob('fake-new-login', 50)
    profile[0]['fake-new-login'] = A
    ctx.claude_cred(A).set_live(fresh)
    sync(ctx)
    assert ctx.cred['claude'].get_live() == fresh


@pytest.mark.parametrize('first,second,expected', [(200,300,'home'), (300,200,'stock'),
                                                 (300,300,None), (None,300,None),
                                                 (float('inf'),300,None), (True,300,None)])
def test_both_changed_use_unique_later_expiry_or_leave_ambiguous(ctx, profile, first, second, expected):
    old = seat(ctx, profile)
    sync(ctx)
    stock, home = blob('fake-stock', first), blob('fake-home', second)
    profile[0].update({'fake-stock': A, 'fake-home': A})
    ctx.cred['claude'].set_live(stock)
    ctx.claude_cred(A).set_live(home)
    sync(ctx)
    if expected:
        winner = home if expected == 'home' else stock
        assert ctx.cred['claude'].get_live() == ctx.claude_cred(A).get_live() == winner
        assert ctx.snapshot_get('claude', A) == winner
    else:
        assert ctx.cred['claude'].get_live() == stock
        assert ctx.claude_cred(A).get_live() == home
        assert ctx.snapshot_get('claude', A) == old


def test_restart_reproves_every_item_and_selects_later_expiry(ctx, profile):
    seat(ctx, profile)
    fresh = blob('fake-new', 200)
    profile[0]['fake-new'] = A
    ctx.claude_cred(A).set_live(fresh)
    sync(ctx)
    assert ctx.cred['claude'].get_live() == fresh
    assert sorted(profile[1]) == ['fake-a', 'fake-new']


@pytest.mark.parametrize('target', ['stock', 'home', 'snapshot'])
def test_cas_refuses_target_changed_after_observation(ctx, profile, monkeypatch, target):
    old = seat(ctx, profile)
    sync(ctx)
    fresh, raced = blob('fake-new', 200), blob('fake-race', 300)
    profile[0].update({'fake-new': A, 'fake-race': A})
    source = ctx.claude_cred(A) if target == 'stock' else ctx.cred['claude']
    source.set_live(fresh)
    real_locked = ctx.locked
    changed = []
    @contextmanager
    def locked():
        if not changed:
            changed.append(True)
            if target == 'snapshot':
                ctx.snapshot_set('claude', A, raced)
            else:
                (ctx.cred['claude'] if target == 'stock' else ctx.claude_cred(A)).set_live(raced)
        with real_locked():
            yield
    monkeypatch.setattr(ctx, 'locked', locked)
    sync(ctx)
    value = ctx.snapshot_get('claude', A) if target == 'snapshot' else (
        ctx.cred['claude'] if target == 'stock' else ctx.claude_cred(A)).get_live()
    assert value == raced
    monkeypatch.setattr(ctx, 'locked', real_locked)
    if target != 'snapshot':
        sync(ctx)  # the rejected writer's bytes become new evidence, never ignored
        assert ctx.cred['claude'].get_live() == raced
        assert ctx.claude_cred(A).get_live() == raced


def test_cas_refuses_source_changed_during_commit(ctx, profile, monkeypatch):
    old = seat(ctx, profile)
    sync(ctx)
    profile[0]['fake-new'] = A
    ctx.cred['claude'].set_live(blob('fake-new', 200))
    real_locked = ctx.locked
    @contextmanager
    def locked():
        ctx.cred['claude'].set_live(blob('fake-unproven-race', 300))
        with real_locked():
            yield
    monkeypatch.setattr(ctx, 'locked', locked)
    sync(ctx)
    assert ctx.claude_cred(A).get_live() == old
    assert ctx.snapshot_get('claude', A) == old


def test_profile_answer_about_replaced_bytes_cannot_propagate(ctx, profile, monkeypatch):
    old = seat(ctx, profile)
    sync(ctx)
    fresh, race = blob('fake-new', 200), blob('fake-race', 300)
    ctx.cred['claude'].set_live(fresh)
    real = identity.claude_live_identity
    def resolve(c, *, email=None):
        answer = real(c, email=email)
        if email is None:
            c.cred['claude'].set_live(race)
        return answer
    profile[0]['fake-new'] = A
    monkeypatch.setattr(identity, 'claude_live_identity', resolve)
    sync(ctx)
    assert ctx.claude_cred(A).get_live() == old


def test_different_proven_owners_never_link_even_with_newer_expiry(ctx, profile):
    old = seat(ctx, profile)
    other = seat(ctx, profile, B, 'fake-b', 500)
    sync(ctx)
    assert ctx.cred['claude'].get_live() == other
    assert ctx.claude_cred(A).get_live() == old
    assert ctx.snapshot_get('claude', A) == old


@pytest.mark.parametrize('unknown', ['stock', 'home'])
def test_unknown_owner_never_propagates_in_either_direction(ctx, profile, unknown):
    old = seat(ctx, profile)
    sync(ctx)
    fresh = blob('fake-unknown', 900)
    (ctx.cred['claude'] if unknown == 'stock' else ctx.claude_cred(A)).set_live(fresh)
    sync(ctx)
    assert ctx.cred['claude'].get_live() == (fresh if unknown == 'stock' else old)
    assert ctx.claude_cred(A).get_live() == (fresh if unknown == 'home' else old)
    assert ctx.snapshot_get('claude', A) == old


def legacy(ctx, value):
    state = ctx.load_state()
    state.upsert_seat('claude', A)
    state.set_active('claude', A)
    ctx.snapshot_set('claude', A, value)
    state.save()


def test_migration_wrong_owner_is_not_seeded_and_flagged(ctx, profile):
    value = blob('fake-wrong')
    profile[0]['fake-wrong'] = B
    legacy(ctx, value)
    with pytest.raises(CredentialIdentityMismatch):
        claudehome.ensure_home(ctx, A)
    assert ctx.claude_cred(A).get_live() is None
    assert ctx.load_state().get_seat('claude', A)['auth_error']
    assert accounts.list_seats(ctx.load_state(), 'claude')[0]['needs_login']


@pytest.mark.parametrize('owner', [A, B])
def test_unknown_seed_verified_after_first_child_use_and_quarantined_on_mismatch(ctx, profile, owner):
    expired = blob('fake-expired')
    legacy(ctx, expired)
    def spawn(argv, output, on_tick=None, *, env=None):
        assert ctx.claude_cred(A).get_live() == expired
        assert (ctx.claude_home(A) / '.acctsw-unverified').exists()
        # Successful child refresh, no acctsw grant.
        profile[0]['fake-refreshed'] = owner
        ctx.claude_cred(A).set_live(blob('fake-refreshed', 300))
        return 0
    assert launcher.run(ctx, 'claude', [], spawn=spawn, get=lambda *_: (0,'')) == 0
    if owner == B:
        assert ctx.load_state().get_seat('claude', A)['auth_error']
        assert claudehome.load(ctx, A) is None
        assert not (ctx.claude_home(A) / '.acctsw-unverified').exists()
        assert (ctx.claude_home(A) / '.acctsw-quarantined').stat().st_mode & 0o777 == 0o600
        assert ctx.snapshot_get('claude', A) == expired
        with pytest.raises(CredentialIdentityMismatch):
            claudehome.ensure_home(ctx, A)
    else:
        assert claudehome.load(ctx, A) == blob('fake-refreshed', 300)
        assert ctx.snapshot_get('claude', A) == blob('fake-refreshed', 300)
        assert not (ctx.claude_home(A) / '.acctsw-unverified').exists()


def test_unknown_seed_retried_after_child_use_even_if_bytes_stayed_identical(ctx, profile, monkeypatch):
    value = blob('fake-initially-unknown')
    legacy(ctx, value)
    merge = claudehome.merge_config
    def merged(c,email,**kwargs):
        if profile[0].get('fake-initially-unknown') == B:
            assert c.load_state().get_seat('claude',A).get('auth_error'), 'owner must be verified before post-child config merge'
        return merge(c,email,**kwargs)
    monkeypatch.setattr(claudehome,'merge_config',merged)
    def spawn(*a, **kw):
        assert (ctx.claude_home(A) / '.acctsw-unverified').exists()
        profile[0]['fake-initially-unknown'] = B
        return 0
    assert launcher.run(ctx, 'claude', [], spawn=spawn, get=lambda *_: (0,'')) == 0
    assert ctx.load_state().get_seat('claude', A)['auth_error']


@pytest.mark.parametrize('stock_owner,stock_exp,snap_exp,expected', [
    (A,300,100,'stock'), (A,100,300,'snapshot'), (B,300,100,'snapshot'),
    (A,100,100,'snapshot'), (A,None,100,'snapshot'), (None,300,100,'snapshot')])
def test_migration_prefers_only_proven_newer_stock(ctx, profile, stock_owner, stock_exp, snap_exp, expected):
    saved, stock = blob('fake-saved', snap_exp), blob('fake-stock', stock_exp)
    legacy(ctx, saved)
    profile[0].update({'fake-saved': A, 'fake-stock': stock_owner})
    ctx.cred['claude'].set_live(stock)
    claudehome.ensure_home(ctx, A)
    assert ctx.claude_cred(A).get_live() == (stock if expected == 'stock' else saved)
    assert profile[1] == ['fake-saved', 'fake-stock']
    assert ctx.snapshot_get('claude',A) == (stock if expected == 'stock' else saved)
    assert not (ctx.claude_home(A) / '.acctsw-unverified').exists()


def test_migration_plan_race_cannot_seed_stale_snapshot(ctx, profile):
    legacy(ctx, blob('fake-initial'))
    claudehome.prepare(ctx, A)
    ctx.snapshot_set('claude', A, blob('fake-replaced'))
    with ctx.locked(), pytest.raises(MissingSnapshot):
        claudehome.ensure_home(ctx, A)
    assert ctx.claude_cred(A).get_live() is None


def test_home_identity_ignores_matching_or_foreign_config_label(ctx, profile):
    seat(ctx, profile)
    config = ctx.claude_home(A) / '.claude.json'
    for label, token, actual in [(B,'fake-a',A),(A,'fake-foreign',B)]:
        config.write_text(json.dumps({'oauthAccount': {'emailAddress': label}}))
        profile[0][token] = actual
        value = blob(token)
        ctx.claude_cred(A).set_live(value)
        assert identity.claude_live_identity(ctx, email=A) == identity.ClaudeLiveIdentity(value, actual)
    sync(ctx)
    assert claudehome.load(ctx, A) is None
    assert ctx.load_state().get_seat('claude', A)['auth_error']


@pytest.mark.parametrize('mode', ['exception', 'body', 'write'])
def test_convergence_failures_do_not_surface_tokens_or_profile_data(ctx, profile, monkeypatch, caplog, capsys, mode):
    seat(ctx, profile)
    sync(ctx)
    secret = 'fake-private-token'
    private = json.dumps({'account':{'email': A, 'name':'fake-name', 'uuid':'fake-uuid'},
                          'organization':{'name':'fake-org'}, 'echo':secret})
    ctx.cred['claude'].set_live(blob(secret, 500))
    def get(*_):
        if mode == 'exception':
            raise RuntimeError(private)
        return (401 if mode == 'body' else 200), private
    monkeypatch.setattr(usage, '_default_get', get)
    if mode == 'write':
        def fail(*_):
            raise KeychainError(private)
        monkeypatch.setattr(ctx.keychain, 'set_raw', fail)
    result = bridge.handle(ctx, {'action':'claude_sync'})
    output = repr(result) + caplog.text + repr(capsys.readouterr()) + ctx.state_file.read_text()
    for text in (secret, 'fake-name', 'fake-uuid', 'fake-org', private):
        assert text not in output


def test_migration_reuses_proof_for_unchanged_bytes_and_refreshes_plan_on_change(ctx, profile):
    saved, stock = blob('fake-saved'), blob('fake-stock', 50)
    legacy(ctx, saved)
    profile[0].update({'fake-saved':A,'fake-stock':A})
    ctx.cred['claude'].set_live(stock)
    claudehome.prepare(ctx, A)
    claudehome.prepare(ctx, A)
    assert profile[1] == ['fake-saved','fake-stock']
    ctx.cred['claude'].set_live(blob('fake-stock-new', 500))
    profile[0]['fake-stock-new'] = A
    claudehome.prepare(ctx, A)
    assert profile[1] == ['fake-saved','fake-stock','fake-saved','fake-stock-new']


def test_readd_clears_quarantine_and_pending_verification(ctx, profile):
    old = seat(ctx, profile)
    home = ctx.claude_home(A)
    (home / '.acctsw-quarantined').write_text('1')
    (home / '.acctsw-unverified').write_text('1')
    state = ctx.load_state()
    state.get_seat('claude',A)['auth_error'] = True
    state.save()
    accounts.add(ctx, state, 'claude', email=A, blob=old)
    assert not (home / '.acctsw-quarantined').exists()
    assert not (home / '.acctsw-unverified').exists()
    assert not state.get_seat('claude',A).get('auth_error')
    assert claudehome.load(ctx,A) == old


def test_sync_back_clears_unverified_only_for_proven_exact_owner(ctx, profile):
    value = seat(ctx,profile)
    pending = ctx.claude_home(A) / '.acctsw-unverified'
    pending.write_text('1')
    assert claudehome.sync_back(ctx,ctx.load_state(),A,
        live_identity=identity.ClaudeLiveIdentity(value,A))
    assert not pending.exists()


def test_removed_seat_cannot_be_recreated_by_a_foreign_identity_answer(ctx, profile):
    seat(ctx,profile)
    sync(ctx)
    state = ctx.load_state()
    value = ctx.claude_cred(A).get_live()
    state.remove_seat('claude',A)
    assert not claudehome.sync_back(ctx,state,A,
        live_identity=identity.ClaudeLiveIdentity(value,B))
    assert not (ctx.claude_home(A) / '.acctsw-quarantined').exists()


@pytest.mark.parametrize('operation', ['item-read','item-write','snapshot-read','snapshot-write'])
def test_credential_backend_errors_never_expose_payload(ctx, profile, monkeypatch, operation):
    seat(ctx,profile)
    secret = 'fake-private-keychain-payload'
    def fail(*_):
        raise KeychainError(secret)
    if operation == 'item-read':
        monkeypatch.setattr(ctx.keychain,'get_raw',fail)
        call = ctx.cred['claude'].get_live
    elif operation == 'item-write':
        monkeypatch.setattr(ctx.keychain,'set_raw',fail)
        call = lambda:ctx.claude_cred(A).set_live(blob('fake-new'))
    elif operation == 'snapshot-read':
        monkeypatch.setattr(ctx.keychain,'get',fail)
        call = lambda:ctx.snapshot_get('claude',A)
    else:
        monkeypatch.setattr(ctx.keychain,'set',fail)
        call = lambda:ctx.snapshot_set('claude',A,blob('fake-new'))
    with pytest.raises(KeychainError) as raised:
        call()
    assert secret not in str(raised.value)
    assert raised.value.__suppress_context__


def test_failed_write_retries_without_losing_changed_item_evidence(ctx, profile, monkeypatch):
    old = seat(ctx,profile)
    sync(ctx)
    fresh = blob('fake-relogin',50)
    profile[0]['fake-relogin'] = A
    ctx.cred['claude'].set_live(fresh)
    real = ctx.keychain.set_raw
    def fail(*_):
        raise KeychainError('fake private failure')
    monkeypatch.setattr(ctx.keychain,'set_raw',fail)
    sync(ctx)
    assert ctx.claude_cred(A).get_live() == old
    monkeypatch.setattr(ctx.keychain,'set_raw',real)
    sync(ctx)
    assert ctx.claude_cred(A).get_live() == fresh
    assert ctx.snapshot_get('claude',A) == fresh
    assert profile[1].count('fake-relogin') == 1


def test_unknown_migration_does_not_rewrite_snapshot(ctx, profile, monkeypatch):
    saved = blob('fake-expired')
    legacy(ctx,saved)
    writes = []
    real = ctx.snapshot_set
    monkeypatch.setattr(ctx,'snapshot_set',lambda *a: (writes.append(a),real(*a))[-1])
    claudehome.ensure_home(ctx,A)
    assert profile[1] == ['fake-expired']
    assert writes == []
    assert ctx.claude_cred(A).get_live() == saved


def test_migration_snapshot_cas_preserves_a_concurrent_update(ctx, profile, monkeypatch):
    from acctsw.credlocations import ClaudeCredLocation
    saved, stock, raced = blob('fake-saved'),blob('fake-stock',500),blob('fake-raced',900)
    legacy(ctx,saved)
    profile[0].update({'fake-saved':A,'fake-stock':A})
    ctx.cred['claude'].set_live(stock)
    original = ClaudeCredLocation.set_live
    def write(item,value):
        original(item,value)
        if item.service == ctx.claude_cred(A).service:
            ctx.snapshot_set('claude',A,raced)
    monkeypatch.setattr(ClaudeCredLocation,'set_live',write)
    claudehome.ensure_home(ctx,A)
    assert profile[1] == ['fake-saved','fake-stock']
    assert ctx.snapshot_get('claude',A) == raced


@pytest.mark.parametrize('entry', ['bridge','cli'])
def test_capture_never_trusts_a_supplied_email_label(ctx, profile, entry):
    from types import SimpleNamespace
    from acctsw import cli
    profile[0]['fake-login'] = A
    value = blob('fake-login')
    ctx.cred['claude'].set_live(value)
    if entry == 'bridge':
        result = bridge.handle(ctx,{'action':'snapshot','tool':'claude','email':B})
        assert result['ok'] and result['added'] == A
    else:
        assert cli._cmd_add(ctx,SimpleNamespace(tool='claude',email=B,name=None)) == 0
    assert ctx.snapshot_get('claude',A) == value
    assert ctx.snapshot_get('claude',B) is None
    assert ctx.claude_cred(B).get_live() is None
    assert profile[1] == ['fake-login']


def test_unknown_changed_owner_only_profiled_once_across_usage_polls(ctx, profile):
    seat(ctx,profile)
    sync(ctx)
    profile[1].clear()
    ctx.cred['claude'].set_live(blob('fake-unknown',700))
    for _ in range(3):
        sync(ctx)
    assert profile[1] == ['fake-unknown']


def test_manual_switch_migrates_landing_home_using_unlocked_owner_proof(ctx, profile):
    saved, landing = blob('fake-current'), blob('fake-landing')
    legacy(ctx,saved)
    profile[0].update({'fake-current':A,'fake-landing':B})
    ctx.cred['claude'].set_live(saved)
    state = ctx.load_state()
    state.upsert_seat('claude',B)
    ctx.snapshot_set('claude',B,landing)
    state.save()
    assert not ctx.claude_home(B).exists()
    result = bridge.handle(ctx,{'action':'switch','tool':'claude','email':B})
    assert result['ok'], result
    assert ctx.claude_cred(B).get_live() == landing
    assert ctx.load_state().active('claude') == B
    assert 'fake-landing' in profile[1]


def test_convergence_refuses_a_locked_network_boundary(ctx, profile):
    from acctsw import claudesync
    seat(ctx,profile)
    sync(ctx)
    with ctx.locked(), pytest.raises(RuntimeError,match='unlocked store'):
        claudesync.converge(ctx)


def test_quarantined_misfiled_home_still_converges_with_its_proven_owner(ctx, profile):
    original = seat(ctx,profile)
    seat(ctx,profile,B,'fake-b')
    sync(ctx)
    rogue = blob('fake-b-in-wrong-home',200)
    profile[0]['fake-b-in-wrong-home'] = B
    ctx.claude_cred(A).set_live(rogue)
    sync(ctx)
    assert claudehome.load(ctx,A) is None
    assert ctx.load_state().get_seat('claude',A)['auth_error']
    assert ctx.snapshot_get('claude',A) == original
    assert ctx.cred['claude'].get_live() == rogue
    assert ctx.claude_cred(B).get_live() == rogue
    assert ctx.snapshot_get('claude',B) == rogue
    newer = blob('fake-b-stock-new',300)
    profile[0]['fake-b-stock-new'] = B
    ctx.cred['claude'].set_live(newer)
    sync(ctx)
    assert ctx.claude_cred(A).get_live() == newer
    assert ctx.claude_cred(B).get_live() == newer
    assert ctx.snapshot_get('claude',A) == original
    assert ctx.snapshot_get('claude',B) == newer
    assert claudehome.load(ctx,A) is None


def test_stock_reconciliation_seeds_home_and_remembers_exact_bytes(ctx, profile):
    legacy(ctx,blob('fake-old'))
    fresh = blob('fake-captured',300)
    profile[0]['fake-captured'] = A
    ctx.cred['claude'].set_live(fresh)
    assert accounts.reconcile_claude(ctx,ctx.load_state()) == A
    assert ctx.claude_cred(A).get_live() == fresh
    assert ctx.claude_home(A).is_dir()
    assert profile[1] == ['fake-captured']
    profile[1].clear()
    assert accounts.reconcile_claude(ctx,ctx.load_state()) is None
    assert profile[1] == []


def test_non_session_launch_converges_before_exec(ctx, profile, monkeypatch):
    seat(ctx,profile)
    sync(ctx)
    fresh = blob('fake-management-refresh',300)
    profile[0]['fake-management-refresh'] = A
    ctx.cred['claude'].set_live(fresh)
    executed = []
    def execute(executable,argv,env):
        executed.append(argv)
        assert ctx.claude_cred(A).get_live() == fresh
        assert ctx.snapshot_get('claude',A) == fresh
    monkeypatch.setattr(launcher.os,'execvpe',execute)
    assert launcher.run(ctx,'claude',['agents','--json']) == 127
    assert executed == [['claude','agents','--json']]


def test_codex_launch_never_inspects_claude_credentials(ctx, profile, monkeypatch):
    from tests.conftest import make_codex_blob
    seat(ctx,profile)
    sync(ctx)
    ctx.cred['codex'].set_live(make_codex_blob(A))
    accounts.add(ctx,ctx.load_state(),'codex')
    def unexpected():
        pytest.fail('Codex launch inspected Claude credentials')
    monkeypatch.setattr(ctx.cred['claude'],'get_live',unexpected)
    assert launcher.run(ctx,'codex',[],spawn=lambda *_a,**_kw:0) == 0


def test_unknown_seed_gets_one_owner_retry_per_child_exit(ctx, profile):
    legacy(ctx,blob('fake-parked'))
    def spawn(*_a,**_kw):
        assert (ctx.claude_home(A) / '.acctsw-unverified').exists()
        profile[1].clear()
        return 0
    assert launcher.run(ctx,'claude',[],spawn=spawn) == 0
    assert profile[1] == ['fake-parked']


def test_verified_unchanged_child_exit_does_not_repeat_profile_request(ctx, profile):
    seat(ctx,profile)
    sync(ctx)
    def spawn(*_a,**_kw):
        profile[1].clear()
        return 0
    assert launcher.run(ctx,'claude',[],spawn=spawn) == 0
    assert profile[1] == []


def test_spawn_boundary_converges_a_refresh_during_command_build(ctx, profile, monkeypatch):
    seat(ctx,profile)
    sync(ctx)
    fresh = blob('fake-pre-spawn-refresh',400)
    profile[0]['fake-pre-spawn-refresh'] = A
    original = launcher.build_cmd
    def build(*a,**kw):
        argv = original(*a,**kw)
        ctx.cred['claude'].set_live(fresh)
        return argv
    monkeypatch.setattr(launcher,'build_cmd',build)
    def spawn(*a,**kw):
        assert ctx.claude_cred(A).get_live() == fresh
        return 0
    assert launcher.run(ctx,'claude',[],spawn=spawn) == 0


def test_wrong_snapshot_is_flagged_before_rest_wait_even_without_a_child(ctx, profile):
    from datetime import timedelta
    from acctsw.util import iso,now
    legacy(ctx,blob('fake-wrong-parked'))
    profile[0]['fake-wrong-parked'] = B
    state = ctx.load_state()
    state.set_limited_until('claude',A,iso(now()+timedelta(hours=1)))
    state.save()
    with pytest.raises(CredentialIdentityMismatch):
        launcher.run(ctx,'claude',[],spawn=lambda *_a,**_kw:pytest.fail('wrong owner launched'),
                     sleep=lambda *_:pytest.fail('wrong owner entered rest wait'),get=lambda *_:(401,''))
    assert ctx.claude_cred(A).get_live() is None
    assert ctx.load_state().get_seat('claude',A)['auth_error']


def test_each_returning_child_retries_its_pending_owner_verification(ctx, profile):
    from datetime import timedelta
    from acctsw.util import iso,now
    from tests.test_usage import claude_ok_body
    legacy(ctx,blob('fake-pending-a'))
    state = ctx.load_state()
    state.upsert_seat('claude',B)
    ctx.snapshot_set('claude',B,blob('fake-valid-b'))
    profile[0]['fake-valid-b'] = B
    state.save()
    phase = []
    def get(url,headers,timeout):
        token = headers['Authorization'].removeprefix('Bearer ')
        active_token = 'fake-valid-b' if len(phase)==2 else 'fake-pending-a'
        return 200,claude_ok_body(five=100 if token==active_token else 0,
                                 five_reset=iso(now()+timedelta(hours=1)))
    def spawn(argv,output,on_tick=None,*,env=None):
        phase.append(env['CLAUDE_CONFIG_DIR'])
        if len(phase)==1:
            assert (ctx.claude_home(A)/'.acctsw-unverified').exists()
        if len(phase)==2:
            state = ctx.load_state()
            state.set_limited_until('claude',A,None)
            state.save()
        if len(phase)<3:
            assert output(b'5-hour limit reached\n')
            return 1
        profile[0]['fake-pending-a'] = A
        return 0
    assert launcher.run(ctx,'claude',['--session-id','fake-thread'],spawn=spawn,get=get)==0
    assert phase == [str(ctx.claude_home(A)),str(ctx.claude_home(B)),str(ctx.claude_home(A))]
    assert not (ctx.claude_home(A)/'.acctsw-unverified').exists()


@pytest.mark.parametrize('cold_start',[False,True])
def test_supervised_hop_never_profiles_stock_under_the_store_lock(ctx, profile, cold_start):
    from datetime import timedelta
    from acctsw.switch import switch
    from acctsw.util import iso,now
    from tests.test_usage import claude_ok_body
    seat(ctx,profile)
    seat(ctx,profile,B,'fake-b')
    switch(ctx,ctx.load_state(),'claude',A,sync=False)
    sync(ctx)
    fresh_b = blob('fake-b-during-hop',300)
    profile[0]['fake-b-during-hop'] = B
    launches = []
    if cold_start:
        state = ctx.load_state()
        state.set_limited_until('claude',A,iso(now()+timedelta(hours=1)))
        state.save()
        ctx.cred['claude'].set_live(fresh_b)
    def get(url,headers,timeout):
        return 200,claude_ok_body(five=100 if headers['Authorization']=='Bearer fake-a' else 0,
                                 five_reset=iso(now()+timedelta(hours=1)))
    def spawn(argv,output,on_tick=None,*,env=None):
        launches.append(env['CLAUDE_CONFIG_DIR'])
        if not cold_start and len(launches)==1:
            ctx.cred['claude'].set_live(fresh_b)
            assert output(b'5-hour limit reached\n')
            return 1
        assert ctx.claude_cred(B).get_live() == fresh_b
        return 0
    assert launcher.run(ctx,'claude',['--session-id','fake-hop'],spawn=spawn,get=get)==0
    expected = [B] if cold_start else [A,B]
    assert launches == [str(ctx.claude_home(e)) for e in expected]


def test_changed_unknown_child_item_is_preserved_and_warned_once(ctx, profile):
    original = seat(ctx,profile)
    sync(ctx)
    unknown = blob('fake-unknown-child',500)
    messages = []
    def spawn(*_a,**_kw):
        ctx.claude_cred(A).set_live(unknown)
        return 0
    assert launcher.run(ctx,'claude',[],spawn=spawn,notify=messages.append)==0
    assert ctx.claude_cred(A).get_live() == unknown
    assert ctx.snapshot_get('claude',A) == original
    assert ctx.cred['claude'].get_live() == original
    assert len(messages)==1 and 'could not be identified' in messages[0]
    assert 'fake-unknown-child' not in repr(messages)


def test_timer_bridge_action_converges_changed_items(ctx, profile):
    seat(ctx,profile)
    sync(ctx)
    fresh = blob('fake-timer-refresh',500)
    profile[0]['fake-timer-refresh'] = A
    ctx.claude_cred(A).set_live(fresh)
    assert bridge.handle(ctx,{'action':'claude_sync'}) == {'ok':True}
    assert ctx.cred['claude'].get_live() == fresh
    assert ctx.snapshot_get('claude',A) == fresh


def test_desktop_handoff_migrates_target_using_unlocked_profile_plan(ctx, profile, monkeypatch):
    from tests.test_auto_handoff import seed,probe
    summary = seed(ctx,'claude',spare_count=1)
    profile[0].update({'x0':'seat0@test.example','x1':'seat1@test.example'})
    sync(ctx)
    claudehome.delete(ctx,'seat1@test.example')  # an unmigrated snapshot still exists
    monkeypatch.setattr(usage,'refresh_live',ctx._real_usage_refresh)
    probe(monkeypatch)
    result = bridge._auto_switch_after_usage(ctx,summary)
    assert result['status']=='switched' and result['to']=='seat1@test.example'
    assert ctx.claude_cred('seat1@test.example').get_live() == ctx.snapshot_get('claude','seat1@test.example')


def test_codex_desktop_handoff_never_reads_claude_items(ctx, profile, monkeypatch):
    from tests.test_auto_handoff import seed,probe
    seat(ctx,profile)
    sync(ctx)
    summary = seed(ctx,'codex',spare_count=1)
    monkeypatch.setattr(usage,'refresh_live',ctx._real_usage_refresh)
    probe(monkeypatch)
    def unexpected(*_):
        pytest.fail('Codex desktop handoff inspected Claude credentials')
    monkeypatch.setattr(ctx.keychain,'get_raw',unexpected)
    result = bridge._auto_switch_after_usage(ctx,summary)
    assert result['status']=='switched'


@pytest.mark.parametrize('snapshot_current',[False,True])
def test_home_refresh_mirror_update_does_not_undo_another_tabs_selected_seat(ctx, profile, snapshot_current):
    from acctsw.switch import switch
    seat(ctx,profile)
    seat(ctx,profile,B,'fake-b')
    switch(ctx,ctx.load_state(),'claude',A,sync=False)
    switch(ctx,ctx.load_state(),'claude',B,sync=False,mirror=False)
    sync(ctx)
    fresh = blob('fake-a-other-tab',500)
    profile[0]['fake-a-other-tab'] = A
    ctx.claude_cred(A).set_live(fresh)
    if snapshot_current:
        ctx.snapshot_set('claude',A,fresh)
    sync(ctx)
    assert ctx.cred['claude'].get_live()==fresh
    assert ctx.snapshot_get('claude',A)==fresh
    assert ctx.load_state().active('claude')==B


def test_missing_stock_item_stays_unknown_during_usage(ctx, profile):
    original = seat(ctx,profile)
    sync(ctx)
    stock = ctx.cred['claude']
    ctx.keychain.delete(stock.service,stock.account)
    sync(ctx)
    assert stock.get_live() is None
    assert ctx.claude_cred(A).get_live()==original
    assert ctx.snapshot_get('claude',A)==original


def test_quarantine_creates_a_private_home_for_an_orphan_item(ctx, profile):
    legacy(ctx,blob('fake-old-snapshot'))
    profile[0]['fake-orphan-foreign'] = B
    ctx.claude_cred(A).set_live(blob('fake-orphan-foreign'))
    assert not ctx.claude_home(A).parent.exists()
    sync(ctx)
    home = ctx.claude_home(A)
    assert home.stat().st_mode & 0o777 == 0o700
    assert (home/'.acctsw-quarantined').stat().st_mode & 0o777 == 0o600
    assert ctx.load_state().get_seat('claude',A)['auth_error']


def test_timer_without_registered_seats_does_not_read_stock(ctx, profile, monkeypatch):
    def unexpected():
        pytest.fail('timer read credentials without any registered seat')
    monkeypatch.setattr(ctx.cred['claude'],'get_live',unexpected)
    assert bridge.handle(ctx,{'action':'claude_sync'}) == {'ok':True}
    assert profile[1] == []


def test_stable_timer_scans_skip_snapshots_and_store_lock(ctx, profile, monkeypatch):
    seat(ctx,profile)
    sync(ctx)
    profile[1].clear()
    def unexpected(*_):
        pytest.fail('stable item scan performed snapshot or store-lock work')
    monkeypatch.setattr(ctx,'snapshot_get',unexpected)
    monkeypatch.setattr(ctx,'locked',unexpected)
    assert bridge.handle(ctx,{'action':'claude_sync'}) == {'ok':True}
    assert profile[1] == []


def test_codex_usage_poll_does_not_inspect_claude_items(ctx, profile, monkeypatch):
    seat(ctx,profile)
    sync(ctx)
    def unexpected(*_):
        pytest.fail('Codex usage poll inspected Claude credentials')
    monkeypatch.setattr(ctx.keychain,'get_raw',unexpected)
    result = bridge.handle(ctx,{'action':'usage','tool':'codex'})
    assert result['ok'],result


@pytest.mark.parametrize('race', ['stock','home','source'])
def test_cas_rechecks_after_owner_group_was_built(ctx, profile, monkeypatch, race):
    from acctsw import claudesync
    original = seat(ctx,profile)
    sync(ctx)
    fresh, concurrent = blob('fake-decided',300),blob('fake-concurrent',400)
    profile[0]['fake-decided'] = A
    source = ctx.claude_cred(A) if race=='stock' else ctx.cred['claude']
    target = ctx.cred['claude'] if race=='stock' else ctx.claude_cred(A)
    source.set_live(fresh)
    newest = claudesync.newest
    changed = []
    def decide(values):
        winner = newest(values)
        if not changed:
            changed.append(True)
            (source if race=='source' else target).set_live(concurrent)
        return winner
    monkeypatch.setattr(claudesync,'newest',decide)
    assert bridge.handle(ctx,{'action':'claude_sync'})=={'ok':True}
    assert target.get_live() == (original if race=='source' else concurrent)


def test_convergence_persists_auth_recovery_when_snapshot_advances(ctx, profile):
    from datetime import timedelta
    from acctsw.util import iso,now
    original = seat(ctx,profile)
    sync(ctx)
    state = ctx.load_state()
    state.get_seat('claude',A)['auth_error'] = True
    state.get_seat('claude',A)['usage'] = {'error':'unauthorized','error_streak':3,
        'fetched_at':iso(now()),'last_attempted_at':iso(now()),'stale':True}
    state.save()
    fresh = blob('fake-recovered',400)
    profile[0]['fake-recovered'] = A
    ctx.cred['claude'].set_live(fresh)
    assert bridge.handle(ctx,{'action':'claude_sync'})=={'ok':True}
    saved = ctx.load_state().get_seat('claude',A)
    assert not saved.get('auth_error')
    assert saved['usage']['error'] is None
    assert saved['usage']['error_streak']==0
    assert saved['usage']['last_attempted_at'] is None
    assert saved['usage']['fetched_at'] is None
    assert ctx.snapshot_get('claude',A) != original


def test_unknown_change_does_not_clear_auth_error_for_unchanged_owner_group(ctx, profile):
    original = seat(ctx,profile)
    sync(ctx)
    state = ctx.load_state()
    state.get_seat('claude',A)['auth_error'] = True
    state.save()
    ctx.cred['claude'].set_live(blob('fake-unrelated-unknown',600))
    assert bridge.handle(ctx,{'action':'claude_sync'})=={'ok':True}
    assert ctx.snapshot_get('claude',A)==original
    assert ctx.load_state().get_seat('claude',A)['auth_error']


def test_quarantined_seat_snapshot_requires_explicit_readd(ctx, profile):
    original = seat(ctx,profile)
    sync(ctx)
    (ctx.claude_home(A)/'.acctsw-quarantined').write_text('1')
    state = ctx.load_state()
    state.get_seat('claude',A)['auth_error'] = True
    state.save()
    fresh = blob('fake-still-quarantined',600)
    profile[0]['fake-still-quarantined'] = A
    ctx.claude_cred(A).set_live(fresh)
    assert bridge.handle(ctx,{'action':'claude_sync'})=={'ok':True}
    assert ctx.cred['claude'].get_live()==fresh
    assert ctx.snapshot_get('claude',A)==original
    assert claudehome.load(ctx,A) is None
    assert ctx.load_state().get_seat('claude',A)['auth_error']


def test_observer_seeding_never_replaces_a_home_login_that_appeared_meanwhile(ctx, profile, monkeypatch):
    legacy(ctx,blob('fake-before-capture'))
    fresh, concurrent = blob('fake-stock-capture',300),blob('fake-home-login',400)
    profile[0].update({'fake-stock-capture':A,'fake-home-login':B})
    ctx.cred['claude'].set_live(fresh)
    original = claudehome.ensure_home
    def ensure(c,email,**kwargs):
        c.claude_cred(email).set_live(concurrent)
        return original(c,email,**kwargs)
    monkeypatch.setattr(claudehome,'ensure_home',ensure)
    assert accounts.reconcile_claude(ctx,ctx.load_state())==A
    assert ctx.claude_cred(A).get_live()==concurrent
    assert ctx.snapshot_get('claude',A)==fresh


def test_usage_does_not_recapture_an_already_converged_stock_refresh(ctx, profile):
    seat(ctx,profile)
    sync(ctx)
    fresh = blob('fake-already-converged',400)
    profile[0]['fake-already-converged'] = A
    ctx.cred['claude'].set_live(fresh)
    before = ctx.load_state().data['rev']
    sync(ctx)
    assert ctx.snapshot_get('claude',A)==fresh
    assert ctx.load_state().data['rev']==before+1


def test_equals_short_resume_is_forwarded_without_a_new_session_id(ctx, profile):
    seat(ctx,profile)
    sync(ctx)
    def spawn(argv,*_a,**_kw):
        assert argv==['claude','-r=fake-thread']
        return 0
    assert launcher.run(ctx,'claude',['-r=fake-thread'],spawn=spawn)==0


@pytest.mark.parametrize('migration',[False,True])
@pytest.mark.parametrize('stock_login',[False,True])
def test_cli_switch_converges_and_resolves_before_lock(ctx, profile, migration, stock_login):
    from types import SimpleNamespace
    from acctsw import cli
    from acctsw.switch import switch
    seat(ctx,profile)
    landing = seat(ctx,profile,B,'fake-cli-b')
    switch(ctx,ctx.load_state(),'claude',B if stock_login else A,sync=False)
    fresh = blob('fake-cli-a-new',500)
    profile[0]['fake-cli-a-new'] = A
    (ctx.cred['claude'] if stock_login else ctx.claude_cred(A)).set_live(fresh)
    if migration:
        claudehome.delete(ctx,B)
    assert cli._cmd_switch(ctx,SimpleNamespace(tool='claude',email=B))==0
    assert ctx.snapshot_get('claude',A)==fresh
    assert ctx.claude_cred(B).get_live()==landing
    assert ctx.cred['claude'].get_live()==landing
    assert ctx.load_state().active('claude')==B
    assert profile[1], 'CLI switch must use profile owner proof'


def test_codex_cli_switch_never_reads_claude_items(ctx, profile, monkeypatch):
    from types import SimpleNamespace
    from acctsw import cli
    from tests.test_auto_handoff import seed
    seat(ctx,profile)
    sync(ctx)
    seed(ctx,'codex',spare_count=1)
    def unexpected(*_):
        pytest.fail('Codex CLI switch inspected Claude credentials')
    monkeypatch.setattr(ctx.keychain,'get_raw',unexpected)
    assert cli._cmd_switch(ctx,SimpleNamespace(tool='codex',email='seat1@test.example'))==0


def test_partial_write_retry_reuses_proof_for_propagated_item_bytes(ctx, profile, monkeypatch):
    seat(ctx,profile)
    sync(ctx)
    fresh = blob('fake-partially-propagated',500)
    profile[0]['fake-partially-propagated'] = A
    ctx.cred['claude'].set_live(fresh)
    real = ctx.keychain.set
    attempts = []
    def write(*args):
        attempts.append(True)
        if len(attempts)==1:
            raise KeychainError('fake snapshot write failure')
        return real(*args)
    monkeypatch.setattr(ctx.keychain,'set',write)
    assert bridge.handle(ctx,{'action':'claude_sync'})=={'ok':True}
    assert ctx.claude_cred(A).get_live()==fresh
    assert bridge.handle(ctx,{'action':'claude_sync'})=={'ok':True}
    assert ctx.snapshot_get('claude',A)==fresh
    assert profile[1].count('fake-partially-propagated')==1


@pytest.mark.parametrize('conflict',['newer_home','unknown_home'])
def test_stock_observer_respects_a_competing_home_after_convergence(ctx, profile, conflict):
    from acctsw.switch import switch
    seat(ctx,profile)
    if conflict=='unknown_home':
        seat(ctx,profile,B,'fake-observer-b')
        switch(ctx,ctx.load_state(),'claude',B,sync=False)
    sync(ctx)
    stock, home = blob('fake-observer-stock',300),blob('fake-competing-home',400)
    profile[0]['fake-observer-stock'] = A
    if conflict=='newer_home':
        profile[0]['fake-competing-home'] = A
    ctx.cred['claude'].set_live(stock)
    ctx.claude_cred(A).set_live(home)
    assert accounts.reconcile_claude(ctx,ctx.load_state()) is None
    assert ctx.claude_cred(A).get_live()==home
    if conflict=='newer_home':
        assert ctx.cred['claude'].get_live()==home
        assert ctx.snapshot_get('claude',A)==home
    else:
        assert ctx.cred['claude'].get_live()==stock
        assert ctx.load_state().active('claude')==B
