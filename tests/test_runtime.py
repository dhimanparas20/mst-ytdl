"""services/runtime.py — runtime settings + the cross-process slot limiter."""

from __future__ import annotations

import threading

import pytest

from configs import get_settings
from services.runtime import SLOT_TTL_S
from services.runtime import runtime_service as rt
from services.runtime import slot_limiter as lim


class TestRuntimeService:
    def test_defaults_come_from_env_settings(self):
        r = rt.get()
        assert r.max_parallel == get_settings().task.max_parallel
        assert r.speed_limit == get_settings().download.speed_limit

    def test_update_max_parallel_persists(self):
        assert rt.update(max_parallel=5).max_parallel == 5
        assert rt.get().max_parallel == 5

    def test_max_parallel_clamped_to_cap_and_floor(self):
        cap = get_settings().task.max_parallel_cap
        assert rt.update(max_parallel=10_000).max_parallel == cap
        assert rt.update(max_parallel=0).max_parallel == 1
        assert rt.update(max_parallel=-3).max_parallel == 1

    def test_speed_limit_set_clear_and_unchanged(self):
        assert rt.update(speed_limit=2_000_000).speed_limit == 2_000_000
        assert rt.update(max_parallel=2).speed_limit == 2_000_000  # untouched by sentinel
        assert rt.update(speed_limit=None).speed_limit is None  # None clears
        assert rt.update(speed_limit=0).speed_limit is None  # 0 clears

    def test_stored_none_beats_env_default(self, monkeypatch):
        monkeypatch.setattr(get_settings().download, "speed_limit", 123)
        assert rt.get().speed_limit == 123
        rt.update(speed_limit=None)
        assert rt.get().speed_limit is None

    def test_key_uses_prefix(self, fake_redis):
        rt.update(max_parallel=2)
        assert any(k.endswith(":RUNTIME") for k in fake_redis.keys("*"))


class TestSlotLimiter:
    def test_acquire_up_to_limit_then_refuse(self):
        assert [lim.acquire(f"i{n}", 3) for n in range(5)] == [True, True, True, False, False]
        assert sorted(lim.holders()) == ["i0", "i1", "i2"]

    def test_refused_item_leaves_no_trace(self, fake_redis):
        lim.acquire("a", 1)
        assert lim.acquire("b", 1) is False
        assert not lim.is_alive("b") and "b" not in lim.holders()

    def test_acquire_is_idempotent_for_holder(self):
        assert lim.acquire("a", 1) and lim.acquire("a", 1) and lim.acquire("a", 1)
        assert lim.holders() == ["a"]

    def test_release_frees_a_slot(self):
        lim.acquire("a", 1)
        assert lim.acquire("b", 1) is False
        lim.release("a")
        assert lim.acquire("b", 1) is True and lim.holders() == ["b"]

    def test_release_unknown_is_safe(self):
        lim.release("ghost")

    def test_dead_holder_is_reclaimed(self, fake_redis):
        lim.acquire("dead", 1)
        fake_redis.delete(lim.slot_key("dead"))  # heartbeat expired = worker died
        assert lim.acquire("new", 1) is True
        assert lim.holders() == ["new"]

    def test_heartbeat_sets_ttl_and_keeps_alive(self, fake_redis):
        lim.acquire("a", 1)
        fake_redis.expire(lim.slot_key("a"), 1)
        lim.heartbeat("a")
        assert fake_redis.ttl(lim.slot_key("a")) > 1
        assert fake_redis.ttl(lim.slot_key("a")) <= SLOT_TTL_S

    def test_lowering_the_limit_does_not_evict_running(self):
        for n in range(3):
            lim.acquire(f"i{n}", 3)
        assert lim.acquire("late", 1) is False  # new work waits…
        assert len(lim.holders()) == 3  # …existing keep running

    def test_raising_the_limit_admits_waiters(self):
        lim.acquire("a", 1)
        assert lim.acquire("b", 1) is False
        assert lim.acquire("b", 2) is True

    @pytest.mark.parametrize("limit,contenders", [(1, 12), (3, 20), (5, 8)])
    def test_concurrent_acquire_never_exceeds_limit(self, limit, contenders):
        winners, gate = [], threading.Barrier(contenders)

        def go(n):
            gate.wait()
            if lim.acquire(f"c{n}", limit):
                winners.append(n)

        ts = [threading.Thread(target=go, args=(n,)) for n in range(contenders)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert len(winners) == min(limit, contenders)
        assert len(lim.holders()) == min(limit, contenders)

    def test_live_items_strips_run_suffix_and_ignores_dead(self, fake_redis):
        lim.acquire("item1:runA", 5)
        lim.acquire("item1:runB", 5)  # same item, second execution
        lim.acquire("item2:runC", 5)
        fake_redis.delete(lim.slot_key("item2:runC"))  # item2's worker died
        assert lim.live_items() == {"item1"}

    def test_one_runs_release_leaves_the_other_holder(self):
        lim.acquire("item1:old", 3)
        lim.acquire("item1:new", 3)
        lim.release("item1:old")
        assert lim.holders() == ["item1:new"] and lim.live_items() == {"item1"}

    def test_is_alive(self):
        assert lim.is_alive("x") is False
        lim.acquire("x", 1)
        assert lim.is_alive("x") is True
