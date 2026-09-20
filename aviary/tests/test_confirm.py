"""Frigate classifies, aviary-id confirms (0.33.0): routing, the verdict, the fallbacks,
and every ordering Frigate's late-arriving sub_label can produce.

Drives ``ingest.handle_frigate`` with MQTT payloads and ``identify._process`` with a fake
service — no broker, no GPU. "Announced" means the ref reached ``ingest._announce`` past
its dedupe, which is exactly when a notification would go out.
"""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

from app import db, identify, ingest, probe
from conftest import MODEL, T0, emb, make_settings, unit

CAM = "birdzone"
JPEG = base64.b64encode(b"\xff\xd8\xff\xd9").decode()
A, B = unit(1), unit(2)  # species "bases" for synthetic embeddings


class FakeService:
    def __init__(self):
        self.answers: list = []
        self.calls: list[dict] = []
        self.raise_exc: Exception | None = None

    async def __call__(self, event_id, priors, exclude=None, zoom=None, target=None,
                       target_subject=0, **kw):
        self.calls.append({"event_id": event_id, "target": target, "zoom": zoom,
                           "exclude": list(exclude or []), **kw})
        if self.raise_exc:
            raise self.raise_exc
        return self.answers.pop(0) if self.answers else None


def svc(name: str, score: float, candidates: list[tuple[str, float]], *,
        embedding: str | None = None, target: str | None = None, status: str = "ok",
        frigate_label: str | None = None, frigate_label_score: float | None = None) -> dict:
    out = {
        "status": status, "common_name": name, "score": score,
        "margin": score - (candidates[1][1] if len(candidates) > 1 else 0.0),
        "runner_up": candidates[1][0] if len(candidates) > 1 else None,
        "candidates": [{"common_name": n, "score": s} for n, s in candidates],
        "embedding": embedding or emb(7), "embedding_key": MODEL,
        "model_version": f"{MODEL}/common@x", "best_crop": JPEG, "frames_used": 2,
        "elapsed_ms": 250, "mode": "confirm",
    }
    if target is not None:
        out["embedding_target"] = target
        out["embedding_target_score"] = 0.5
    if frigate_label:
        out["frigate_label"] = frigate_label
        out["frigate_label_score"] = frigate_label_score
    return out


def msg(ref: str, kind: str, start: float = T0, end: float | None = None,
        label: str | None = None, score: float | None = None) -> bytes:
    sub = [label, score] if (label and score is not None) else label
    after = {"id": ref, "camera": CAM, "label": "bird", "sub_label": sub, "top_score": 0.8,
             "start_time": start, "end_time": end if kind == "end" else None,
             "has_clip": True, "has_snapshot": True, "entered_zones": ["bath"]}
    return json.dumps({"type": kind, "before": after, "after": after}).encode()


def review(review_id: str, kind: str, refs: list[str], start: float,
           end: float | None = None) -> bytes:
    after = {"id": review_id, "camera": CAM, "start_time": start,
             "end_time": end if kind == "end" else None, "severity": "detection",
             "data": {"detections": refs, "objects": ["bird"], "zones": ["bath"],
                      "sub_labels": [], "audio": []}}
    return json.dumps({"type": kind, "before": after, "after": after}).encode()


def row(ref: str) -> dict:
    return db.detection_by_ref("frigate", ref)


def announced(ref: str) -> bool:
    return f"frigate:{ref}" in ingest._announced_refs  # noqa: SLF001


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def confirm_env(env, monkeypatch, tmp_path):
    hooked: list[dict] = []

    def hook(r):
        hooked.append(dict(r))
        return True

    ingest.set_identify_hook(hook)
    ingest.set_confirm_capable(lambda: True)
    ingest.set_late_label_hook(identify.resolve_late_label)
    with ingest._known_lock:  # noqa: SLF001
        ingest._known_species.clear()  # noqa: SLF001
        ingest._announced_refs.clear()  # noqa: SLF001
        ingest._tombstones.clear()  # noqa: SLF001
        ingest._blacklist.clear()  # noqa: SLF001
    monkeypatch.setattr(ingest.notify, "enabled", lambda: False)
    fake = FakeService()
    monkeypatch.setattr(identify, "_call_service", fake)
    yield SimpleNamespace(hooked=hooked, fake=fake, tmp_path=tmp_path)
    ingest.set_identify_hook(None)
    ingest.set_confirm_capable(None)
    ingest.set_late_label_hook(None)


