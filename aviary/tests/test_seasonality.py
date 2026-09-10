"""Seasonality labels, Wikipedia section parsing, AVONET migration mapping, and the
seasonality cache round trip. Run from ``aviary``: python -m pytest tests -q"""

from __future__ import annotations

import time

import pytest

from app import db, seasonality, species_info, traits


# --------------------------------------------------------------------- labels

def months(*pairs):
    """{month: count} → 12-list."""
    out = [0] * 12
    for m, c in pairs:
        out[m - 1] = c
    return out


def test_summer_visitor_like_the_columbus_oriole():
    m = months((1, 1), (2, 1), (3, 2), (4, 161), (5, 783), (6, 176), (7, 43), (8, 38), (9, 4), (10, 2))
    lab = seasonality.season_label(m)
    assert lab["kind"] == "summer"
    assert lab["label"] == "Summer visitor · Apr–Aug"
    assert lab["present"] == [False, False, False, True, True, True, False, False, False, False, False, False] or \
        lab["present"][3:6] == [True, True, True]
    assert lab["peak_month"] == 5


def test_winter_visitor_wraps_the_year():
    m = months((11, 40), (12, 90), (1, 100), (2, 80), (3, 30), (6, 1))
    lab = seasonality.season_label(m)
    assert lab["kind"] == "winter"
    assert lab["label"] == "Winter visitor · Nov–Mar"


def test_year_round_resident():
    m = [50, 60, 70, 80, 90, 100, 95, 85, 75, 65, 55, 50]
    lab = seasonality.season_label(m)
    assert lab["kind"] == "resident" and lab["label"] == "Year-round resident"
    assert all(lab["present"])


def test_passage_migrant_has_two_runs():
    m = months((4, 60), (5, 100), (9, 70), (10, 80), (7, 2), (1, 1))
    lab = seasonality.season_label(m)
    assert lab["kind"] == "passage"
    assert lab["label"] == "Passage migrant · Apr–May & Sep–Oct"


def test_sparse_data_is_labelled_as_such():
    lab = seasonality.season_label(months((5, 4), (6, 3)))
    assert lab["kind"] == "sparse"
    assert "few" in lab["label"].lower()
    assert seasonality.season_label([0] * 12)["kind"] == "sparse"


def test_presence_threshold_needs_share_and_count():
    # 15 % of a 20-observation peak is 3, and the March 2 is under the count floor.
    m = months((3, 2), (4, 3), (5, 20), (6, 20), (7, 20), (8, 3))
    lab = seasonality.season_label(m)
    assert lab["present"][2] is False and lab["present"][3] is True and lab["present"][7] is True


# ------------------------------------------------------------ wikipedia sections

SAMPLE = """The Carolina chickadee (Poecile carolinensis) is a small passerine bird.

== Taxonomy ==
It was often placed in the genus Parus.

== Description ==
Adults have a black cap and bib with white sides to the face. Their underparts are white with rusty brown on the flanks; their back is grey. They have a short dark bill, short wings and a moderately long tail. Very similar to the black-capped chickadee, the Carolina chickadee is distinguished by the slightly browner wing.

== Vocalization ==
The song is a four-note whistle. It also has a fast chick-a-dee-dee-dee call.

=== Subsection ===
Extra notes.

== Diet ==
Insects, seeds and berries.

== References ==
Stuff.
"""


def test_parse_sections_picks_known_headings_in_order_and_trims():
    secs = species_info.parse_sections(SAMPLE)
    assert [s["title"] for s in secs] == ["Description", "Voice", "Diet"]
    desc = secs[0]["text"]
    assert desc.startswith("Adults have a black cap")
    assert desc.count(". ") <= 3 and "slightly browner wing" not in desc  # 4th sentence dropped
    assert secs[1]["text"] == "The song is a four-note whistle. It also has a fast chick-a-dee-dee-dee call."
    assert secs[2]["text"] == "Insects, seeds and berries."


def test_parse_sections_handles_empty_and_unknown():
    assert species_info.parse_sections("") == []
    assert species_info.parse_sections("== Etymology ==\nNamed after Carolina.") == []


def test_trim_cuts_a_runaway_sentence_at_a_word():
    long = "word " * 200
    t = species_info._trim(long)
    assert len(t) <= species_info._SECTION_MAX_CHARS + 1 and t.endswith("…")


# ---------------------------------------------------------------------- traits

def test_traits_migration_and_mass():
    row = traits.lookup("Icterus galbula")
    if row is None:  # table not regenerated on this machine
        pytest.skip("AVONET table without the species")
    assert row.get("migration") in ("Sedentary", "Partially migratory", "Migratory", None)
    if row.get("mass_g") is not None:
        assert 10 < row["mass_g"] < 100


# --------------------------------------------------------------------- cache

@pytest.fixture()
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "aviary.db"))
    return db


def test_species_season_cache_round_trip(fresh_db):
    row = {"common_name": "Baltimore Oriole", "taxon_id": 9346, "lat": 39.96, "lon": -82.99,
           "radius_km": 150, "months": list(range(12)), "total": 66, "fetched_at": time.time(), "ok": 1}
    db.put_species_season(row)
    got = db.get_species_season("baltimore oriole")
    assert got["months"] == list(range(12)) and got["total"] == 66 and got["ok"] == 1
    assert seasonality._fresh(got, 39.96, -82.99)
    # Moved house: stale.
    assert not seasonality._fresh(got, 40.5, -82.99)
    # Old fetch: stale.
    got["fetched_at"] -= 40 * 86400
    assert not seasonality._fresh(got, 39.96, -82.99)
    assert db.get_species_season("nobody") is None
