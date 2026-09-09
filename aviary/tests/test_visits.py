"""Visits: Frigate review items grouping events, in-process (no MQTT broker needed).

Drives the same handlers the MQTT client calls, against a temporary database, so the
race between a review item's message and its events' messages can be replayed in both
orders. Run from the ``aviary`` directory:

    python -m pytest tests -q
"""

from __future__ import annotations

import json
import time

import pytest

from app import db, ingest

CAM = "birdzone"
T0 = 1_788_904_615.0


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "aviary.db"))
    ingest.configure(ignore_unclassified=False, ignore_cameras=())
    ingest.set_identify_hook(None)
    # Reset the in-memory notification state between tests.
    with ingest._known_lock:
        ingest._known_species.clear()
        ingest._announced_refs.clear()
        ingest._tombstones.clear()
        ingest._blacklist.clear()
    # Nothing here should actually try to reach Home Assistant.
    monkeypatch.setattr(ingest.notify, "enabled", lambda: False)
    return db


def event_msg(ref: str, kind: str, start: float, end: float | None = None,
              label: str | None = None) -> bytes:
    after = {
        "id": ref, "camera": CAM, "label": "bird", "sub_label": label, "top_score": 0.8,
        "start_time": start, "end_time": end if kind == "end" else None,
        "has_clip": True, "has_snapshot": True, "entered_zones": ["bath"],
    }
    return json.dumps({"type": kind, "before": after, "after": after}).encode()


def review_msg(review_id: str, kind: str, refs: list[str], start: float,
               end: float | None = None, objects=("bird",)) -> bytes:
    after = {
        "id": review_id, "camera": CAM, "start_time": start,
        "end_time": end if kind == "end" else None, "severity": "detection",
        "data": {"detections": refs, "objects": list(objects), "zones": ["bath"],
                 "sub_labels": [], "audio": []},
    }
    return json.dumps({"type": kind, "before": after, "after": after}).encode()


def _visit(review_id: str) -> dict:
    v = db.visit_by_review_id(review_id)
    assert v is not None
    v["members"] = db.visit_members(v["id"])
    return v


def test_review_first_then_events_link_and_widen(fresh_db):
    refs = ["e1", "e2", "e3"]
    ingest.handle_frigate_review(review_msg("r1", "new", refs[:1], T0))
    ingest.handle_frigate(event_msg("e1", "new", T0 + 1))
    ingest.handle_frigate(event_msg("e1", "end", T0 + 1, T0 + 5, label="Baltimore Oriole"))
    ingest.handle_frigate_review(review_msg("r1", "update", refs[:2], T0))
    ingest.handle_frigate(event_msg("e2", "new", T0 + 7))
    ingest.handle_frigate(event_msg("e2", "end", T0 + 7, T0 + 12, label="Baltimore Oriole"))
    ingest.handle_frigate(event_msg("e3", "new", T0 + 14))
    # e3 outlives Frigate's review end by 9 s, like the real 16:56 item did.
    ingest.handle_frigate(event_msg("e3", "end", T0 + 14, T0 + 40, label="Baltimore Oriole"))
    ingest.handle_frigate_review(review_msg("r1", "end", refs, T0, T0 + 31))

    v = _visit("r1")
    assert [m["source_ref"] for m in v["members"]] == refs
    assert v["end_time"] == pytest.approx(T0 + 40)  # widened past review_end
    assert v["review_end"] == pytest.approx(T0 + 31)
    assert v["start_time"] == pytest.approx(T0)


def test_events_first_then_review_links_retroactively(fresh_db):
    for i, ref in enumerate(["e1", "e2"]):
        ingest.handle_frigate(event_msg(ref, "new", T0 + i * 10))
        ingest.handle_frigate(event_msg(ref, "end", T0 + i * 10, T0 + i * 10 + 4, label="Carolina Wren"))
    assert db.detection_by_ref("frigate", "e1")["visit_id"] is None
    ingest.handle_frigate_review(review_msg("r2", "end", ["e1", "e2"], T0, T0 + 30))
    v = _visit("r2")
    assert {m["source_ref"] for m in v["members"]} == {"e1", "e2"}
    assert db.detection_by_ref("frigate", "e2")["visit_id"] == v["id"]


def test_one_announcement_per_species_per_visit(fresh_db, monkeypatch):
    claimed: list[tuple[int, str]] = []
    real_claim = db.claim_visit_announcement

    def spy_claim(visit_id, name, det_id):
        ok = real_claim(visit_id, name, det_id)
        if ok:
            claimed.append((visit_id, name))
        return ok

    monkeypatch.setattr(db, "claim_visit_announcement", spy_claim)

    # Frigate publishes a review `update` as each new object joins the item — before
    # that object ends — so members are normally linked by the time they are named.
    members = [("a", "Northern Cardinal"), ("b", "Northern Cardinal"),
               ("c", "Carolina Wren"), ("d", "Northern Cardinal")]
    listed: list[str] = []
    for i, (ref, label) in enumerate(members):
        listed.append(ref)
        ingest.handle_frigate_review(review_msg("r3", "new" if i == 0 else "update", list(listed), T0))
        ingest.handle_frigate(event_msg(ref, "new", T0 + i * 5))
        ingest.handle_frigate(event_msg(ref, "end", T0 + i * 5, T0 + i * 5 + 3, label=label))
    ingest.handle_frigate_review(review_msg("r3", "end", listed, T0, T0 + 50))

    v = _visit("r3")
    assert sorted(claimed) == [(v["id"], "Carolina Wren"), (v["id"], "Northern Cardinal")]
    # A later member of an already-announced species is silent even on a fresh process
    # (persisted claim), while a species not yet seen in this visit still gets through.
    assert not db.claim_visit_announcement(v["id"], "northern cardinal", None)  # NOCASE
    assert db.claim_visit_announcement(v["id"], "House Finch", None)