def confirm(ce, ref: str, label="Northern Cardinal", score=0.93) -> dict:
    """An event Frigate named, ended: the row the confirm path queued."""
    ingest.handle_frigate(msg(ref, "new"))
    ingest.handle_frigate(msg(ref, "end", end=T0 + 5, label=label, score=score))
    return ce.hooked[-1]


def teach(ref: str, species: str, embedding: str, start: float = T0) -> int:
    """A hand-named, embedded detection: what the probe learns from."""
    db.upsert_detection({**row_template(ref, start)})
    det_id = row(ref)["id"]
    db.set_identification("frigate", ref, "ok", 0.9, 0.5, f"{MODEL}/common@x", embedding,
                          True, None, embedding_model=MODEL, embedding_target=species)
    db.set_species_manually(det_id, species, None)
    db.confirm_species(species)
    return det_id


def row_template(ref: str, start: float) -> dict:
    return {"source": "frigate", "source_ref": ref, "common_name": "bird",
            "scientific_name": None, "species_code": None, "confidence": 0.8,
            "location": CAM, "zone": "bath", "start_time": start, "end_time": start + 5,
            "has_clip": 1, "has_snapshot": 1, "clip_ref": ref, "snapshot_ref": ref,
            "native_id": ref, "raw_json": None, "created_at": start}


# ----------------------------------------------------------------------------- routing

def test_end_with_frigates_label_is_held_for_confirmation(confirm_env):
    queued = confirm(confirm_env, "c1")
    r = row("c1")
    assert r["id_status"] == "confirming"
    assert r["common_name"] == "Northern Cardinal"          # visible straight away
    assert (r["frigate_label"], r["frigate_score"]) == ("Northern Cardinal", 0.93)
    assert queued["_mode"] == "confirm" and queued["frigate_label"] == "Northern Cardinal"
    assert not announced("c1")                                # ...but not announced yet


def test_named_update_before_end_neither_announces_nor_preempts(confirm_env):
    ingest.handle_frigate(msg("c2", "new"))
    ingest.handle_frigate(msg("c2", "update", label="Northern Cardinal", score=0.91))
    r = row("c2")
    assert r["common_name"] == "Northern Cardinal" and r["id_status"] is None
    assert not announced("c2") and not confirm_env.hooked
    ingest.handle_frigate(msg("c2", "end", end=T0 + 5, label="Northern Cardinal", score=0.93))
    assert row("c2")["id_status"] == "confirming" and len(confirm_env.hooked) == 1
    assert not announced("c2")


def test_plain_bird_end_still_takes_the_full_route(confirm_env):
    ingest.handle_frigate(msg("c3", "end", end=T0 + 5))
    assert row("c3")["id_status"] == "pending"
    assert "_mode" not in confirm_env.hooked[-1]


def test_without_a_capable_service_frigates_label_announces_at_end(confirm_env):
    ingest.set_confirm_capable(lambda: False)
    ingest.handle_frigate(msg("c4", "end", end=T0 + 5, label="Northern Cardinal", score=0.93))
    r = row("c4")
    assert r["id_status"] is None and r["common_name"] == "Northern Cardinal"
    assert r["frigate_label"] == "Northern Cardinal"
    assert announced("c4") and not confirm_env.hooked


