"""Which sighting's picture stands for a species (heroes.pick). Pure: candidates are
dicts shaped like db.species_heroes rows, newest first, and has_crop is a fake."""

from __future__ import annotations

from app import heroes

T0 = 1_788_904_615.0


def cand(ref, idx=0, age=0, snapshot=1, retained=None, keepsake=0):
    return {"id": hash(ref) % 1000, "source_ref": ref, "subject_idx": idx,
            "start_time": T0 - age, "has_snapshot": snapshot, "retained_at": retained,
            "is_keepsake": keepsake}


def crops_of(*pairs):
    have = set(pairs)
    return lambda ref, idx: (ref, idx) in have


def test_own_crop_wins_even_for_a_subject_sighting():
    rows = [cand("finch", idx=2, age=0), cand("wren", idx=0, age=100)]
    # The wren was "also in view" in the finch's event and has its own crop there.
    assert heroes.pick(rows, crops_of(("finch", 2)))["source_ref"] == "finch"
    assert heroes.pick(rows, crops_of(("finch", 2)))["subject_idx"] == 2


def test_subject_without_crop_is_never_used():
    rows = [cand("finch", idx=2, age=0), cand("wren", idx=0, age=100)]
    # No crop anywhere: the finch event's thumbnail shows the finch, so skip to the
    # wren's own tracked event.
    picked = heroes.pick(rows, crops_of())
    assert picked["source_ref"] == "wren" and picked["subject_idx"] == 0


def test_only_subject_sightings_and_no_crop_means_no_hero():
    rows = [cand("finch", idx=1, age=0), cand("cardinal", idx=3, age=50)]
    assert heroes.pick(rows, crops_of()) is None


def test_kept_primary_beats_a_newer_unkept_one():
    rows = [cand("new", age=0), cand("kept", age=500, retained=T0), cand("old", age=900)]
    assert heroes.pick(rows, crops_of())["source_ref"] == "kept"
    rows = [cand("new", age=0), cand("ks", age=500, keepsake=1)]
    assert heroes.pick(rows, crops_of())["source_ref"] == "ks"


def test_newest_crop_beats_a_kept_thumbnail():
    rows = [cand("new", age=0), cand("kept", age=500, retained=T0)]
    assert heroes.pick(rows, crops_of(("new", 0)))["source_ref"] == "new"


def test_primary_without_snapshot_is_skipped():
    rows = [cand("nosnap", age=0, snapshot=0), cand("snap", age=10)]
    assert heroes.pick(rows, crops_of())["source_ref"] == "snap"
    assert heroes.pick([cand("nosnap", snapshot=0)], crops_of()) is None
