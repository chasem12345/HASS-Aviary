"""The learning probe: what it learns from, per-visit dedup/cap, provenance and the audit.

Synthetic 64-dim embeddings: each species is a unit "base" vector, each example the base
nudged by a little noise (cosine ~0.93 to it, ~0 to another species' base).
"""

from __future__ import annotations

from app import db, probe
from conftest import MODEL, T0, emb, encode, frigate_row, unit

A, B = unit(1), unit(2)


def add(ref: str, species: str, embedding: str, *, status: str = "manual",
        visit_id: int | None = None, start: float = T0) -> int:
    """A confirmed, embedded detection of ``species`` — labelled by hand or by the service."""
    db.upsert_detection(frigate_row(ref, start))
    det_id = db.detection_by_ref("frigate", ref)["id"]
    db.set_identification("frigate", ref, "ok", 0.9, 0.5, f"{MODEL}/common@x", embedding,
                          True, None, embedding_model=MODEL, embedding_target=species)
    if status == "manual":
        db.set_species_manually(det_id, species, None)
    else:
        with db._connect() as conn:  # noqa: SLF001 — an automatic answer, as _process stores it
            conn.execute("UPDATE detections SET common_name = ?, id_status = 'ok' WHERE id = ?",
                         (species, det_id))
    if visit_id is not None:
        with db._connect() as conn:  # noqa: SLF001
            conn.execute("UPDATE detections SET visit_id = ? WHERE id = ?", (visit_id, det_id))
    db.confirm_species(species)
    return det_id


def test_manual_only_unless_configured(env):
    add("m1", "Northern Cardinal", emb(11, A, 0.05))
    add("a1", "Northern Cardinal", emb(12, A, 0.05), status="ok")
    s = probe.rebuild(MODEL)
    assert (s["examples"], s["auto_ignored"], s["learn_from_auto"]) == (1, 1, False)
    probe.configure(True)
    try:
        s = probe.rebuild(MODEL)
        assert (s["examples"], s["auto_ignored"]) == (2, 0)
    finally:
        probe.configure(False)


def test_excluded_example_leaves_pool_but_stays_listed(env):
    det = add("m1", "Northern Cardinal", emb(11, A, 0.05))
    add("m2", "Northern Cardinal", emb(12, A, 0.05), start=T0 + 100)
    db.set_learning_excluded(det, 0, True)
    assert probe.rebuild(MODEL)["examples"] == 1
    rows = {r["detection_id"]: r for r in probe.examples("Northern Cardinal")}
    assert rows[det]["excluded"] and not rows[det]["used"]
    assert rows[det]["dropped_reason"] == "excluded"
    summary = probe.species_summary()[0]
    assert (summary["labelled"], summary["used"], summary["excluded"]) == (2, 1, 1)
    db.set_learning_excluded(det, 0, False)
    assert probe.rebuild(MODEL)["examples"] == 2


def test_visit_dedup_and_cap(env):
    # Six near-identical frames from one visit are one look at the bird.
    for i in range(6):
        add(f"d{i}", "Northern Cardinal", emb(20 + i, A, 0.01), visit_id=7, start=T0 + i)
    s = probe.rebuild(MODEL)
    assert (s["examples"], s["deduped"]) == (1, 5)
    # Six genuinely different frames from one visit still cannot fill a top-3 pool.
    for i in range(6):
        add(f"e{i}", "Carolina Wren", emb(40 + i), visit_id=8, start=T0 + i)
    s = probe.rebuild(MODEL)
    assert probe.examples_for("Carolina Wren") == 2 and s["capped"] == 4
    # Another visit is independent evidence; no visit at all is left alone.
    add("f1", "Carolina Wren", emb(50), visit_id=9, start=T0 + 500)
    add("f2", "Carolina Wren", emb(51), start=T0 + 600)
    add("f3", "Carolina Wren", emb(52), start=T0 + 700)
    probe.rebuild(MODEL)
    assert probe.examples_for("Carolina Wren") == 5


def test_audit_flags_a_mislabelled_visit(env):
    for i in range(3):
        add(f"a{i}", "Northern Cardinal", emb(60 + i, A, 0.05), visit_id=10 + i, start=T0 + i)
        add(f"b{i}", "Carolina Wren", emb(70 + i, B, 0.05), visit_id=20 + i, start=T0 + i)
    # A wren frame filed under cardinal — and a same-visit clone that must not vouch for it.
    bad1 = add("x1", "Northern Cardinal", emb(80, B, 0.05), visit_id=99, start=T0 + 50)
    bad2 = add("x2", "Northern Cardinal", emb(81, B, 0.05), visit_id=99, start=T0 + 51)
    probe.rebuild(MODEL)
    out = probe.flag_suspicious()
    flagged = {(e["detection_id"], e["other_species"]) for sp in out["species"] for e in sp["examples"]}
    assert out["flagged"] == 2
    assert flagged == {(bad1, "Carolina Wren"), (bad2, "Carolina Wren")}
    rows = {r["detection_id"]: r for r in probe.examples("Northern Cardinal")}
    assert rows[bad1]["suspicious"]["species"] == "Carolina Wren"
    assert all(rows[d]["suspicious"] is None for d in rows if d not in (bad1, bad2))
    assert probe.species_summary()[0]["suspicious"] == 2


def test_provenance_survives_rebuild(env):
    det = add("m1", "Northern Cardinal", emb(11, A, 0.05))
    probe.rebuild(MODEL)
    ex = probe.examples("Northern Cardinal")[0]
    assert (ex["detection_id"], ex["subject_idx"], ex["source_ref"]) == (det, 0, "m1")
    assert ex["label_source"] == "manual" and ex["target"] == "Northern Cardinal"
    assert not ex["untargeted"]


def test_blend_still_promotes_a_learned_species(env):
    for i in range(5):
        add(f"c{i}", "Northern Cardinal", emb(90 + i, A, 0.05), visit_id=30 + i, start=T0 + i)
    probe.rebuild(MODEL)
    zero = [{"name": "House Finch", "sci": None, "code": None, "score": 0.5},
            {"name": "Northern Cardinal", "sci": None, "code": None, "score": 0.3}]
    out = probe.blend(encode(A + 0.05 * unit(123)), zero, MODEL)
    assert out and out["name"] == "Northern Cardinal" and out["probe_examples"] == 5
    assert probe.evaluate(MODEL)["pool"] == "manual-only"


def test_forget_learning_wipes_examples_but_keeps_names_and_exclusions(env):
    det = add("m1", "Northern Cardinal", emb(11, A, 0.05))
    add("m2", "Carolina Wren", emb(12, B, 0.05), start=T0 + 10)
    db.set_learning_excluded(det, 0, True)
    db.put_reference_embedding("Northern Cardinal", 0, MODEL, emb(99, A, 0.05))
    probe.rebuild(MODEL)
    assert probe.examples_for("Carolina Wren") == 1
    counts = db.forget_learning()
    assert counts == {"detections": 2, "subjects": 0}
    s = probe.rebuild(MODEL)
    assert s["examples"] == 0 and s["labelled"] == 0
    assert probe.ready()                                   # reference photos survive
    row = db.detection_by_id(det)
    assert row["common_name"] == "Northern Cardinal" and row["id_status"] == "manual"
    assert [r["id"] for r in db.manual_rows_missing_embeddings()] == [
        db.detection_by_ref("frigate", "m2")["id"], det]
    # The exclusion is a decision about that detection and still applies if it comes back.
    db.put_detection_embedding(det, MODEL, emb(11, A, 0.05), "Northern Cardinal")
    assert probe.rebuild(MODEL)["excluded"] == 1