def test_duplicate_end_for_a_confirming_row_does_not_announce(confirm_env):
    confirm(confirm_env, "c5")
    ingest.handle_frigate(msg("c5", "end", end=T0 + 5, label="Northern Cardinal", score=0.95))
    r = row("c5")
    assert r["id_status"] == "confirming" and not announced("c5")
    assert r["frigate_score"] == 0.95 and len(confirm_env.hooked) == 1


def test_blacklisted_frigate_label_goes_to_full_identification(confirm_env):
    ingest.add_blacklist("Rock Pigeon", "Columba livia")
    ingest.handle_frigate(msg("c6", "end", end=T0 + 5, label="Rock Pigeon", score=0.97))
    r = row("c6")
    assert r["common_name"] == "bird" and r["id_status"] == "pending"
    assert r["frigate_label"] == "Rock Pigeon"                # provenance survives
    assert "_mode" not in confirm_env.hooked[-1]


# ----------------------------------------------------------------------------- verdict

def test_service_agrees_frigate_stands_and_announces_once(confirm_env):
    queued = confirm(confirm_env, "v1")
    confirm_env.fake.answers = [svc("Northern Cardinal", 0.72,
                                    [("Northern Cardinal", 0.72), ("Summer Tanager", 0.1)],
                                    target="Northern Cardinal")]
    run(identify._process(queued))  # noqa: SLF001
    r = row("v1")
    assert r["id_status"] == "ok" and r["common_name"] == "Northern Cardinal"
    assert r["confidence"] == pytest.approx(0.93)             # Frigate's own confidence
    assert r["id_score"] == pytest.approx(0.72) and r["id_margin"] == pytest.approx(0.62)
    assert announced("v1")
    call = confirm_env.fake.calls[0]
    assert call["mode"] == "confirm" and call["zoom"] is None
    assert call["frigate_label"] == "Northern Cardinal" and call["frigate_score"] == 0.93
    assert call["target"] == "Northern Cardinal"
    assert len(confirm_env.fake.calls) == 1                    # agreement: no clip run
    assert db.embedding_target_for(r["id"]) == (True, "Northern Cardinal")
    # The re-announce guard holds: a second pass through store_row is silent.
    before = len(ingest._announced_refs)  # noqa: SLF001
    ingest.store_row(dict(r), True, True)
    assert len(ingest._announced_refs) == before  # noqa: SLF001


def test_disagreement_escalates_to_the_clip_and_a_weak_clip_leaves_frigate_standing(confirm_env):
    queued = confirm(confirm_env, "v2")
    confirm_env.fake.answers = [
        svc("House Finch", 0.46, [("House Finch", 0.46), ("Northern Cardinal", 0.12)]),  # crops
        svc("House Finch", 0.20, [("House Finch", 0.20), ("Northern Cardinal", 0.15)]),  # clip
    ]
    run(identify._process(queued))  # noqa: SLF001
    calls = confirm_env.fake.calls
    assert len(calls) == 2 and calls[0]["mode"] == "confirm" and "mode" not in calls[1]
    r = row("v2")
    assert r["common_name"] == "Northern Cardinal" and r["id_status"] == "ok"
    assert r["id_score"] == pytest.approx(0.15)               # honest number for the label
    assert r["confidence"] == pytest.approx(0.93)
    shortlist = json.loads(r["id_candidates"])
    assert shortlist[0]["name"] == "House Finch"              # what aviary-id would have said
    assert announced("v2")


