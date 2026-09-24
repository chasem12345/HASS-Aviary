"""'Not a bird' (0.37.0): learned negatives set matching crops aside, unnamed and silent.

Same harness as test_confirm: MQTT payloads into ingest, a fake service into
``identify._process``; "announced" means the ref passed ``ingest._announce``.
"""

from __future__ import annotations

import pytest

from app import db, identify, ingest, notbird
from conftest import MODEL, T0, emb, encode, unit
from test_confirm import announced, confirm, confirm_env, msg, row, run, svc  # noqa: F401

LEAF = unit(42)


@pytest.fixture()
def nb_env(confirm_env):
    notbird.configure(0.9)
    notbird.invalidate()
    yield confirm_env
    notbird.configure(0.0)
    notbird.invalidate()


def learn_leaf() -> int:
    ex = db.not_bird_add(MODEL, encode(LEAF), source_ref="old", camera="birdzone",
                         label="Northern Cardinal")
    notbird.invalidate()
    return ex


def queued_unnamed(ce, ref: str) -> dict:
    ingest.handle_frigate(msg(ref, "new"))
    ingest.handle_frigate(msg(ref, "end", end=T0 + 5))
    return ce.hooked[-1]


def test_match_respects_threshold_and_model(nb_env):
    ex = learn_leaf()
    assert notbird.match(emb(1, LEAF, 0.05), MODEL)[0] == ex        # cosine ~0.93
    assert notbird.match(emb(2), MODEL) is None                     # unrelated
    assert notbird.match(emb(1, LEAF, 0.05), "other-model") is None
    notbird.configure(0.0)
    assert notbird.match(encode(LEAF), MODEL) is None               # gate off


def test_full_identification_sets_a_leaf_aside(nb_env):
    learn_leaf()
    queued = queued_unnamed(nb_env, "leaf1")
    nb_env.fake.answers = [svc("Northern Cardinal", 0.9, [("Northern Cardinal", 0.9),
                                                          ("Pyrrhuloxia", 0.05)],
                               embedding=emb(3, LEAF, 0.05))]
    run(identify._process(queued))  # noqa: SLF001
    r = row("leaf1")
    assert r["id_status"] == "not_bird" and r["common_name"] == "bird"
    assert not announced("leaf1")
    # Out of the to-do and the feed, onto the audit list.
    assert db.unidentified_counts()["not_bird"] == 1
    assert all(d["source_ref"] != "leaf1" for d in db.unidentified_detections())
    assert all(d.get("source_ref") != "leaf1" for d in db.feed_page())
    assert [d["source_ref"] for d in db.not_bird_detections()] == ["leaf1"]


def test_confirm_path_resets_frigates_label(nb_env):
    learn_leaf()
    queued = confirm(nb_env, "leaf2")
    assert row("leaf2")["common_name"] == "Northern Cardinal"   # provisional
    nb_env.fake.answers = [svc("Northern Cardinal", 0.8, [("Northern Cardinal", 0.8)],
                               embedding=emb(4, LEAF, 0.05))]
    run(identify._process(queued))  # noqa: SLF001
    r = row("leaf2")
    assert r["id_status"] == "not_bird" and r["common_name"] == "bird"
    assert r["frigate_label"] == "Northern Cardinal"            # provenance kept
    assert not announced("leaf2")


def test_a_real_bird_is_untouched(nb_env):
    learn_leaf()
    queued = queued_unnamed(nb_env, "bird1")
    nb_env.fake.answers = [svc("Northern Cardinal", 0.9, [("Northern Cardinal", 0.9),
                                                          ("Pyrrhuloxia", 0.05)],
                               embedding=emb(5))]
    run(identify._process(queued))  # noqa: SLF001
    assert row("bird1")["common_name"] == "Northern Cardinal" and announced("bird1")


def test_other_bird_like_a_negative_is_dropped(nb_env):
    learn_leaf()
    queued = queued_unnamed(nb_env, "two1")
    answer = svc("Northern Cardinal", 0.9, [("Northern Cardinal", 0.9), ("Pyrrhuloxia", 0.05)],
                 embedding=emb(6))
    answer["subjects"] = [
        {"idx": 0, "primary": True, "common_name": "Northern Cardinal", "score": 0.9,
         "embedding": emb(6), "n_frames": 3},
        {"idx": 1, "primary": False, "common_name": "Carolina Wren", "score": 0.9, "margin": 0.8,
         "candidates": [{"common_name": "Carolina Wren", "score": 0.9}],
         "embedding": emb(8, LEAF, 0.05), "n_frames": 3, "co_occurring": 3},
    ]
    nb_env.fake.answers = [answer]
    run(identify._process(queued))  # noqa: SLF001
    assert db.secondary_subjects_for(row("two1")["id"]) == []


def test_examples_list_and_forget(nb_env):
    ex = learn_leaf()
    assert [e["id"] for e in db.not_bird_list()] == [ex]
    assert db.not_bird_remove(ex)["id"] == ex
    notbird.invalidate()
    assert notbird.match(encode(LEAF), MODEL) is None
    assert db.not_bird_remove(ex) is None