def test_late_review_link_records_already_announced_species(fresh_db):
    """The race the other way: a named event announced per-event BEFORE the review item
    listed it must count as announced for the visit, so its siblings stay silent."""
    ingest.handle_frigate(event_msg("a", "end", T0, T0 + 3, label="Northern Cardinal"))
    assert db.detection_by_ref("frigate", "a")["visit_id"] is None
    ingest.handle_frigate_review(review_msg("r3b", "update", ["a", "b"], T0))
    v = _visit("r3b")
    assert not db.claim_visit_announcement(v["id"], "Northern Cardinal", None)
    assert db.claim_visit_announcement(v["id"], "Carolina Wren", None)


def test_quiet_gap_excludes_visit_siblings(fresh_db):
    ingest.handle_frigate_review(review_msg("r4", "new", ["a", "b"], T0))
    ingest.handle_frigate(event_msg("a", "end", T0, T0 + 3, label="Blue Jay"))
    ingest.handle_frigate(event_msg("b", "end", T0 + 5, T0 + 8, label="Blue Jay"))
    v = _visit("r4")
    # Without the visit filter the sibling 5 s earlier would be "the last sighting".
    assert db.species_last_times("Blue Jay", "frigate", "b")["seen"] == pytest.approx(T0)
    assert db.species_last_times("Blue Jay", "frigate", "b", visit_id=v["id"])["seen"] is None


def test_deleting_members_settles_or_removes_visit(fresh_db):
    ingest.handle_frigate_review(review_msg("r5", "new", ["a", "b"], T0))
    ingest.handle_frigate(event_msg("a", "end", T0, T0 + 3, label="Blue Jay"))
    ingest.handle_frigate(event_msg("b", "end", T0 + 5, T0 + 60, label="Blue Jay"))
    ingest.handle_frigate_review(review_msg("r5", "end", ["a", "b"], T0, T0 + 30))
    v = _visit("r5")
    assert v["end_time"] == pytest.approx(T0 + 60)
    # Ingest already announced Blue Jay for this visit when the first member ended.
    assert not db.claim_visit_announcement(v["id"], "Blue Jay", None)

    db.delete_detection(db.detection_by_ref("frigate", "b")["id"])
    v = _visit("r5")
    assert v["end_time"] == pytest.approx(T0 + 30)  # shrank back to Frigate's end
    # Blue Jay still has a member (a), so the announcement stands.
    assert not db.claim_visit_announcement(v["id"], "Blue Jay", None)

    db.delete_detection(db.detection_by_ref("frigate", "a")["id"])
    assert db.visit_by_review_id("r5") is None
    assert db.visit_members(v["id"]) == []


def test_seen_counts_visits_not_events(fresh_db):
    ingest.handle_frigate_review(review_msg("r6", "new", ["a", "b", "c"], T0))
    for i, ref in enumerate("abc"):
        ingest.handle_frigate(event_msg(ref, "end", T0 + i, T0 + i + 2, label="Baltimore Oriole"))
    # An ungrouped event still counts once.
    ingest.handle_frigate(event_msg("solo", "end", T0 + 900, T0 + 903, label="Baltimore Oriole"))
    db.confirm_species("Baltimore Oriole")
    stats = db.summary_stats()
    assert stats["frigate_total"] == 2
    assert stats["total"] == 2
    top = db.top_species(limit=5)
    assert top[0]["common_name"] == "Baltimore Oriole" and top[0]["seen"] == 2
    recap = db.daily_recap(T0 - 3600, T0 + 3600)
    assert recap[0]["seen"] == 2 and recap[0]["count"] == 2


def test_review_without_bird_or_on_ignored_camera_is_skipped(fresh_db):
    ingest.handle_frigate_review(review_msg("r7", "end", ["x"], T0, T0 + 5, objects=("person",)))
    assert db.visit_by_review_id("r7") is None
    ingest.configure(ignore_unclassified=False, ignore_cameras=(CAM,))
    ingest.handle_frigate_review(review_msg("r8", "end", ["x"], T0, T0 + 5))
    assert db.visit_by_review_id("r8") is None


def test_feed_page_groups_and_keeps_ungrouped(fresh_db):
    ingest.handle_frigate_review(review_msg("r9", "end", ["a", "b"], T0, T0 + 30))
    ingest.handle_frigate(event_msg("a", "end", T0, T0 + 3, label="Blue Jay"))
    ingest.handle_frigate(event_msg("b", "end", T0 + 5, T0 + 8, label="Blue Jay"))
    ingest.handle_frigate(event_msg("solo", "end", T0 + 500, T0 + 503, label="Blue Jay"))
    items = db.feed_page(limit=10)
    kinds = [it.get("kind") for it in items]
    assert kinds == [None, "visit"]  # newest first: the solo event, then the visit
    assert len(items[1]["members"]) == 2
    assert [it.get("kind") for it in db.feed_page(limit=10, source="birdnet")] == []
    assert len(db.feed_page(limit=10, species="Blue Jay")) == 2
    by_zone = db.feed_page(limit=10, zone="bath")
    assert [it.get("kind") for it in by_zone] == [None, "visit"]
    assert db.feed_page(limit=10, zone="porch") == []