def test_disagreement_is_settled_by_a_confident_clip(confirm_env):
    """Tonight's cardinal: Frigate said Vermilion Flycatcher 0.80, the crop said cardinal
    0.46, the clip said cardinal 0.92 with three frames agreeing. The clip wins."""
    queued = confirm(confirm_env, "v5", label="Vermilion Flycatcher", score=0.80)
    confirm_env.fake.answers = [
        svc("Northern Cardinal", 0.46, [("Northern Cardinal", 0.46), ("Eastern Screech-Owl", 0.27)]),
        svc("Northern Cardinal", 0.92, [("Northern Cardinal", 0.92), ("Tufted Titmouse", 0.01)]),
    ]
    run(identify._process(queued))  # noqa: SLF001
    assert len(confirm_env.fake.calls) == 2
    r = row("v5")
    assert r["common_name"] == "Northern Cardinal" and r["id_status"] == "ok"
    assert r["id_score"] == pytest.approx(0.92)
    assert r["frigate_label"] == "Vermilion Flycatcher"       # what Frigate said, kept
    assert announced("v5")
    assert ingest._announced_refs.get("frigate:v5") is None or True  # noqa: SLF001
    assert sum(1 for k in ingest._announced_refs if k.startswith("frigate:v5")) == 1  # noqa: SLF001


def test_escalated_clip_failure_keeps_frigates_label(confirm_env):
    queued = confirm(confirm_env, "v6")
    confirm_env.fake.answers = [
        svc("House Finch", 0.5, [("House Finch", 0.5), ("Northern Cardinal", 0.1)]),
        None,                                                 # the clip run failed
    ]
    run(identify._process(queued))  # noqa: SLF001
    r = row("v6")
    assert r["id_status"] == "frigate" and r["common_name"] == "Northern Cardinal"
    assert announced("v6")


def test_learned_birds_override_frigate(confirm_env):
    for i in range(5):
        teach(f"hf{i}", "House Finch", emb(20 + i, B, 0.05), start=T0 - 1000 + i * 100)
    probe.rebuild(MODEL)
    queued = confirm(confirm_env, "v3")
    confirm_env.fake.answers = [svc("Northern Cardinal", 0.5,
                                    [("Northern Cardinal", 0.5), ("House Finch", 0.3)],
                                    embedding=emb(99, B, 0.05), target="Northern Cardinal")]
    run(identify._process(queued))  # noqa: SLF001
    r = row("v3")
    assert r["common_name"] == "House Finch" and r["id_status"] == "ok"
    assert r["id_probe_examples"] == 5 and r["id_probe_weight"] > 0
    assert r["confidence"] == pytest.approx(r["id_score"])  # the learned answer's score
    assert r["frigate_label"] == "Northern Cardinal"           # ...and what Frigate said
    names = [c["name"] for c in json.loads(r["id_candidates"])]
    assert names[0] == "House Finch" and "Northern Cardinal" in names
    assert announced("v3")


def test_probe_disagreement_below_threshold_leaves_frigate_standing(confirm_env, tmp_path):
    for i in range(5):
        teach(f"hf{i}", "House Finch", emb(20 + i, B, 0.05), start=T0 - 1000 + i * 100)
    probe.rebuild(MODEL)
    identify.configure(make_settings(tmp_path, identify_min_margin=0.95))
    try:
        queued = confirm(confirm_env, "v4")
        confirm_env.fake.answers = [svc("Northern Cardinal", 0.5,
                                        [("Northern Cardinal", 0.5), ("House Finch", 0.3)],
                                        embedding=emb(99, B, 0.05))]
        run(identify._process(queued))  # noqa: SLF001
    finally:
        identify.configure(make_settings(tmp_path))
    r = row("v4")
    assert r["common_name"] == "Northern Cardinal" and r["id_status"] == "ok"
    assert announced("v4")


# --------------------------------------------------------------------------- fallbacks

@pytest.mark.parametrize("answer", [None, {"status": "no_media"}, {"status": "error"}])
def test_no_usable_answer_announces_frigates_label_as_is(confirm_env, answer):
    queued = confirm(confirm_env, "f1")
    confirm_env.fake.answers = [answer]
    run(identify._process(queued))  # noqa: SLF001
    r = row("f1")
    assert r["id_status"] == "frigate" and r["common_name"] == "Northern Cardinal"
    assert r["confidence"] == pytest.approx(0.93)
    assert not db.has_embedding(r["id"])                       # never a learning example
    assert announced("f1")


