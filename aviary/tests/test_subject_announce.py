"""Other birds in view announce (0.35.0): a named secondary subject fires the same
detection event as a tracked bird — once per species per visit, with its own crop, and as a
new species when Aviary has never recorded it. Fake notify; no HA, no GPU.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app import crops, db, identify, ingest, notify
from conftest import MODEL, emb, frigate_row, make_settings
from test_subjects_store import service_result


@pytest.fixture()
def notified(env, monkeypatch):
    sent: list[tuple[dict, bool]] = []

    async def fake_send(row, is_new, test=False):
        sent.append((row, is_new))
        return {"fired": True, "image": None, "error": None}

    monkeypatch.setattr(notify, "enabled", lambda: True)
    monkeypatch.setattr(notify, "send_detection", fake_send)
    with ingest._known_lock:  # noqa: SLF001
        ingest._known_species.clear()  # noqa: SLF001
        ingest._announced_refs.clear()  # noqa: SLF001
    new_hooked: list[str] = []
    ingest.set_new_species_hook(new_hooked.append)
    yield sent, new_hooked
    ingest.set_new_species_hook(None)


def store(row, result, announce=True):
    async def go():
        ingest.set_event_loop(asyncio.get_running_loop())
        await identify._store_subjects(row, result, MODEL, announce=announce)  # noqa: SLF001
        await asyncio.sleep(0.01)  # let the scheduled notify coroutine run
    asyncio.run(go())


def test_named_other_bird_announces_as_a_new_species(notified):
    sent, new_hooked = notified
    row = db.detection_by_ref("frigate", "ev1")
    store(row, service_result())
    assert len(sent) == 1
    payload, is_new = sent[0]
    assert payload["common_name"] == "Carolina Wren" and payload["subject_idx"] == 1
    assert payload["source_ref"] == "ev1" and payload["id"] == row["id"]
    assert payload["confidence"] == pytest.approx(0.81)
    assert is_new is True and new_hooked == ["Carolina Wren"]
    assert "frigate:ev1:1" in ingest._announced_refs  # noqa: SLF001
    assert "frigate:ev1" not in ingest._announced_refs  # noqa: SLF001 — the tracked bird's key is untouched
    assert "carolina wren" in ingest._known_species  # noqa: SLF001
    # The same event again (a re-identify) says nothing more.
    store(row, service_result())
    assert len(sent) == 1


def test_known_species_is_not_new_and_visits_announce_once(notified):
    sent, _ = notified
    with ingest._known_lock:  # noqa: SLF001
        ingest._known_species.add("carolina wren")  # noqa: SLF001
    with db._connect() as conn:  # noqa: SLF001
        conn.execute("UPDATE detections SET visit_id = 42 WHERE source_ref = 'ev1'")
    row = db.detection_by_ref("frigate", "ev1")
    store(row, service_result())
    assert len(sent) == 1 and sent[0][1] is False
    # A second fragment of the same visit with the same wren: silent.
    other = frigate_row("ev2")
    other["common_name"] = "Northern Cardinal"
    db.upsert_detection(other)
    with db._connect() as conn:  # noqa: SLF001
        conn.execute("UPDATE detections SET visit_id = 42 WHERE source_ref = 'ev2'")
    store(db.detection_by_ref("frigate", "ev2"), service_result(wren_emb=emb(3)))
    assert len(sent) == 1
    # And the tracked bird becoming a wren later in that visit is silent too (claimed).
    assert db.claim_visit_announcement(42, "Carolina Wren", None) is False


def test_unnamed_other_bird_and_history_stay_silent(notified):
    sent, _ = notified
    row = db.detection_by_ref("frigate", "ev1")
    store(row, service_result(wren_score=0.2))          # below threshold: no name, no news
    assert sent == []
    store(row, service_result(), announce=False)         # a re-embed of history
    assert sent == []


def test_blacklisted_other_bird_stays_silent(notified):
    sent, _ = notified
    ingest.add_blacklist("Carolina Wren", "Thryothorus ludovicianus")
    try:
        store(db.detection_by_ref("frigate", "ev1"), service_result())
    finally:
        ingest.remove_blacklist("Carolina Wren", "Thryothorus ludovicianus")
    assert sent == []


def test_notification_image_is_the_other_birds_own_crop(env, monkeypatch, tmp_path):
    notify.configure(make_settings(tmp_path, notify_new_species=True))
    crop = tmp_path / "wren.jpg"
    crop.write_bytes(b"\xff\xd8wren\xff\xd9")
    seen: list[tuple] = []
    monkeypatch.setattr(crops, "path_if_exists", lambda ref, idx=0: (seen.append((ref, idx)) or str(crop)) if idx else None)

    async def no_fetch(url):
        raise AssertionError(f"must not fetch the event snapshot for a subject: {url}")
    monkeypatch.setattr(notify, "_fetch_image", no_fetch)
    out = asyncio.run(notify._resolve_image({"source": "frigate", "source_ref": "ev1",  # noqa: SLF001
                                             "subject_idx": 1, "common_name": "Carolina Wren"}))
    assert out == (b"\xff\xd8wren\xff\xd9", "image/jpeg") and seen == [("ev1", 1)]


def test_review_path_points_at_the_species_page_for_a_new_unconfirmed_species(env, monkeypatch, tmp_path):
    notify.configure(make_settings(tmp_path, notify_new_species=True,
                                   require_species_confirmation=True))
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    fired: list[tuple[str, dict]] = []

    async def fake_fire(event_type, payload):
        fired.append((event_type, payload))
        return None

    async def slug():
        return "local_aviary"

    async def no_image(row):
        return None
    monkeypatch.setattr(notify, "_fire_event", fake_fire)
    monkeypatch.setattr(notify, "_panel_slug", slug)
    monkeypatch.setattr(notify, "_resolve_image", no_image)
    row = {**db.detection_by_ref("frigate", "ev1"), "common_name": "Carolina Wren", "subject_idx": 1}

    asyncio.run(notify.send_detection(row, is_new=True))
    payload = fired[0][1]
    assert payload["review_path"] == "/local_aviary/species/Carolina%20Wren"
    assert payload["panel_path"] == f"/local_aviary/detection/{row['id']}"
    assert payload["subject_idx"] == 1 and payload["is_new_species"] is True
    assert [e for e, _ in fired] == ["aviary_detection", "aviary_new_species"]

    fired.clear()
    asyncio.run(notify.send_detection({**row, "subject_idx": None}, is_new=False))
    assert fired[0][1]["review_path"] is None and fired[0][1]["subject_idx"] is None

    db.confirm_species("Carolina Wren")
    fired.clear()
    asyncio.run(notify.send_detection(row, is_new=True))
    assert fired[0][1]["review_path"] is None                 # already confirmed: nothing to review
