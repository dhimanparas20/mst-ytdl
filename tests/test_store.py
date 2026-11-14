"""services/store.py — Redis persistence, guarded transitions, races."""

from __future__ import annotations

import threading
import time

from models import DownloadItem, DownloadJob, DownloadOptions, MediaFormat, ResolveState
from models import DownloadStatus as S
from services.store import job_store as store


def mk_job(job_id="j1", **kw) -> DownloadJob:
    return DownloadJob(job_id=job_id, url="https://x", options=DownloadOptions(format=MediaFormat.MP3), **kw)


def mk_item(item_id="i1", job_id="j1", **kw) -> DownloadItem:
    return DownloadItem(item_id=item_id, job_id=job_id, url="https://x", **kw)


class TestJobs:
    def test_create_and_get_roundtrip(self):
        j = store.create_job(mk_job(title="T", uploader=None, folder="2026-01-01"))
        got = store.get_job("j1")
        assert got == j and got.created_at > 0 and got.options.format is MediaFormat.MP3
        assert got.uploader is None  # None survives the JSON-per-field encoding

    def test_get_missing(self):
        assert store.get_job("nope") is None

    def test_update_job(self):
        store.create_job(mk_job())
        before = store.get_job("j1").updated_at
        time.sleep(0.01)
        assert store.update_job("j1", title="New", resolve_state=ResolveState.READY) is True
        g = store.get_job("j1")
        assert g.title == "New" and g.resolve_state is ResolveState.READY and g.updated_at > before

    def test_update_missing_job_does_not_resurrect(self, fake_redis):
        assert store.update_job("ghost", title="x") is False
        assert fake_redis.exists(store.job_key("ghost")) == 0

    def test_get_jobs_batches_and_skips_missing(self):
        store.create_job(mk_job("a"))
        store.create_job(mk_job("b"))
        assert {j.job_id for j in store.get_jobs(["a", "zzz", "b"])} == {"a", "b"}
        assert store.get_jobs([]) == []

    def test_ttl_applied(self, fake_redis):
        store.create_job(mk_job())
        assert 0 < fake_redis.ttl(store.job_key("j1")) <= store.ttl

    def test_list_job_ids_newest_first_and_paginates(self):
        for i in range(5):
            store.create_job(mk_job(f"j{i}", created_at=1000 + i))
        assert store.list_job_ids(0, 3) == ["j4", "j3", "j2"]
        assert store.list_job_ids(3, 10) == ["j1", "j0"]
        assert store.count_jobs() == 5

    def test_list_job_ids_prunes_expired_index_entries(self, fake_redis):
        store.create_job(mk_job("alive", created_at=2))
        store.create_job(mk_job("dead", created_at=1))
        fake_redis.delete(store.job_key("dead"))  # simulate TTL expiry
        assert store.list_job_ids() == ["alive"]
        assert store.count_jobs() == 1  # index healed

    def test_transition_job_guard(self):
        store.create_job(mk_job(resolve_state=ResolveState.PENDING))
        assert store.transition_job("j1", {"ready"}, {"title": "x"}) is None
        out = store.transition_job("j1", {"pending"}, {"resolve_state": ResolveState.RESOLVING})
        assert out["resolve_state"] == "resolving"
        assert store.get_job("j1").resolve_state is ResolveState.RESOLVING
        assert store.transition_job("ghost", {"pending"}, {}) is None

    def test_delete_job_removes_everything(self, fake_redis):
        store.create_job(mk_job())
        store.add_items([mk_item("i1"), mk_item("i2")])
        assert store.delete_job("j1") == 2
        assert fake_redis.keys("*") == []


class TestItems:
    def test_add_get_roundtrip_with_defaults(self):
        store.create_job(mk_job())
        store.add_items([mk_item(title="Song", duration=12.5, index=3)])
        it = store.get_item("i1")
        assert (it.title, it.duration, it.index, it.status) == ("Song", 12.5, 3, S.QUEUED)
        assert it.partials == [] and it.control is None and it.total_bytes is None

    def test_add_items_preserves_order(self):
        store.create_job(mk_job())
        store.add_items([mk_item(f"i{n}", index=n) for n in range(1, 6)])
        assert [i.item_id for i in store.get_items("j1")] == ["i1", "i2", "i3", "i4", "i5"]
        assert store.get_item_ids("j1") == ["i1", "i2", "i3", "i4", "i5"]

    def test_add_empty_is_noop(self):
        store.add_items([])

    def test_get_items_many(self):
        store.create_job(mk_job("a"))
        store.create_job(mk_job("b"))
        store.add_items([mk_item("a1", "a"), mk_item("a2", "a")])
        store.add_items([mk_item("b1", "b")])
        many = store.get_items_many(["a", "b", "c"])
        assert [i.item_id for i in many["a"]] == ["a1", "a2"] and len(many["b"]) == 1 and many["c"] == []
        assert store.get_items_many([]) == {}

    def test_get_items_skips_vanished_hashes(self, fake_redis):
        store.create_job(mk_job())
        store.add_items([mk_item("i1"), mk_item("i2")])
        fake_redis.delete(store.item_key("i1"))
        assert [i.item_id for i in store.get_items("j1")] == ["i2"]

    def test_patch_item_updates_only_given_fields(self):
        store.create_job(mk_job())
        store.add_items([mk_item(status=S.DOWNLOADING, token="t", percent=1.0)])
        assert store.patch_item("i1", percent=42.5, speed=1000.0)
        it = store.get_item("i1")
        assert it.percent == 42.5 and it.speed == 1000.0 and it.status is S.DOWNLOADING and it.token == "t"

    def test_patch_missing_item_does_not_resurrect(self, fake_redis):
        assert store.patch_item("ghost", percent=1) is False
        assert fake_redis.exists(store.item_key("ghost")) == 0

    def test_patch_accepts_enums(self):
        store.create_job(mk_job())
        store.add_items([mk_item()])
        store.patch_item("i1", status=S.PAUSED)
        assert store.get_item("i1").status is S.PAUSED

    def test_control_state(self):
        store.create_job(mk_job())
        store.add_items([mk_item(token="tok", status=S.DOWNLOADING)])
        assert store.get_control_state("i1") == {"control": None, "token": "tok", "status": "downloading"}
        store.patch_item("i1", control="pause")
        assert store.get_control_state("i1")["control"] == "pause"
        assert store.get_control_state("ghost") is None

    def test_delete_item(self):
        store.create_job(mk_job())
        store.add_items([mk_item("i1"), mk_item("i2")])
        store.delete_item("i1", "j1")
        assert store.get_item("i1") is None and store.get_item_ids("j1") == ["i2"]


