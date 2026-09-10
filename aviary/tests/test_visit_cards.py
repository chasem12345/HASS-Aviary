"""Visit card view-model: focus species ordering, subject-aware representatives, the
media fallback. Pure — members are plain dicts shaped like db rows with ``present``
attached the way views._hydrate does it."""

from __future__ import annotations

from app import visits

T0 = 1_788_904_615.0
PAD = 10.0


def member(ref, start, name, score=0.9, has_clip=1, present=None, **extra):
    m = {
        "id": hash(ref) % 100000, "source": "frigate", "source_ref": ref, "common_name": name,
        "scientific_name": None, "confidence": 0.8, "id_score": score, "start_time": start,
        "end_time": start + 4, "has_clip": has_clip, "has_snapshot": 1, "zone": "bath",
    }
    m.update(extra)
    if present is not None:
        m["present"] = present
    return m


def primary(name, score, sci=None):
    return {"common_name": name, "scientific_name": sci, "score": score, "idx": 0, "source": "primary"}


def subject(name, score, idx, sci=None):
    return {"common_name": name, "scientific_name": sci, "score": score, "idx": idx, "source": "subject"}


def visit(members, end=T0 + 60):
    return {"id": 7, "review_id": "r7", "camera": "birdzone", "start_time": T0, "end_time": end,
            "zones": "bath", "members": members}


def two_species():
    # Cardinal appears first (twice), oriole later — the oriole-page complaint.
    return [
        member("c1", T0 + 1, "Northern Cardinal", 0.95),
        member("c2", T0 + 10, "Northern Cardinal", 0.80),
        member("o1", T0 + 20, "Baltimore Oriole", 0.88),
    ]


def test_unfocused_keeps_first_appearance_order():
    v = visits.card_model(visit(two_species()), PAD)
    assert [s["common_name"] for s in v["species"]] == ["Northern Cardinal", "Baltimore Oriole"]
    assert v["focus"] is None
    assert v["others"] == v["species"]
    assert v["fallback"]["source_ref"] == "c1"          # lead rep: highest score
    assert v["species"][0]["count"] == 2


def test_focus_leads_and_others_keep_order():
    v = visits.card_model(visit(two_species()), PAD, focus="Baltimore Oriole")
    assert [s["common_name"] for s in v["species"]] == ["Baltimore Oriole", "Northern Cardinal"]
    assert v["focus"]["common_name"] == "Baltimore Oriole"
    assert [s["common_name"] for s in v["others"]] == ["Northern Cardinal"]
    assert v["fallback"]["source_ref"] == "o1"
    # The play window is still the whole visit.
    assert v["window"]["start"] == T0 - PAD and v["window"]["end"] == T0 + 60 + PAD


def test_focus_is_case_insensitive_and_unknown_focus_is_ignored():
    v = visits.card_model(visit(two_species()), PAD, focus="baltimore oriole")
    assert v["focus"]["common_name"] == "Baltimore Oriole"
    v = visits.card_model(visit(two_species()), PAD, focus="Blue Jay")
    assert v["focus"] is None
    assert [s["common_name"] for s in v["species"]] == ["Northern Cardinal", "Baltimore Oriole"]


def test_focus_present_only_as_a_subject_uses_its_own_crop():
    members = [
        member("c1", T0 + 1, "Northern Cardinal", 0.95,
               present=[primary("Northern Cardinal", 0.95, "Cardinalis cardinalis"),
                        subject("Baltimore Oriole", 0.77, 1, "Icterus galbula")]),
        member("c2", T0 + 10, "Northern Cardinal", 0.80,
               present=[primary("Northern Cardinal", 0.80)]),
    ]
    v = visits.card_model(visit(members), PAD, focus="Baltimore Oriole")
    f = v["focus"]
    assert f is not None and f["count"] == 1
    assert f["rep"]["source_ref"] == "c1"
    assert f["rep"]["subject_idx"] == 1               # the oriole's crop, not the cardinal's
    assert f["rep"]["id_score"] == 0.77
    assert f["scientific_name"] == "Icterus galbula"
    assert [s["common_name"] for s in v["others"]] == ["Northern Cardinal"]


def test_fallback_prefers_a_member_with_a_clip():
    members = [
        member("c1", T0 + 1, "Northern Cardinal", 0.95, has_clip=1),
        member("o1", T0 + 20, "Baltimore Oriole", 0.88, has_clip=0),
    ]
    v = visits.card_model(visit(members), PAD, focus="Baltimore Oriole")
    assert v["species"][0]["common_name"] == "Baltimore Oriole"
    assert v["fallback"]["source_ref"] == "c1"        # the oriole's event has no clip
    # No clip anywhere: the lead's own row still stands in (its snapshot is the hero).
    for m in members:
        m["has_clip"] = 0
    v = visits.card_model(visit(members), PAD, focus="Baltimore Oriole")
    assert v["fallback"]["source_ref"] == "o1"


def test_no_species_falls_back_to_any_member():
    members = [member("u1", T0 + 1, "bird", None, has_clip=0), member("u2", T0 + 5, "bird", None, has_clip=1)]
    v = visits.card_model(visit(members), PAD, focus="Baltimore Oriole")
    assert v["species"] == [] and v["focus"] is None
    assert len(v["unidentified"]) == 2
    assert v["fallback"]["source_ref"] == "u2"