def test_worker_exception_on_a_confirm_row_falls_back(confirm_env):
    async def scenario():
        settings = identify._settings  # noqa: SLF001
        await identify.start(asyncio.get_running_loop())
        try:
            queued = confirm(confirm_env, "f2")
            confirm_env.fake.raise_exc = RuntimeError("boom")
            assert identify.submit(queued)
            await asyncio.sleep(0)          # submit enqueues via call_soon_threadsafe
            await identify._queue.join()  # noqa: SLF001
        finally:
            await identify.stop()
            identify.configure(settings)
    run(scenario())
    r = row("f2")
    assert r["id_status"] == "frigate" and announced("f2")


def test_confirmations_jump_the_queue(confirm_env):
    async def scenario():
        identify._loop = asyncio.get_running_loop()  # noqa: SLF001
        identify._queue = asyncio.PriorityQueue()  # noqa: SLF001
        full1 = {"source": "frigate", "source_ref": "q1", "common_name": "bird"}
        full2 = {"source": "frigate", "source_ref": "q2", "common_name": "bird"}
        conf = {"source": "frigate", "source_ref": "q3", "common_name": "Northern Cardinal",
                "_mode": "confirm"}
        for r in (full1, full2, conf):
            assert identify.submit(r)
        await asyncio.sleep(0)
        order = [identify._queue.get_nowait()[2]["source_ref"] for _ in range(3)]  # noqa: SLF001
        for r in (full1, full2, conf):
            identify._discard(r["source_ref"])  # noqa: SLF001
        identify._queue = None  # noqa: SLF001
        return order
    assert run(scenario()) == ["q3", "q1", "q2"]


# --------------------------------------------------------------- late labels (ordering)

def classification(ref: str, label: str, score: float) -> bytes:
    return json.dumps({"type": "classification", "id": ref, "camera": CAM,
                       "timestamp": T0 + 9, "model": "bird", "sub_label": label,
                       "score": score}).encode()


def test_late_label_on_an_uncertain_row_resolves_locally(confirm_env):
    ingest.handle_frigate(msg("l1", "end", end=T0 + 5))
    queued = confirm_env.hooked[-1]
    confirm_env.fake.answers = [svc("House Finch", 0.2,
                                    [("House Finch", 0.2), ("Northern Cardinal", 0.15)])]
    run(identify._process(queued))  # noqa: SLF001
    assert row("l1")["id_status"] == "low_confidence" and not announced("l1")
    ingest.handle_frigate_object_update(classification("l1", "Northern Cardinal", 0.94))
    r = row("l1")
    assert r["id_status"] == "ok" and r["common_name"] == "Northern Cardinal"
    assert (r["frigate_label"], r["frigate_score"]) == ("Northern Cardinal", 0.94)
    assert r["confidence"] == pytest.approx(0.94)
    assert announced("l1") and len(confirm_env.fake.calls) == 1   # no second GPU pass


def test_late_label_on_a_failed_row_is_accepted(confirm_env):
    ingest.handle_frigate(msg("l2", "end", end=T0 + 5))
    queued = confirm_env.hooked[-1]
    confirm_env.fake.answers = [None]
    run(identify._process(queued))  # noqa: SLF001
    assert row("l2")["id_status"] == "failed"
    ingest.handle_frigate_object_update(classification("l2", "Northern Cardinal", 0.9))
    r = row("l2")
    assert r["id_status"] == "frigate" and r["common_name"] == "Northern Cardinal"
    assert announced("l2")


def test_late_label_on_a_settled_row_is_provenance_only(confirm_env):
    ingest.handle_frigate(msg("l3", "end", end=T0 + 5))
    queued = confirm_env.hooked[-1]
    confirm_env.fake.answers = [svc("House Finch", 0.8,
                                    [("House Finch", 0.8), ("Northern Cardinal", 0.1)])]
    run(identify._process(queued))  # noqa: SLF001
    assert row("l3")["common_name"] == "House Finch" and announced("l3")
    n = len(ingest._announced_refs)  # noqa: SLF001
    ingest.handle_frigate_object_update(classification("l3", "Northern Cardinal", 0.9))
    r = row("l3")
    assert r["common_name"] == "House Finch" and r["id_status"] == "ok"
    assert r["frigate_label"] == "Northern Cardinal"
    assert len(ingest._announced_refs) == n  # noqa: SLF001


