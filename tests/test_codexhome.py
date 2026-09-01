"""Regression tests for auth-only Codex seat storage and runtime leases."""
from __future__ import annotations

import pytest

from acctsw import codexhome, codexruntime
from acctsw import accounts as acct
from acctsw.errors import CodexBusy
from acctsw.switch import switch
from tests.conftest import make_codex_blob


def _add(ctx, email: str) -> None:
    ctx.cred["codex"].set_live(make_codex_blob(email))
    acct.add(ctx, ctx.load_state(), "codex", email=email)


def test_auth_store_never_links_canonical_codex_state(tmp_path):
    real = tmp_path / "codex"
    real.mkdir()
    (real / "config.toml").write_text("model = 'x'\n")
    (real / "queue_1.sqlite").write_bytes(b"sqlite")
    (real / "sessions").mkdir()

    root = tmp_path / "stores"
    codexhome.save("a@x.com", "secret", codex_home=real, root=root)
    store = codexhome.home_dir("a@x.com", root)

    assert {p.name for p in store.iterdir()} == {"auth.json"}
    assert not any(p.is_symlink() for p in store.iterdir())
    assert (store / "auth.json").read_text() == "secret"


def test_legacy_mixed_database_layout_is_preserved_but_ignored(tmp_path):
    real = tmp_path / "codex"
    real.mkdir()
    (real / "queue_1.sqlite-wal").write_bytes(b"canonical wal")
    root = tmp_path / "stores"
    store = codexhome.home_dir("a@x.com", root)
    store.mkdir(parents=True)
    (store / "queue_1.sqlite").write_bytes(b"legacy db")
    (store / "queue_1.sqlite-wal").symlink_to(real / "queue_1.sqlite-wal")

    codexhome.save("a@x.com", "fresh auth", codex_home=real, root=root)

    assert (store / "auth.json").read_text() == "fresh auth"
    assert (store / "queue_1.sqlite").read_bytes() == b"legacy db"
    assert (store / "queue_1.sqlite-wal").is_symlink()


def test_delete_removes_legacy_real_directories_without_following_symlinks(tmp_path):
    outside = tmp_path / "canonical"
    outside.mkdir()
    (outside / "keep").write_text("safe")
    root = tmp_path / "stores"
    store = codexhome.home_dir("a@x.com", root)
    (store / "thread-writer-locks").mkdir(parents=True)
    (store / "thread-writer-locks" / "old").write_text("x")
    (store / "sessions").symlink_to(outside, target_is_directory=True)

    assert codexhome.delete("a@x.com", root=root) is True
    assert not store.exists()
    assert (outside / "keep").read_text() == "safe"


def test_delete_unlinks_replaced_store_root_without_following_it(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("safe")
    root = tmp_path / "stores"
    root.mkdir()
    codexhome.home_dir("a@x.com", root).symlink_to(outside, target_is_directory=True)

    assert codexhome.delete("a@x.com", root=root) is True
    assert (outside / "keep").read_text() == "safe"


def test_runtime_lease_counts_only_running_children_and_cleans_stale_files(tmp_path):
    first = codexruntime.SupervisorLease(tmp_path)
    second = codexruntime.SupervisorLease(tmp_path)
    try:
        assert codexruntime.running_count(tmp_path) == 0
        first.mark_running("a@x.com")
        second.mark_running("a@x.com")
        assert codexruntime.running_count(tmp_path) == 2
        first.mark_stopped()
        assert codexruntime.running_count(tmp_path) == 1
        second.mark_stopped()
        assert codexruntime.running_count(tmp_path) == 0
    finally:
        first.close()
        second.close()

    stale = tmp_path / "codex-runners" / "stale.lease"
    stale.write_text("running\ta@x.com\n")
    assert codexruntime.running_count(tmp_path) == 0
    assert not stale.exists()


def test_codex_switch_rejects_running_child_but_allows_stopped_waiters(ctx):
    _add(ctx, "a@x.com")
    _add(ctx, "b@x.com")
    lease_a = codexruntime.SupervisorLease(ctx.data_dir)
    lease_b = codexruntime.SupervisorLease(ctx.data_dir)
    try:
        lease_a.mark_running("b@x.com")
        lease_b.mark_running("b@x.com")
        with pytest.raises(CodexBusy):
            switch(ctx, ctx.load_state(), "codex", "a@x.com")
        assert ctx.load_state().active("codex") == "b@x.com"

        lease_a.mark_stopped()
        with pytest.raises(CodexBusy):
            switch(ctx, ctx.load_state(), "codex", "a@x.com")

        # Both supervisors can be stopped/waiting without blocking each other forever; one wins the
        # canonical switch and both can subsequently resume on the new active seat.
        lease_b.mark_stopped()
        switch(ctx, ctx.load_state(), "codex", "a@x.com")
        assert ctx.load_state().active("codex") == "a@x.com"
    finally:
        lease_a.close()
        lease_b.close()


def test_removing_active_codex_seat_is_rejected_while_child_runs(ctx):
    _add(ctx, "a@x.com")
    lease = codexruntime.SupervisorLease(ctx.data_dir)
    lease.mark_running("a@x.com")
    try:
        with pytest.raises(CodexBusy):
            acct.remove(ctx, ctx.load_state(), "codex", "a@x.com")
    finally:
        lease.close()
    assert "a@x.com" in ctx.load_state().accounts("codex")
