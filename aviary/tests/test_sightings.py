"""Named other-birds count as sightings: the species_sightings view behind every species
aggregate, the feed's species filter, and removal. Run from ``aviary``:

    python -m pytest tests -q
"""

from __future__ import annotations

import json

import pytest

from app import crops, db, heroes, ingest

T0 = 1_788_904_615.0
CAM = "birdzone"
CARD = "Northern Cardinal"
ORIOLE = "Baltimore Oriole"


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    # Same shape as tests/test_visits.py: real ingest handlers against a temp database,
    # notifications off.
    db.init_db(str(tmp_path / "aviary.db"))
    ingest.configure(ignore_unclassified=False, ignore_cameras=())
    ingest.set_identify_hook(None)
    with ingest._known_lock:
        ingest._known_species.clear()
        ingest._announced_refs.clear()
        ingest._tombstones.clear()
        ingest._blacklist.clear()
    monkeypatch.setattr(ingest.notify, "enabled", lambda: False)
    return db


def event_msg(ref, kind, start, end=None, label=None):
    after = {
        "id": ref, "camera": CAM, "label": "bird", "sub_label": label, "top_score": 0.8,
        "start_time": start, "end_time": end if kind == "end" else None,
        "has_clip": True, "has_snapshot": True, "entered_zones": ["bath"],
    }
    return json.dumps({"type": kind, "before": after, "after": after}).encode()


def review_msg(review_id, kind, refs, start, end=None):
    after = {
        "id": review_id, "camera": CAM, "start_time": start,
        "end_time": end if kind == "end" else None, "severity": "detection",
        "data": {"detections": refs, "objects": ["bird"], "zones": ["bath"],
                 "sub_labels": [], "audio": []},
    }
    return json.dumps({"type": kind, "before": after, "after": after}).encode()


def det_id(ref: str) -> int:
    row = db.detection_by_ref("frigate", ref)
    assert row is not None
    return row["id"]


def subjects(primary_name: str, *others: tuple[str, str, float, str]) -> list[dict]:
    """(name, sci, score, status) tuples for the OTHER birds; idx counts up from 1."""
    rows = [{"idx": 0, "is_primary": 1, "common_name": primary_name, "score": 0.9, "id_status": "ok"}]
    for i, (name, sci, score, status) in enumerate(others, start=1):
        rows.append({"idx": i, "is_primary": 0, "common_name": name, "scientific_name": sci,
                     "score": score, "id_status": status})
    return rows


@pytest.fixture()
def world(fresh_db):
    # Visit r1 = two cardinal objects; the oriole is only ever "also in view" on the first,
    # and a house wren was seen too but below threshold.
    ingest.handle_frigate_review(review_msg("r1", "end", ["a", "b"], T0, T0 + 30))
    ingest.handle_frigate(event_msg("a", "end", T0, T0 + 3, label=CARD))
    ingest.handle_frigate(event_msg("b", "end", T0 + 5, T0 + 8, label=CARD))
    db.replace_subjects(det_id("a"), subjects(
        CARD, (ORIOLE, "Icterus galbula", 0.8, "ok"), ("House Wren", None, 0.3, "low_confidence")))
    db.replace_subjects(det_id("b"), subjects(CARD))
    # A visit-less cardinal event, oriole in view again — 500 s later.
    ingest.handle_frigate(event_msg("solo", "end", T0 + 500, T0 + 503, label=CARD))
    db.replace_subjects(det_id("solo"), subjects(CARD, (ORIOLE, "Icterus galbula", 0.7, "ok")))
    db._regularity_cache.clear()
    return fresh_db


def by_name(rows):
    return {r["common_name"]: r for r in rows}


def test_feed_lists_the_oriole_visit_and_event(world):
    items = db.feed_page(limit=10, species=ORIOLE)
    assert [it.get("kind") for it in items] == [None, "visit"]
    assert items[0]["source_ref"] == "solo"
    assert items[1]["review_id"] == "r1"
    # The wren never passed the threshold, so it has no page.
    assert db.feed_page(limit=10, species="House Wren") == []