def test_late_label_while_pending_is_used_when_the_answer_is_uncertain(confirm_env):
    ingest.handle_frigate(msg("l4", "end", end=T0 + 5))
    queued = confirm_env.hooked[-1]
    assert ingest.apply_frigate_label("l4", "Northern Cardinal", 0.92) == "recorded"
    assert row("l4")["id_status"] == "pending" and not announced("l4")
    confirm_env.fake.answers = [svc("House Finch", 0.2,
                                    [("House Finch", 0.2), ("Northern Cardinal", 0.15)])]
    run(identify._process(queued))  # noqa: SLF001
    r = row("l4")
    assert r["id_status"] == "ok" and r["common_name"] == "Northern Cardinal"
    assert r["id_score"] == pytest.approx(0.15) and announced("l4")


def test_service_echo_of_frigates_label_counts_as_a_late_label(confirm_env):
    ingest.handle_frigate(msg("l5", "end", end=T0 + 5))
    queued = confirm_env.hooked[-1]
    confirm_env.fake.answers = [svc("House Finch", 0.2,
                                    [("House Finch", 0.2), ("Northern Cardinal", 0.15)],
                                    frigate_label="Northern Cardinal", frigate_label_score=0.9)]
    run(identify._process(queued))  # noqa: SLF001
    r = row("l5")
    assert r["common_name"] == "Northern Cardinal" and r["id_status"] == "ok"
    assert (r["frigate_label"], r["frigate_score"]) == ("Northern Cardinal", 0.9)


def test_rejected_species_is_not_revived_by_a_late_label(confirm_env):
    ingest.handle_frigate(msg("l6", "end", end=T0 + 5))
    queued = confirm_env.hooked[-1]
    confirm_env.fake.answers = [svc("House Finch", 0.2,
                                    [("House Finch", 0.2), ("Northern Cardinal", 0.15)])]
    run(identify._process(queued))  # noqa: SLF001
    db.reject_identification(row("l6")["id"], "Northern Cardinal")
    ingest.handle_frigate_object_update(classification("l6", "Northern Cardinal", 0.9))
    r = row("l6")
    assert r["id_status"] == "low_confidence" and r["common_name"] == "bird"
    assert not announced("l6")


def test_late_label_for_an_unknown_event_is_ignored(confirm_env):
    assert ingest.apply_frigate_label("nope", "Northern Cardinal", 0.9) == "ignored"
    assert ingest.apply_frigate_label("nope", "bird", 0.9) == "ignored"


# ------------------------------------------------------------- visits, restarts, upserts

def test_one_announcement_per_species_per_visit_across_confirmations(confirm_env, monkeypatch):
    claimed: list[tuple[int, str]] = []
    real = db.claim_visit_announcement

    def spy(visit_id, name, det_id):
        ok = real(visit_id, name, det_id)
        if ok:
            claimed.append((visit_id, name))
        return ok
    monkeypatch.setattr(db, "claim_visit_announcement", spy)

    members = [("a", "Northern Cardinal"), ("b", "Northern Cardinal"),
               ("c", "Carolina Wren"), ("d", "Northern Cardinal")]
    listed: list[str] = []
    for i, (ref, label) in enumerate(members):
        listed.append(ref)
        ingest.handle_frigate_review(review("r1", "new" if i == 0 else "update", list(listed), T0))
        queued = confirm(confirm_env, ref, label=label)
        confirm_env.fake.answers = [svc(label, 0.7, [(label, 0.7), ("Blue Jay", 0.1)])]
        run(identify._process(queued))  # noqa: SLF001
    ingest.handle_frigate_review(review("r1", "end", listed, T0, T0 + 40))
    assert [name for _, name in claimed] == ["Northern Cardinal", "Carolina Wren"]


