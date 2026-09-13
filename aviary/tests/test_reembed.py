"""Re-embedding for a label: the stored example becomes a frame of the bird that was NAMED.

identify._call_service is replaced with a fake that records what was asked and answers
with a canned response — no GPU, no network.
"""

from __future__ import annotations

import asyncio
import base64

import pytest

from app import crops, db, identify
from conftest import MODEL, emb, unit

JPEG = base64.b64encode(b"\xff\xd8\xff\xd9").decode()


def svc(embedding: str, target: str | None, subjects: list | None = None) -> dict:
    out = {"status": "ok", "common_name": "House Finch", "score": 0.6, "margin": 0.3,
           "embedding": embedding, "embedding_key": MODEL, "model_version": f"{MODEL}/common@x",
           "best_crop": JPEG, "candidates": [{"common_name": "House Finch", "score": 0.6}]}
    if target is not None:
        out["embedding_target"] = target
        out["embedding_target_score"] = 0.42
    if subjects is not None:
        out["subjects"] = subjects
    return out


@pytest.fixture()
def fake(monkeypatch):
    """Install a fake service; ``fake.answers`` is consumed in order, ``fake.calls`` records."""
    class Fake:
        answers: list = []
        calls: list = []

        async def __call__(self, event_id, priors, exclude=None, zoom=None, target=None,
                           target_subject=0):
            self.calls.append({"event_id": event_id, "target": target,
                               "target_subject": target_subject})
            return self.answers.pop(0) if self.answers else None
    f = Fake()
    monkeypatch.setattr(identify, "_call_service", f)
    return f


def stored_embedding(det_id: int):
    with db._connect() as conn:  # noqa: SLF001
        row = conn.execute("SELECT embedding, target FROM identification_embeddings "
                           "WHERE detection_id = ?", (det_id,)).fetchone()
    return (row["embedding"], row["target"]) if row else None


def labelled(env, name="Northern Cardinal"):
    """The row as it stands after a wrong auto answer and a human correction."""
    db.set_identification("frigate", "ev1", "ok", 0.6, 0.3, f"{MODEL}/common@x", emb(1),
                          True, None, embedding_model=MODEL, embedding_target="House Finch")
    db.set_species_manually(env["id"], name, "Cardinalis cardinalis")
    return db.detection_by_id(env["id"])


def test_relabel_replaces_the_frame_and_keeps_the_label(env, fake):
    det = labelled(env)
    assert db.embedding_target_for(env["id"]) == (True, "House Finch")
    fake.answers = [svc(emb(5), "Northern Cardinal")]
    out = asyncio.run(identify.reembed(det, "Northern Cardinal"))
    assert out["status"] == "ok" and out["target"] == "Northern Cardinal"
    assert fake.calls[0]["target"] == "Northern Cardinal" and fake.calls[0]["target_subject"] == 0
    assert stored_embedding(env["id"]) == (emb(5), "Northern Cardinal")
    after = db.detection_by_id(env["id"])
    assert after["common_name"] == "Northern Cardinal" and after["id_status"] == "manual"
    assert crops.exists("ev1")
    assert db.manual_rows_untargeted() == []


def test_old_service_never_replaces_an_existing_frame(env, fake):
    det = labelled(env)
    fake.answers = [svc(emb(5), None)]                       # no embedding_target: pre-0.11
    assert asyncio.run(identify.reembed(det, "Northern Cardinal"))["status"] == "untargeted"
    assert stored_embedding(env["id"]) == (emb(1), "House Finch")
    # ...but does fill a missing one, marked as chosen for the service's winner.
    db.delete_detection_embedding(env["id"])
    fake.answers = [svc(emb(6), None)]
    assert asyncio.run(identify.reembed(det, "Northern Cardinal"))["status"] == "untargeted"
    assert stored_embedding(env["id"]) == (emb(6), None)
    assert [r["id"] for r in db.manual_rows_untargeted()] == [env["id"]]


