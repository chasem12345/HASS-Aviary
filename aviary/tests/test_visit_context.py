"""Uncertain fragments inherit a visit sibling's species (identify_visit_context, 0.36.0).

Frigate tracks one stay as many short objects; the two-second tail of a cardinal's visit
comes back uncertain on its own footage. With a settled sibling in the same visit, zone
and moment, and that species on the fragment's own shortlist, the fragment is named
``visit`` — never a learning example, never a second notification. Fake service, no GPU.
"""

from __future__ import annotations

import json

import pytest

from app import db, identify, ingest
from conftest import MODEL, T0, make_settings
from test_confirm import announced, confirm_env, msg, review, row, run, svc  # noqa: F401


def settle(ref: str, species: str, start: float, zone: str = "bath",
           status: str = "ok", claim_visit: str | None = None) -> None:
    """A member identified as ``species``: what a fragment may inherit from."""
    ingest.handle_frigate(msg(ref, "new", start=start))
    ingest.handle_frigate(msg(ref, "end", start=start, end=start + 3))
    with db._connect() as conn:  # noqa: SLF001
        conn.execute("UPDATE detections SET common_name = ?, zone = ? WHERE source_ref = ?",
                     (species, zone, ref))
    db.set_identification("frigate", ref, status, 0.9, 0.5, f"{MODEL}/common@x")
    if claim_visit:
        v = db.visit_by_review_id(claim_visit)
        db.claim_visit_announcement(v["id"], species, row(ref)["id"])


def uncertain(name="Cassin's Sparrow", score=0.25, cardinal=0.2) -> dict:
    out = svc(name, score, [(name, score), ("Northern Cardinal", cardinal),
                            ("Tufted Titmouse", 0.05)])
    out.pop("mode")
    return out


def fragment(confirm_env, ref: str, start: float) -> dict:
    ingest.handle_frigate(msg(ref, "end", start=start, end=start + 2))
    return confirm_env.hooked[-1]


def test_fragment_takes_the_name_of_a_sibling_seconds_away(confirm_env):
    ingest.handle_frigate_review(review("rv", "new", ["s1", "f1"], T0))
    settle("s1", "Northern Cardinal", T0, claim_visit="rv")
    base = db.unidentified_counts()["actionable"]       # the env fixture's own 'bird' row
    queued = fragment(confirm_env, "f1", T0 + 6)
    confirm_env.fake.answers = [uncertain()]
    run(identify._process(queued))  # noqa: SLF001
    r = row("f1")
    assert r["id_status"] == "visit" and r["common_name"] == "Northern Cardinal"
    assert r["confidence"] == pytest.approx(0.2) and r["id_score"] == pytest.approx(0.2)
    assert [c["name"] for c in json.loads(r["id_candidates"])][0] == "Cassin's Sparrow"
    assert not announced("f1")                          # the visit had announced it
    assert db.unidentified_counts()["actionable"] == base
    # Never a learning example, even with learning-from-auto on.
    assert dict(db.confirmed_embeddings(MODEL, include_auto=True)).get("Northern Cardinal") is None
    # Settled: a later Frigate label or a re-import cannot rename it.
    assert ingest.apply_frigate_label("f1", "House Finch", 0.9) == "recorded"
    assert row("f1")["common_name"] == "Northern Cardinal"
    db.upsert_detection({**row("f1"), "common_name": "House Finch"})
    assert row("f1")["common_name"] == "Northern Cardinal"


@pytest.mark.parametrize("why", ["two species", "outside window", "other zone",
                                 "not on shortlist", "option off", "no visit"])
def test_fragment_stays_uncertain_when_the_rule_does_not_apply(confirm_env, why, tmp_path):
    if why != "no visit":
        ingest.handle_frigate_review(review("rv", "new", ["s1", "s2", "f1"], T0))
    settle("s1", "Northern Cardinal", T0 - (90 if why == "outside window" else 0),
           zone="finch" if why == "other zone" else "bath")
    if why == "two species":
        settle("s2", "Carolina Wren", T0 + 2)
    if why == "option off":
        identify.configure(make_settings(tmp_path, identify_visit_context=False))
    base = db.unidentified_counts()["actionable"]
    try:
        queued = fragment(confirm_env, "f1", T0 + 6)
        confirm_env.fake.answers = [uncertain(cardinal=0.05 if why == "not on shortlist" else 0.2)]
        run(identify._process(queued))  # noqa: SLF001
    finally:
        identify.configure(make_settings(tmp_path))
    r = row("f1")
    assert r["id_status"] == "low_confidence" and r["common_name"] == "bird", why
    assert db.unidentified_counts()["actionable"] == base + 1


def test_backlog_pass_names_fragments_already_in_the_review_queue(confirm_env):
    ingest.handle_frigate_review(review("rv", "new", ["s1", "f1", "f2"], T0))
    settle("s1", "Northern Cardinal", T0, claim_visit="rv")
    for ref, start, cardinal in (("f1", T0 + 4, 0.3), ("f2", T0 + 8, 0.02)):
        ingest.handle_frigate(msg(ref, "end", start=start, end=start + 2))
        shortlist = json.dumps([{"name": "Cassin's Sparrow", "score": 0.25},
                                {"name": "Northern Cardinal", "score": cardinal}])
        db.set_identification("frigate", ref, "low_confidence", 0.25, 0.1, f"{MODEL}/common@x",
                              None, False, shortlist, probe_weight=0.3, probe_examples=4)
    assert run(identify.inherit_backlog()) == 1
    f1, f2 = row("f1"), row("f2")
    assert f1["id_status"] == "visit" and f1["common_name"] == "Northern Cardinal"
    assert f1["id_probe_weight"] == pytest.approx(0.3) and f1["id_probe_examples"] == 4
    assert f2["id_status"] == "low_confidence"             # cardinal not plausible here
    assert not announced("f1")
    assert run(identify.inherit_backlog()) == 0            # idempotent


def test_inherited_rows_count_as_siblings_and_carry_the_visit_forward(confirm_env):
    """A fragment named from a sibling can itself name the next fragment: one stay, one
    species, however many objects Frigate cut it into."""
    ingest.handle_frigate_review(review("rv", "new", ["s1", "f1", "f2"], T0))
    settle("s1", "Northern Cardinal", T0, claim_visit="rv")
    for ref, start in (("f1", T0 + 15), ("f2", T0 + 30)):   # f2 is 30 s from s1, 15 from f1
        queued = fragment(confirm_env, ref, start)
        confirm_env.fake.answers = [uncertain()]
        run(identify._process(queued))  # noqa: SLF001
    assert row("f1")["id_status"] == "visit" and row("f2")["id_status"] == "visit"