def test_species_list_and_stats_count_once_per_visit(world):
    rows = by_name(db.species_list())
    assert set(rows) == {CARD, ORIOLE}
    assert rows[CARD]["frigate_total"] == 2          # visit r1 + solo
    assert rows[ORIOLE]["frigate_total"] == 2        # the same two units, as the oriole
    assert rows[ORIOLE]["scientific_name"] == "Icterus galbula"
    stats = db.species_stats(ORIOLE)
    assert stats["frigate_total"] == 2 and stats["total"] == 2
    assert stats["scientific_name"] == "Icterus galbula"
    assert db.summary_stats()["frigate_total"] == 4  # two units × two species
    assert db.summary_stats()["species"] == 2
    top = by_name(db.top_species())
    assert top[ORIOLE]["seen"] == 2
    assert db.scientific_name_for(ORIOLE) == "Icterus galbula"


def test_recap_ticks_and_first_ever_include_subjects(world):
    day_start = db.species_regularity()[ORIOLE.lower()]["first_seen"] - 3600
    recap = by_name(db.daily_recap(day_start, day_start + 86400))
    assert recap[ORIOLE]["seen"] == 2 and recap[ORIOLE]["is_first_ever"]
    ticks = db.recap_ticks(day_start, day_start + 86400)
    # Visit r1 (two clips) and solo: two oriole ticks, one standing for the 2-clip visit
    # only via its parent detection `a` (the subject lives on one event).
    assert [(t["source"], t["n"]) for t in ticks[ORIOLE]] == [("frigate", 1), ("frigate", 1)]
    assert [(t["source"], t["n"]) for t in ticks[CARD]] == [("frigate", 2), ("frigate", 1)]
    hours = db.hourly_by_species(day_start, day_start + 86400)
    assert sum(h["seen"] for h in hours[ORIOLE]) == 2
    assert db.species_regularity()[ORIOLE.lower()]["days_active"] == 1
    assert db.monthly_counts(ORIOLE) and sum(db.monthly_counts(ORIOLE)) == 2


def test_thumbnails_dex_numbers_and_registry(world, tmp_path):
    crops.configure(str(tmp_path / "data"))
    # The oriole only ever appears as the OTHER bird in cardinal events. With no crop of
    # its own there is no honest picture: the cardinal's thumbnail must not stand in.
    assert ORIOLE not in heroes.for_species([ORIOLE])
    assert heroes.for_species([CARD])[CARD] == {
        "id": det_id("solo"), "source_ref": "solo", "subject_idx": 0, "start_time": T0 + 500}
    # Once the identifier stored the oriole's own crop from the solo event, that is it.
    (tmp_path / "data" / "crops" / "solo-1.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    assert heroes.for_species([ORIOLE])[ORIOLE] == {
        "id": det_id("solo"), "source_ref": "solo", "subject_idx": 1, "start_time": T0 + 500}
    assert set(db.species_dex_numbers()) == {CARD, ORIOLE}
    assert db.registry_stats()["total"] == 2 and db.registry_stats()["seen"] == 2
    assert db.distinct_species() == [ORIOLE, CARD]
    assert db.distinct_species(include_subjects=False) == [CARD]


def test_same_species_as_primary_and_subject_does_not_double_count(world):
    db.replace_subjects(det_id("b"), subjects(CARD, (CARD, "Cardinalis cardinalis", 0.6, "ok")))
    assert by_name(db.species_list())[CARD]["frigate_total"] == 2
    assert db.summary_stats()["frigate_total"] == 4


def test_deleting_the_oriole_rules_it_out_of_its_events(world):
    rows = db.delete_species(ORIOLE)
    assert rows == []                                 # it never had a tracked detection
    assert set(by_name(db.species_list())) == {CARD}
    assert db.species_stats(ORIOLE)["total"] == 0
    assert db.feed_page(limit=10, species=ORIOLE) == []
    others = db.secondary_subjects_for(det_id("a"))
    oriole_row = next(s for s in others if s["idx"] == 1)
    assert oriole_row["id_status"] == "rejected" and oriole_row["display_name"] is None
    assert ORIOLE in oriole_row["rejected"]
    # The cardinal, and the visit, are untouched.
    assert by_name(db.species_list())[CARD]["frigate_total"] == 2