def test_failure_and_only_if_missing_leave_things_alone(env, fake):
    det = labelled(env)
    fake.answers = [{"status": "no_media"}]
    assert asyncio.run(identify.reembed(det, "Northern Cardinal"))["status"] == "failed"
    assert stored_embedding(env["id"]) == (emb(1), "House Finch")
    assert asyncio.run(identify.reembed(det, "Northern Cardinal", only_if_missing=True))["status"] == "kept"
    assert len(fake.calls) == 1                               # the kept path made no call
    assert asyncio.run(identify.backfill_embedding(det)) is False


def test_other_bird_is_found_by_embedding_and_asked_for_again(env, fake):
    # Store a two-bird event, then name the other bird by hand.
    wren = {"idx": 1, "primary": False, "anchored": True, "common_name": "House Wren",
            "score": 0.5, "margin": 0.2, "embedding": emb(2), "n_frames": 2, "best_crop": JPEG,
            "candidates": [{"common_name": "House Wren", "score": 0.5}]}
    primary = {"idx": 0, "primary": True, "anchored": True, "common_name": "House Finch",
               "score": 0.6, "margin": 0.3, "embedding": emb(1), "n_frames": 3,
               "candidates": [{"common_name": "House Finch", "score": 0.6}]}
    asyncio.run(identify._store_subjects(env, svc(emb(1), None, [primary, wren]), MODEL))
    assert db.set_subject_species_manually(env["id"], 1, "Carolina Wren", None)
    # The service re-numbers: the wren now comes back as subject 2. First call is asked
    # for subject 1, the match is found by cosine, and it is asked again for subject 2.
    new_wren = dict(wren, idx=2, embedding=emb(21, unit(2), 0.05), embedding_target=None)
    honoured = dict(new_wren, embedding_target="Carolina Wren", embedding_target_score=0.7)
    fake.answers = [svc(emb(1), None, [primary, new_wren]), svc(emb(1), None, [primary, honoured])]
    out = asyncio.run(identify.reembed(db.detection_by_id(env["id"]), "Carolina Wren", subject_idx=1))
    assert out["status"] == "ok"
    assert [c["target_subject"] for c in fake.calls] == [1, 2]
    sub = db.secondary_subjects_for(env["id"])[0]
    assert sub["embedding"] == honoured["embedding"] and sub["embedding_target"] == "Carolina Wren"
    assert sub["manual_name"] == "Carolina Wren" and sub["id_status"] == "manual"
    # A genuinely different bird in the new answer is not the one we meant.
    fake.answers = [svc(emb(1), None, [primary, dict(wren, embedding=emb(9))])]
    out = asyncio.run(identify.reembed(db.detection_by_id(env["id"]), "Carolina Wren", subject_idx=1))
    assert out["status"] == "subject_not_found"


def test_learning_flags_follow_a_carried_subject(env, fake):
    wren = {"idx": 1, "primary": False, "anchored": True, "common_name": "Carolina Wren",
            "score": 0.8, "margin": 0.5, "embedding": emb(2), "n_frames": 2, "best_crop": JPEG,
            "candidates": [{"common_name": "Carolina Wren", "score": 0.8}]}
    primary = {"idx": 0, "primary": True, "anchored": True, "common_name": "House Finch",
               "score": 0.6, "margin": 0.3, "embedding": emb(1), "n_frames": 3, "candidates": []}
    asyncio.run(identify._store_subjects(env, svc(emb(1), None, [primary, wren]), MODEL))
    db.set_subject_species_manually(env["id"], 1, "Carolina Wren", None)
    db.set_learning_excluded(env["id"], 1, True)
    asyncio.run(identify._store_subjects(env, svc(emb(1), None, [primary, dict(wren, idx=2)]), MODEL))
    db.confirm_species("Carolina Wren")
    rows = db.learning_examples(MODEL)
    assert [(r["subject_idx"], bool(r["excluded_at"])) for r in rows] == [(2, True)]
    assert db.confirmed_embeddings(MODEL) == []