def test_restart_requeues_confirming_rows_and_keeps_their_announcement(confirm_env):
    confirm(confirm_env, "s1")
    pending = db.pending_identifications()
    assert [p["source_ref"] for p in pending] == ["s1"] and pending[0]["id_status"] == "confirming"
    # Neither restart seed pre-marks a row still waiting on its answer.
    assert ("frigate", "s1") not in db.recent_refs(T0 - 10)
    ingest.handle_frigate_review(review("r2", "end", ["s1"], T0, T0 + 30))
    assert db.seed_visit_announcements(T0 - 10) == 0
    # requeue restores the confirm mode from the status.
    submitted: list[dict] = []
    real_submit = identify.submit
    identify.submit = lambda r: submitted.append(r) or True
    try:
        assert run(identify.requeue_pending()) == 1
    finally:
        identify.submit = real_submit
    assert submitted[0]["_mode"] == "confirm"
    assert submitted[0]["common_name"] == "Northern Cardinal"


def test_reimport_cannot_undo_a_settled_species(confirm_env):
    for i in range(5):
        teach(f"hf{i}", "House Finch", emb(20 + i, B, 0.05), start=T0 - 1000 + i * 100)
    probe.rebuild(MODEL)
    queued = confirm(confirm_env, "u1")
    confirm_env.fake.answers = [svc("Northern Cardinal", 0.5,
                                    [("Northern Cardinal", 0.5), ("House Finch", 0.3)],
                                    embedding=emb(99, B, 0.05))]
    run(identify._process(queued))  # noqa: SLF001
    settled = row("u1")
    assert settled["common_name"] == "House Finch"
    # Backfill (and any later Frigate message) re-reports Frigate's label, non-authoritative.
    again = ingest.build_frigate_row({"id": "u1", "label": "bird", "camera": CAM,
                                      "sub_label": "Northern Cardinal", "top_score": 0.99,
                                      "start_time": T0, "end_time": T0 + 5,
                                      "has_clip": True, "has_snapshot": True,
                                      "data": {"sub_label_score": 0.97}})
    assert ingest.store_row(again, live=False)
    r = row("u1")
    assert r["common_name"] == "House Finch"
    assert r["confidence"] == pytest.approx(settled["confidence"])
    assert r["frigate_score"] == pytest.approx(0.97)           # provenance still updates
    # A hand label is protected the same way.
    db.set_species_manually(r["id"], "Carolina Wren", None)
    assert ingest.store_row(again, live=False)
    assert row("u1")["common_name"] == "Carolina Wren" and row("u1")["confidence"] is None


def test_frigate_row_builder_keeps_the_classification_score(confirm_env):
    mqtt = ingest.build_frigate_row({"id": "b1", "label": "bird", "camera": CAM,
                                     "sub_label": ["Northern Cardinal", 0.93],
                                     "top_score": 0.8, "start_time": T0})
    assert (mqtt["common_name"], mqtt["frigate_label"], mqtt["frigate_score"]) == \
        ("Northern Cardinal", "Northern Cardinal", 0.93)
    assert mqtt["confidence"] == 0.8                            # the object score, unchanged
    http = ingest.build_frigate_row({"id": "b2", "label": "bird", "camera": CAM,
                                     "sub_label": "Northern Cardinal", "start_time": T0,
                                     "data": {"top_score": 0.8, "sub_label_score": 0.91}})
    assert (http["frigate_label"], http["frigate_score"]) == ("Northern Cardinal", 0.91)
    plain = ingest.build_frigate_row({"id": "b3", "label": "bird", "camera": CAM,
                                      "sub_label": None, "start_time": T0})
    assert (plain["common_name"], plain["frigate_label"], plain["frigate_score"]) == \
        ("bird", None, None)
