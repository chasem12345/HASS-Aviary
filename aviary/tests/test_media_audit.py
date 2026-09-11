"""Media audit: Frigate's bird-event listing vs our has_clip/has_snapshot flags. A fake
proxy.get_json serves the pages; nothing touches the network. Run from ``aviary``:

    python -m pytest tests -q
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from app import crops, db, ingest, media_audit, proxy

T0 = time.time() - 10 * 86400
CAM = "birdzone"
CARD = "Northern Cardinal"


class FakeListing:
    """Frigate's GET /api/events, paged newest-first by ``before``."""

    def __init__(self, events, page=200, fail_on_page=None):
        self.events = sorted(events, key=lambda e: -e["start_time"])
        self.page = page
        self.fail_on_page = fail_on_page
        self.pages_served = 0

    async def get_json(self, url):
        self.pages_served += 1
        if self.fail_on_page == self.pages_served:
            raise httpx.ConnectError("boom")
        before = None
        if "before=" in url:
            before = float(url.split("before=")[1].split("&")[0])
        rows = [e for e in self.events if before is None or e["start_time"] < before]
        return 200, rows[: self.page]


def frigate_event(ref, start, clip=True, snap=True):
    return {"id": ref, "start_time": start, "has_clip": clip, "has_snapshot": snap}


@pytest.fixture()
def world(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "aviary.db"))
    crops.configure(str(tmp_path))
    ingest.configure(ignore_unclassified=False, ignore_cameras=())
    ingest.set_identify_hook(None)
    ingest.set_keepsake_hook(None)
    with ingest._known_lock:
        ingest._known_species.clear()
        ingest._announced_refs.clear()
        ingest._tombstones.clear()
        ingest._blacklist.clear()
    monkeypatch.setattr(ingest.notify, "enabled", lambda: False)
    media_audit.configure(SimpleNamespace(frigate_url="http://frigate"))
    return SimpleNamespace(tmp_path=tmp_path, monkeypatch=monkeypatch)


def event(ref, start, label=CARD, end_offset=5.0):
    after = {"id": ref, "camera": CAM, "label": "bird", "sub_label": label, "top_score": 0.8,
             "start_time": start, "end_time": start + end_offset,
             "has_clip": True, "has_snapshot": True, "entered_zones": ["bath"]}
    ingest.handle_frigate(json.dumps({"type": "end", "before": after, "after": after}).encode())
    return db.detection_by_ref("frigate", ref)["id"]


def review(review_id, refs, start, end):
    after = {"id": review_id, "camera": CAM, "start_time": start, "end_time": end,
             "severity": "detection",
             "data": {"detections": refs, "objects": ["bird"], "zones": ["bath"],
                      "sub_labels": [], "audio": []}}
    ingest.handle_frigate_review(json.dumps({"type": "end", "before": after, "after": after}).encode())


def audit(world, listing):
    world.monkeypatch.setattr(proxy, "get_json", listing.get_json)
    # The fake serves ``page`` events per call; the audit must ask for the same page
    # size, since "shorter than a page" is how it knows the listing is complete.
    world.monkeypatch.setattr(media_audit, "_PAGE", listing.page)
    return asyncio.run(media_audit.run_audit())


def flags(det_id):
    row = db.detection_by_id(det_id)
    return row["has_clip"], row["has_snapshot"], row["media_expired_at"]


def test_unlisted_old_event_is_marked_expired_and_kept(world):
    gone = event("gone", T0)
    kept = event("kept", T0 + 60)
    result = audit(world, FakeListing([frigate_event("kept", T0 + 60)]))
    assert result == {"listed": 1, "expired": 1, "synced": 0, "restored": 0}
    c, s, stamp = flags(gone)
    assert (c, s) == (0, 0) and stamp is not None
    assert flags(kept) == (1, 1, None)
    # The row and everything that counts it are still there.
    assert db.detection_by_id(gone)["common_name"] == CARD
    assert db.species_stats(CARD)["frigate_total"] == 2
    # A second pass changes nothing and keeps the original stamp.
    audit(world, FakeListing([frigate_event("kept", T0 + 60)]))
    assert flags(gone)[2] == stamp


