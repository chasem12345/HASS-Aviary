"""The missed Baltimore Oriole (0.36.0): a review item that links a Frigate-labelled event
while it is still in progress must not pre-claim the species for the visit — the verdict's
announcement is the announcement. Plus the start-up repairs and the cooldown gap.

Same in-process harness as test_confirm: MQTT payloads into ``ingest``, a fake service
into ``identify._process``; "announced" means the ref passed ``ingest._announce``.
"""

from __future__ import annotations

import time

import pytest

from app import db, identify, ingest
from conftest import T0, frigate_row
from test_confirm import announced, confirm_env, msg, review, row, run, svc  # noqa: F401
from test_visits import event_msg, fresh_db, review_msg  # noqa: F401
from test_subjects_store import service_result, store


def test_confirm_verdict_announces_when_the_review_item_linked_it_first(confirm_env):
    """2026-09-20, 17:55: Frigate named the oriole on its second message, the review item
    listed it a moment later, the verdict landed after `end` — and nothing notified,
    because linking a *named* member had claimed (visit, Baltimore Oriole) already."""
    ingest.handle_frigate(msg("o1", "new"))
    ingest.handle_frigate(msg("o1", "update", label="Baltimore Oriole", score=0.96))
    ingest.handle_frigate_review(review("r-oriole", "new", ["o1"], T0))
    v = db.visit_by_review_id("r-oriole")
    # Linking a member that has a name but no announcement claims nothing.
    assert row("o1")["visit_id"] == v["id"] and row("o1")["announced_at"] is None
    ingest.handle_frigate(msg("o1", "end", end=T0 + 3, label="Baltimore Oriole", score=0.96))
    queued = confirm_env.hooked[-1]
    assert row("o1")["id_status"] == "confirming" and not announced("o1")

    confirm_env.fake.answers = [svc("Baltimore Oriole", 0.8,
                                    [("Baltimore Oriole", 0.8), ("Orchard Oriole", 0.1)],
                                    target="Baltimore Oriole")]
    run(identify._process(queued))  # noqa: SLF001
    r = row("o1")
    assert r["id_status"] == "ok" and announced("o1")
    assert r["announced_at"] is not None
    # The verdict took the claim; a later sibling of the same species stays silent.
    assert not db.claim_visit_announcement(v["id"], "Baltimore Oriole", None)
    ingest.handle_frigate_review(review("r-oriole", "update", ["o1", "o2"], T0))
    ingest.handle_frigate(msg("o2", "end", start=T0 + 5, end=T0 + 8,
                              label="Baltimore Oriole", score=0.9))
    confirm_env.fake.answers = [svc("Baltimore Oriole", 0.8,
                                    [("Baltimore Oriole", 0.8), ("Orchard Oriole", 0.1)],
                                    target="Baltimore Oriole")]
    run(identify._process(confirm_env.hooked[-1]))  # noqa: SLF001
    assert row("o2")["id_status"] == "ok" and not announced("o2")


def test_member_announced_per_event_before_the_link_is_still_seeded(fresh_db):
    """The case the link-time seed exists for keeps working: announced on its own `end`
    (no identifier), linked afterwards — its species counts as announced for the visit."""
    ingest.handle_frigate(event_msg("a", "end", T0, T0 + 3, label="Northern Cardinal"))
    assert db.detection_by_ref("frigate", "a")["announced_at"] is not None
    ingest.handle_frigate_review(review_msg("r1", "update", ["a", "b"], T0))
    v = db.visit_by_review_id("r1")
    assert not db.claim_visit_announcement(v["id"], "Northern Cardinal", None)


def test_startup_seed_skips_events_still_in_progress_and_repair_releases_premature_claims(fresh_db):
    # An event named by Frigate mid-track, linked, not ended: nothing has announced it.
    ingest.handle_frigate(event_msg("p", "new", T0))
    ingest.handle_frigate(event_msg("p", "update", T0, label="Blue Jay"))
    ingest.handle_frigate_review(review_msg("r2", "new", ["p"], T0))
    v = db.visit_by_review_id("r2")
    assert db.seed_visit_announcements(T0 - 10) == 0
    # What 0.33–0.35's link-time seed did: claimed the pair for the still-confirming
    # member itself. The start-up repair releases exactly that.
    p_id = db.detection_by_ref("frigate", "p")["id"]
    db.set_identification("frigate", "p", "confirming")
    assert db.claim_visit_announcement(v["id"], "Blue Jay", p_id)
    assert db.unseed_pending_claims() == 1
    assert db.claim_visit_announcement(v["id"], "Blue Jay", None)     # free again
    # A settled row's claim is not touched.
    db.set_identification("frigate", "p", "ok", 0.9, 0.5)
    assert db.unseed_pending_claims() == 0
    assert not db.claim_visit_announcement(v["id"], "Blue Jay", None)
    # Legacy rows (announced before the stamp existed) are still seeded by status.
    ingest.handle_frigate(event_msg("q", "end", T0 + 20, T0 + 23, label="Carolina Wren"))
    ingest.handle_frigate_review(review_msg("r2", "update", ["p", "q"], T0))
    with db._connect() as conn:  # noqa: SLF001
        conn.execute("UPDATE detections SET announced_at = NULL WHERE source_ref = 'q'")
        conn.execute("DELETE FROM visit_announced WHERE common_name = 'Carolina Wren'")
    assert db.seed_visit_announcements(T0 - 10) == 1
    # And the in-memory restart seed leaves in-progress events out too: q (ended and
    # announced) is pre-marked; z (named mid-track, no end yet) and p (never ended, never
    # announced — set 'ok' by hand above) are not.
    ingest.handle_frigate(event_msg("z", "update", T0 + 30, label="Blue Jay"))
    assert {r for _, r in db.recent_refs(T0 - 10)} == {"q"}


def test_quiet_gap_excludes_unlinked_recent_siblings(fresh_db):
    """A burst of fragments whose review message has not arrived yet is one stay: the
    second fragment must not report the species as last seen four seconds ago."""
    ingest.handle_frigate(event_msg("a", "end", T0, T0 + 3, label="Blue Jay"))
    assert db.species_last_times("Blue Jay", "frigate", "b", None, T0 + 5)["seen"] is None
    # Far enough apart it IS a previous sighting.
    assert db.species_last_times("Blue Jay", "frigate", "b", None, T0 + 300)["seen"] == \
        pytest.approx(T0)
    # Without a start time the old behaviour stands.
    assert db.species_last_times("Blue Jay", "frigate", "b")["seen"] == pytest.approx(T0)


def test_species_known_only_as_an_other_bird_has_a_last_seen_time(env):
    """Before 0.36.0 such a species read as 'First sighting!' on every announcement."""
    store(env, service_result())   # a Carolina Wren beside the tracked cardinal at T0
    last = db.species_last_times("Carolina Wren", "frigate", "some-other-event")
    assert last["seen"] == pytest.approx(T0) and last["heard"] is None
    # The parent event itself is excluded, as the tracked bird's own row would be.
    assert db.species_last_times("Carolina Wren", "frigate", "ev1")["seen"] is None


def test_mark_announced_is_idempotent_and_first_wins(fresh_db):
    db.upsert_detection(frigate_row("m1"))
    db.mark_announced("frigate", "m1", when=100.0)
    db.mark_announced("frigate", "m1", when=200.0)
    assert db.detection_by_ref("frigate", "m1")["announced_at"] == 100.0
    assert time.time() > 100.0