class TestTransitions:
    def setup_method(self):
        store.create_job(mk_job())

    def test_allowed_status_applies(self):
        store.add_items([mk_item(token="t")])
        out = store.transition_item("i1", {S.QUEUED}, {"status": S.DOWNLOADING})
        assert out.status is S.DOWNLOADING and store.get_item("i1").status is S.DOWNLOADING

    def test_disallowed_status_is_rejected_and_unchanged(self):
        store.add_items([mk_item(status=S.COMPLETED)])
        assert store.transition_item("i1", {S.QUEUED, S.PAUSED}, {"status": S.DOWNLOADING}) is None
        assert store.get_item("i1").status is S.COMPLETED

    def test_token_guard(self):
        store.add_items([mk_item(token="good")])
        assert store.transition_item("i1", {S.QUEUED}, {"status": S.DOWNLOADING}, expect_token="bad") is None
        assert store.transition_item("i1", {S.QUEUED}, {"status": S.DOWNLOADING}, expect_token="good")

    def test_callable_updates_see_current_values(self):
        store.add_items([mk_item(attempts=2)])
        out = store.transition_item("i1", {S.QUEUED}, lambda cur: {"attempts": cur["attempts"] + 1})
        assert out.attempts == 3 and store.get_item("i1").attempts == 3

    def test_missing_item(self):
        assert store.transition_item("ghost", {S.QUEUED}, {}) is None

    def test_updated_at_bumped(self):
        store.add_items([mk_item()])
        before = store.get_item("i1").updated_at
        time.sleep(0.01)
        store.transition_item("i1", {S.QUEUED}, {"stage": "x"})
        assert store.get_item("i1").updated_at > before

    def test_exactly_one_of_many_concurrent_claims_wins(self):
        store.add_items([mk_item(token="t")])
        wins, barrier = [], threading.Barrier(16)

        def claim():
            barrier.wait()
            r = store.transition_item("i1", {S.QUEUED}, {"status": S.DOWNLOADING}, expect_token="t")
            if r:
                wins.append(r)

        threads = [threading.Thread(target=claim) for _ in range(16)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert len(wins) == 1

    def test_pause_vs_claim_race_has_one_consistent_outcome(self):
        """API pauses a queued item while a worker claims it: exactly one wins."""
        def race(iid: str) -> dict:
            results: dict = {}
            gate = threading.Barrier(2)

            def api():
                gate.wait()
                results["pause"] = store.transition_item(iid, {S.QUEUED}, {"status": S.PAUSED, "token": ""})

            def worker():
                gate.wait()
                results["claim"] = store.transition_item(iid, {S.QUEUED}, {"status": S.DOWNLOADING}, expect_token="t")

            threads = [threading.Thread(target=api), threading.Thread(target=worker)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            return results

        for n in range(30):
            iid = f"r{n}"
            store.add_items([mk_item(iid, token="t")])
            results = race(iid)
            assert (results["pause"] is None) != (results["claim"] is None)
            final = store.get_item(iid).status
            assert final is (S.PAUSED if results["pause"] else S.DOWNLOADING)

    def test_progress_patches_do_not_break_concurrent_transitions(self):
        """WATCH retries transparently when a worker keeps writing progress."""
        store.add_items([mk_item(status=S.DOWNLOADING, token="t")])
        stop = threading.Event()

        def writer():
            n = 0
            while not stop.is_set():
                store.patch_item("i1", percent=float(n % 100))
                n += 1

        t = threading.Thread(target=writer)
        t.start()
        try:
            out = store.transition_item("i1", {S.DOWNLOADING}, {"control": "pause"})
        finally:
            stop.set()
            t.join()
        assert out is not None and store.get_item("i1").control.value == "pause"
