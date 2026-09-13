"""Other birds in view ("subjects"): storage, presence, learning and re-identify carry-over.

Drives identify._store_subjects with a fake service response — no GPU, no network — and
checks the invariant the feature exists for: a label on one bird only ever stores THAT
bird's embedding under the name.
"""

from __future__ import annotations

import asyncio
import base64

from app import crops, db, identify

from conftest import MODEL, T0, emb  # noqa: E402,F401 — env fixture comes from conftest


def service_result(wren_emb: str = emb(2), wren_score: float = 0.81) -> dict:
    return {
        "status": "ok", "common_name": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis",
        "species_code": "norcar", "score": 0.9, "margin": 0.7, "embedding": emb(1),
        "embedding_key": MODEL, "model_version": f"{MODEL}/common@abc", "frames_used": 3,
        "candidates": [{"common_name": "Northern Cardinal", "score": 0.9}],
        "subjects": [
            {"idx": 0, "primary": True, "anchored": True, "common_name": "Northern Cardinal",
             "scientific_name": "Cardinalis cardinalis", "species_code": "norcar", "score": 0.9,
             "margin": 0.7, "embedding": emb(1), "n_frames": 3,
             "candidates": [{"common_name": "Northern Cardinal", "score": 0.9}]},
            {"idx": 1, "primary": False, "anchored": True, "common_name": "Carolina Wren",
             "scientific_name": "Thryothorus ludovicianus", "species_code": "carwre",
             "score": wren_score, "margin": 0.6, "embedding": wren_emb, "n_frames": 2,
             "best_crop": base64.b64encode(b"\xff\xd8\xff\xd9").decode(),
             "candidates": [{"common_name": "Carolina Wren", "score": wren_score},
                            {"common_name": "House Wren", "score": 0.1}]},
        ],
    }


def store(row, result):
    asyncio.run(identify._store_subjects(row, result, MODEL))


def test_subjects_are_stored_with_own_crop_and_embedding(env):
    store(env, service_result())
    subs = db.subjects_for(env["id"])
    assert [s["idx"] for s in subs] == [0, 1]
    wren = subs[1]
    assert wren["common_name"] == "Carolina Wren" and wren["id_status"] == "ok"
    assert wren["embedding"] == emb(2) and wren["embedding_model"] == MODEL
    assert wren["crop_file"] == "ev1-1.jpg" and crops.exists("ev1", 1)
    others = db.secondary_subjects_for(env["id"])
    assert others[0]["display_name"] == "Carolina Wren"


def test_low_confidence_other_bird_keeps_shortlist_not_name(env):
    store(env, service_result(wren_score=0.2))
    other = db.secondary_subjects_for(env["id"])[0]
    assert other["id_status"] == "low_confidence" and other["display_name"] is None
    assert "Carolina Wren" in (other["candidates"] or "")
    assert db.unidentified_counts()["subjects"] == 1
    assert [d["id"] for d in db.detections_with_unidentified_subjects()] == [env["id"]]


def test_species_present_and_probe_training_use_each_birds_own_embedding(env):
    store(env, service_result())
    # The primary is what the detection row says; make it a named, confirmed cardinal.
    db.set_identification("frigate", "ev1", "ok", 0.9, 0.7, f"{MODEL}/common@abc", emb(1),
                          True, None, embedding_model=MODEL)
    db.set_species_manually(env["id"], "Northern Cardinal", "Cardinalis cardinalis")
    db.confirm_species("Northern Cardinal")
    db.confirm_species("Carolina Wren")
    present = db.species_present([env["id"]])[env["id"]]
    assert [(p["common_name"], p["idx"]) for p in present] == [("Northern Cardinal", 0), ("Carolina Wren", 1)]
    # Each bird's own embedding, under its own name. The wren's is the service's own
    # (auto) answer, so it only counts when learning from automatic answers is on.
    learned = dict(db.confirmed_embeddings(MODEL, include_auto=True))
    assert learned == {"Northern Cardinal": emb(1), "Carolina Wren": emb(2)}
    assert dict(db.confirmed_embeddings(MODEL)) == {"Northern Cardinal": emb(1)}


def test_labelling_other_bird_never_touches_primary_embedding(env):
    store(env, service_result(wren_score=0.2))
    db.set_identification("frigate", "ev1", "ok", 0.9, 0.7, f"{MODEL}/common@abc", emb(1),
                          True, None, embedding_model=MODEL)
    db.set_species_manually(env["id"], "Northern Cardinal", "Cardinalis cardinalis")
    db.confirm_species("Northern Cardinal")
    assert db.set_subject_species_manually(env["id"], 1, "Carolina Wren", "Thryothorus ludovicianus")
    db.confirm_species("Carolina Wren")
    learned = dict(db.confirmed_embeddings(MODEL))
    # The wren label is backed by the WREN's vector; the cardinal's vector is untouched.
    assert learned["Carolina Wren"] == emb(2) and learned["Northern Cardinal"] == emb(1)
    assert db.secondary_subjects_for(env["id"])[0]["display_name"] == "Carolina Wren"
    assert not db.set_subject_species_manually(env["id"], 0, "x", None)  # primary is off-limits here


def test_reject_other_bird_records_and_clears(env):
    store(env, service_result())
    assert db.reject_subject(env["id"], 1, "Carolina Wren")
    other = db.secondary_subjects_for(env["id"])[0]
    assert other["id_status"] == "rejected" and other["display_name"] is None
    assert other["rejected"] == ["Carolina Wren"]
    assert db.subject_rejections(env["id"]) == {1: ["Carolina Wren"]}


def test_reidentify_carries_label_and_rejection_by_embedding(env):
    store(env, service_result())
    db.set_subject_species_manually(env["id"], 1, "Carolina Wren", None)
    # Same wren re-found under a different idx with a near-identical embedding.
    result = service_result()
    result["subjects"][1]["idx"] = 2
    result["subjects"][1]["common_name"] = "House Wren"   # the service now disagrees...
    store(env, result)
    other = db.secondary_subjects_for(env["id"])[0]
    assert other["idx"] == 2 and other["manual_name"] == "Carolina Wren"   # ...but the label wins
    assert other["id_status"] == "manual"

    # A rejection travels the same way, and rules the species out of the next answer.
    db.reject_subject(env["id"], 2, "House Wren")
    result = service_result()
    result["subjects"][1]["idx"] = 1
    result["subjects"][1]["common_name"] = "House Wren"
    store(env, result)
    other = db.secondary_subjects_for(env["id"])[0]
    assert other["rejected"] == ["House Wren"]
    assert other["common_name"] == "Carolina Wren"   # next candidate not ruled out

    # A genuinely different bird does not inherit anything.
    result = service_result(wren_emb=emb(9))
    store(env, result)
    other = db.secondary_subjects_for(env["id"])[0]
    assert other["manual_name"] is None and other["rejected"] == []


def test_old_service_without_subjects_stores_one_primary_row(env):
    result = service_result()
    result.pop("subjects")
    store(env, result)
    subs = db.subjects_for(env["id"])
    assert len(subs) == 1 and subs[0]["is_primary"] == 1
    assert db.secondary_subjects_for(env["id"]) == []


def test_deleting_detection_removes_subjects(env):
    store(env, service_result())
    db.reject_subject(env["id"], 1, "House Wren")
    db.delete_detection(env["id"])
    assert db.subjects_for(env["id"]) == [] and db.subject_rejections(env["id"]) == {}