def test_partial_expiry_is_synced_and_restore_clears_the_stamp(world):
    a = event("a", T0)
    audit(world, FakeListing([frigate_event("a", T0, clip=False, snap=True)]))
    assert flags(a) == (0, 1, None)
    audit(world, FakeListing([frigate_event("a", T0, clip=False, snap=False)]))
    assert flags(a)[:2] == (0, 0) and flags(a)[2] is not None
    audit(world, FakeListing([frigate_event("a", T0, clip=True, snap=True)]))
    assert flags(a) == (1, 1, None)


def test_recent_events_get_an_hour_of_grace(world):
    fresh = event("fresh", time.time() - 600)
    audit(world, FakeListing([]))                  # nothing listed at all, no rows older than a day
    assert flags(fresh) == (1, 1, None)


def test_incomplete_listing_changes_nothing(world):
    ids = [event(f"e{i}", T0 + i) for i in range(5)]
    listing = FakeListing([frigate_event(f"e{i}", T0 + i) for i in range(5)], page=2, fail_on_page=2)
    assert audit(world, listing) is None
    assert all(flags(i) == (1, 1, None) for i in ids)
    # A complete multi-page listing works.
    listing = FakeListing([frigate_event(f"e{i}", T0 + i) for i in range(1, 5)], page=2)
    result = audit(world, listing)
    assert result["expired"] == 1 and flags(ids[0])[:2] == (0, 0)
    assert all(flags(i) == (1, 1, None) for i in ids[1:])


def test_empty_listing_with_recent_ingest_is_not_trusted(world):
    old = event("old", T0)
    event("today", time.time() - 7200)              # ingested today, past the grace hour
    assert audit(world, FakeListing([])) is None
    assert flags(old) == (1, 1, None)


def test_feed_hides_expired_detections_unless_they_have_a_crop(world):
    plain = event("plain", T0)
    event("cropped", T0 + 60)
    db.set_has_crop("cropped", True)
    # A visit made only of expired, crop-less members hides; one with a live member stays.
    review("r1", ["v1", "v2"], T0 + 1000, T0 + 1100)
    event("v1", T0 + 1000)
    event("v2", T0 + 1050)
    review("r2", ["v3", "live"], T0 + 2000, T0 + 2100)
    event("v3", T0 + 2000)
    event("live", T0 + 2050)
    audit(world, FakeListing([frigate_event("live", T0 + 2050)]))
    items = db.feed_page(limit=20)
    refs = [it.get("source_ref") or it.get("review_id") for it in items]
    assert "plain" not in refs and "cropped" in refs
    assert "r1" not in refs and "r2" in refs
    # Direct access still works, and the aggregates are untouched.
    assert db.detection_by_id(plain)["media_expired_at"] is not None
    assert db.species_stats(CARD)["frigate_total"] == 4     # 2 visits + 2 solo events


def test_has_crop_catch_up_from_the_crops_directory(world):
    a = event("a", T0)
    b = event("b", T0 + 1)
    crops_dir = world.tmp_path / "crops"
    (crops_dir / "a.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    (crops_dir / "b-1.jpg").write_bytes(b"\xff\xd8\xff\xd9")        # a subject's crop, not b's own
    (crops_dir / "1718123456.123456-abc.jpg").write_bytes(b"\xff")   # a real Frigate id with a dash
    refs = crops.list_primary_refs()
    assert sorted(refs) == ["1718123456.123456-abc", "a"]
    assert db.mark_crops_present(refs) == 1
    assert db.detection_by_id(a)["has_crop"] == 1 and db.detection_by_id(b)["has_crop"] == 0
