"""Subject partition: which crops are the same bird. Pure numpy, no GPU.

Synthetic embeddings: each "bird" is a unit centroid, each crop of it the centroid plus
a little noise, renormalized. With DIM=32 and per-dimension noise 0.04 the noise vector
has norm ~0.23, so same-bird cosines land ~0.95 and cross-bird ~0.0 — a caricature of
BioCLIP (same bird ~0.85, different species ~0.4) that exercises every rule.

Crops that stand for a different bird declare it with a confident ``top1`` vote, as real
crops do (the classifier votes on every crop; only low-confidence votes are silenced): a
second bird that never shared a frame with the tracked one is evidenced by nothing else.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.subjects import CropMeta, partition

rng = np.random.default_rng(7)
DIM = 32


def centroid(seed: int) -> np.ndarray:
    v = np.random.default_rng(seed).standard_normal(DIM)
    return v / np.linalg.norm(v)


def sample(c: np.ndarray, noise: float = 0.04) -> np.ndarray:
    v = c + noise * rng.standard_normal(DIM)
    return v / np.linalg.norm(v)


A, B, C = centroid(1), centroid(2), centroid(3)


def crop(origin, *, det=0.8, rank=50.0, pre=False, center=None, anchor=None, t=None,
         top1=None, score=1.0, zoomed=False, boxes=1):
    return CropMeta(origin=origin, det_score=det, rank=rank, pre_cropped=pre,
                    center=center, anchor_dist=anchor, t=t, top1=top1, top1_score=score,
                    zoomed=zoomed, frame_boxes=boxes)


def run(metas, vecs, **kw):
    return partition(metas, np.stack(vecs), **kw)


def test_two_birds_split_by_anchor_and_similarity():
    metas = [
        crop("thumbnail", pre=True, rank=90),                  # 0 seed: bird A
        crop("snapshot+box", pre=True, rank=80),               # 1 seed: bird A
        crop("clip@1.00s", anchor=0.05, t=1.0, center=(0.3, 0.6)),   # 2 on path: A
        crop("clip@1.00s", anchor=0.40, t=1.0, center=(0.7, 0.6)),   # 3 off path: B
        crop("clip@2.00s", anchor=0.04, t=2.0, center=(0.31, 0.61)), # 4 on path: A
        crop("clip@2.00s", anchor=0.42, t=2.0, center=(0.71, 0.6)),  # 5 off path: B
    ]
    vecs = [sample(A), sample(A), sample(A), sample(B), sample(A), sample(B)]
    subs = run(metas, vecs)
    assert len(subs) == 2
    assert subs[0].primary and subs[0].anchored and subs[0].indices == [0, 1, 2, 4]
    assert not subs[1].primary and subs[1].indices == [3, 5]
    assert subs[1].co_occurring == 2


def test_far_crop_never_joins_primary_even_if_similar():
    metas = [crop("thumbnail", pre=True), crop("clip@1.00s", anchor=0.5, t=1.0, center=(0.8, 0.8))]
    subs = run(metas, [sample(A), sample(A)])          # same bird by embedding...
    # ...but flagged far from the tracked path: it is barred on its own; then the merge
    # pass re-joins a far CLUSTER that looks like the primary (path estimate was off).
    assert subs[0].indices == [0, 1]


def test_far_cluster_of_other_species_stays_separate():
    metas = [crop("thumbnail", pre=True),
             crop("clip@1.00s", anchor=0.5, t=1.0, center=(0.8, 0.8)),
             crop("clip@2.00s", anchor=0.5, t=2.0, center=(0.8, 0.8))]
    subs = run(metas, [sample(A), sample(B), sample(B)])
    assert [s.indices for s in subs] == [[0], [1, 2]]


def test_cannot_link_within_one_frame():
    # Two boxes from the same frame, embeddings that happen to look alike: two birds.
    metas = [crop("thumbnail", pre=True),
             crop("clip@1.00s", t=1.0, center=(0.2, 0.5)),
             crop("clip@1.00s", t=1.0, center=(0.8, 0.5)),
             crop("clip@2.00s", t=2.0, center=(0.8, 0.5))]
    subs = run(metas, [sample(A), sample(A), sample(A), sample(A)], min_secondary_crops=1)
    assert subs[0].indices == [0, 1]
    # The same-frame twin cannot join the primary; the later frame's crop joins the
    # cluster whose nearest-in-time crop is closest in the frame.
    assert subs[1].indices == [2, 3]


def test_seed_anchor_disagreement_trusts_seed():
    metas = [crop("thumbnail", pre=True, top1=0),
             crop("clip@1.00s", anchor=0.02, t=1.0, center=(0.3, 0.5), top1=1),
             crop("clip@2.00s", anchor=0.02, t=2.0, center=(0.3, 0.5), top1=1)]
    # The path points at bird B's boxes but Frigate's own crop is bird A. B's crops were
    # never beside A in a frame, so what keeps them a reported bird is that they cohere
    # and confidently name another species.
    subs = run(metas, [sample(A), sample(B), sample(B)])
    assert subs[0].indices == [0] and subs[0].anchored
    assert subs[1].indices == [1, 2]
    assert "reassessed" in subs[1].notes[0]


def test_unknown_crops_join_primary_by_similarity():
    metas = [crop("thumbnail", pre=True),
             crop("clip@0.10s", t=0.1, center=(0.3, 0.5)),   # padding frame: no anchor
             crop("clip@9.00s", t=9.0, center=(0.3, 0.5))]
    subs = run(metas, [sample(A), sample(A), sample(A)])
    assert len(subs) == 1 and subs[0].indices == [0, 1, 2]


def test_single_low_score_stray_is_dropped_but_confident_one_kept():
    metas = [crop("thumbnail", pre=True),
             crop("clip@1.00s", anchor=0.6, t=1.0, det=0.35, center=(0.9, 0.9))]
    subs = run(metas, [sample(A), sample(B)])
    assert len(subs) == 1
    # Off the path (evidence of a second bird), a sure detection AND a confident species:
    # a bird worth reporting on one crop.
    metas[1] = crop("clip@1.00s", anchor=0.6, t=1.0, det=0.8, center=(0.9, 0.9), top1=1)
    subs = run(metas, [sample(A), sample(B)])
    assert len(subs) == 2 and subs[1].indices == [1]
    # A sure detection the classifier cannot name is not worth a chip of its own.
    metas[1] = crop("clip@1.00s", anchor=0.6, t=1.0, det=0.8, center=(0.9, 0.9),
                    top1=1, score=0.2)
    subs = run(metas, [sample(A), sample(B)], include_dropped=True)
    assert len(subs) == 2 and not subs[1].kept
    assert "no confident species" in subs[1].notes[0]


def test_upload_single_seed_is_one_subject():
    subs = run([crop("upload.jpg", pre=True)], [sample(A)])
    assert len(subs) == 1 and subs[0].primary and subs[0].indices == [0]


def test_no_seed_no_anchor_falls_back_to_heaviest_cluster():
    metas = [crop("clip@1.00s", t=1.0, rank=10, center=(0.2, 0.5), top1=0, boxes=2),
             crop("clip@1.00s", t=1.0, rank=90, center=(0.8, 0.5), top1=1, boxes=2),
             crop("clip@2.00s", t=2.0, rank=85, center=(0.8, 0.5), top1=1)]
    subs = run(metas, [sample(A), sample(B), sample(B)])
    assert subs[0].indices == [1, 2] and not subs[0].anchored
    # The lone bird-A crop was beside the primary in a frame, the detector was sure and
    # it names a species: a second bird on one crop.
    assert len(subs) == 2 and subs[1].co_occurring == 1
    metas[0] = crop("clip@1.00s", t=1.0, rank=10, center=(0.2, 0.5), top1=0, score=0.3, boxes=2)
    assert len(run(metas, [sample(A), sample(B), sample(B)])) == 1


def test_max_subjects_cap_keeps_heaviest_secondaries():
    metas = [crop("thumbnail", pre=True)]
    vecs = [sample(A)]
    for k, cen in enumerate((B, C, centroid(4))):
        for f in range(2):
            metas.append(crop(f"clip@{f + 1}.{k}0s", anchor=0.5, t=f + 1 + k / 10,
                              rank=100 - 10 * k, center=(0.9, 0.9)))
            vecs.append(sample(cen))
    subs = run(metas, vecs, max_subjects=2)
    assert len(subs) == 2 and subs[1].indices == [1, 2]   # bird B: heaviest cluster


def test_empty_and_shape_errors():
    assert partition([], np.zeros((0, DIM))) == []
    with pytest.raises(ValueError):
        partition([crop("thumbnail", pre=True)], np.zeros((2, DIM)))


# --- exact-cosine constructions for the two-bar rules ---------------------------------

def at_cos(a: np.ndarray, b: np.ndarray, c: float) -> np.ndarray:
    """Unit vector with cosine exactly ``c`` to ``a``, leaning toward ``b``."""
    bp = b - (b @ a) * a
    bp /= np.linalg.norm(bp)
    return c * a + (1 - c ** 2) ** 0.5 * bp


def test_unknown_at_merge_bar_needs_species_agreement_for_primary():
    metas = [crop("thumbnail", pre=True), crop("clip@1.00s", t=1.0, center=(0.3, 0.5))]
    metas[0].top1, metas[1].top1 = 0, 1
    subs = run(metas, [A, at_cos(A, B, 0.78)], sim_primary=0.85, include_dropped=True)
    # Looks alike, disagrees: not mine — and alone, never beside me, not a bird either.
    assert [s.indices for s in subs] == [[0], [1]] and not subs[1].kept
    metas[1].top1 = 0
    subs = run(metas, [A, at_cos(A, B, 0.78)], sim_primary=0.85)
    assert len(subs) == 1 and subs[0].indices == [0, 1]
    assert "same species" in subs[0].notes[1]


def test_unknown_above_primary_bar_joins_regardless_of_top1():
    metas = [crop("thumbnail", pre=True), crop("clip@1.00s", t=1.0, center=(0.3, 0.5))]
    metas[0].top1, metas[1].top1 = 0, 1
    subs = run(metas, [A, at_cos(A, B, 0.90)], sim_primary=0.85)
    assert len(subs) == 1 and subs[0].indices == [0, 1]


def test_defaults_unchanged_without_new_knobs():
    # Pins 0.10.0 behaviour: with no sim_primary, the merge bar alone admits to the primary.
    metas = [crop("thumbnail", pre=True), crop("clip@1.00s", t=1.0, center=(0.3, 0.5))]
    metas[0].top1, metas[1].top1 = 0, 1
    subs = run(metas, [A, at_cos(A, B, 0.78)])
    assert len(subs) == 1 and subs[0].indices == [0, 1]


def test_anchored_crop_unlike_seed_is_demoted_individually():
    metas = [crop("thumbnail", pre=True, top1=0),
             crop("clip@1.00s", anchor=0.05, t=1.0, center=(0.3, 0.5), top1=0),
             crop("clip@2.00s", anchor=0.05, t=2.0, center=(0.3, 0.5), top1=0),
             crop("clip@3.00s", anchor=0.05, t=3.0, center=(0.35, 0.5), top1=1),
             crop("clip@4.00s", anchor=0.05, t=4.0, center=(0.35, 0.5), top1=1)]
    like, unlike = at_cos(A, B, 0.9), at_cos(A, B, 0.3)
    # Cluster mean vs the seed is 0.6 >= sim_split: the old all-or-nothing test would have
    # kept all four on-path boxes. Per crop, the two unlike ones are reassessed.
    subs = run(metas, [A, like, like, unlike, unlike])
    assert subs[0].indices == [0, 1, 2]
    assert subs[1].indices == [3, 4]
    assert all("reassessed" in n for n in subs[1].notes)


def test_secondary_join_needs_agreement_below_its_strict_bar():
    metas = [crop("thumbnail", pre=True),
             crop("clip@1.00s", anchor=0.5, t=1.0, center=(0.8, 0.8)),
             crop("clip@2.00s", anchor=0.5, t=2.0, center=(0.8, 0.8)),
             crop("clip@3.00s", anchor=0.5, t=3.0, center=(0.8, 0.8))]
    metas[0].top1 = 0
    metas[1].top1 = metas[2].top1 = 1
    near_b = at_cos(B, C, 0.85)                 # a crop 0.85 from the other bird's frames
    # Disagreeing top-1 at 0.85: below the strict secondary bar, so it is a third cluster.
    metas[3].top1 = 2
    subs = run(metas, [A, B, B, near_b], sim_secondary=0.90)
    assert [s.indices for s in subs] == [[0], [1, 2], [3]]
    # Agreeing top-1: the ordinary bar applies and it is the same bird.
    metas[3].top1 = 1
    subs = run(metas, [A, B, B, near_b], sim_secondary=0.90)
    assert [s.indices for s in subs] == [[0], [1, 2, 3]]
    assert "same species" in subs[1].notes[2]
    # Without the knob, similarity alone decides (0.10.0 behaviour).
    metas[3].top1 = 2
    subs = run(metas, [A, B, B, near_b])
    assert [s.indices for s in subs] == [[0], [1, 2, 3]]


def test_strict_bars_are_never_below_the_merge_bar():
    metas = [crop("thumbnail", pre=True), crop("clip@1.00s", t=1.0, center=(0.3, 0.5))]
    metas[0].top1, metas[1].top1 = 0, 1
    # sim_primary below sim_merge would make disagreeing crops EASIER to admit; clamped.
    subs = run(metas, [A, at_cos(A, B, 0.70)], sim_primary=0.60, include_dropped=True)
    assert [s.indices for s in subs] == [[0], [1]] and not subs[1].kept


def test_primary_merge_allows_same_species_cluster_at_merge_bar():
    metas = [crop("thumbnail", pre=True),
             crop("clip@1.00s", anchor=0.5, t=1.0, center=(0.8, 0.8)),
             crop("clip@2.00s", anchor=0.5, t=2.0, center=(0.8, 0.8))]
    for m in metas:
        m.top1 = 0
    near = at_cos(A, B, 0.78)
    subs = run(metas, [A, near, near], sim_primary=0.85)
    assert len(subs) == 1 and subs[0].indices == [0, 1, 2]
    metas[1].top1 = metas[2].top1 = 1
    subs = run(metas, [A, near, near], sim_primary=0.85)
    assert [s.indices for s in subs] == [[0], [1, 2]]


def test_dropped_crops_reported_when_requested():
    metas = [crop("thumbnail", pre=True),
             crop("clip@1.00s", anchor=0.6, t=1.0, det=0.35, center=(0.9, 0.9))]
    assert len(run(metas, [A, B])) == 1
    subs = run(metas, [A, B], include_dropped=True)
    assert len(subs) == 2 and not subs[1].kept and not subs[1].primary
    assert subs[1].indices == [1] and subs[1].notes[0].endswith("det 0.35 < 0.50")
    assert "dropped" in subs[1].notes[0]


def test_cohesion_is_min_intra_subject_cosine():
    metas = [crop("thumbnail", pre=True),
             crop("clip@0.10s", t=0.1, center=(0.3, 0.5)),
             crop("clip@9.00s", t=9.0, center=(0.3, 0.5))]
    subs = run(metas, [A, at_cos(A, B, 0.9), at_cos(A, B, 0.8)])
    assert len(subs) == 1 and abs(subs[0].cohesion - 0.8) < 1e-3
    assert run([metas[0]], [A])[0].cohesion is None


def test_over_cap_cluster_reported_as_dropped():
    metas = [crop("thumbnail", pre=True)]
    vecs = [sample(A)]
    for k, cen in enumerate((B, C)):
        for f in range(2):
            metas.append(crop(f"clip@{f + 1}.{k}0s", anchor=0.5, t=f + 1 + k / 10,
                              rank=100 - 10 * k, center=(0.9, 0.9)))
            vecs.append(sample(cen))
    subs = run(metas, vecs, max_subjects=2, include_dropped=True)
    assert [s.kept for s in subs] == [True, True, False]
    assert subs[2].indices == [3, 4] and "over SUBJECT_MAX" in subs[2].notes[0]


# --- 0.13.0: confidence-gated votes, zoomed footage, evidence -------------------------

def test_low_confidence_seed_vote_does_not_veto_the_primary():
    """Frigate's crop scores 15 % on some sparrow; the clear cardinal frames must not be
    barred from the primary by that 'disagreement'."""
    metas = [crop("snapshot+box", pre=True, top1=3, score=0.15),
             crop("clip@1.00s", t=1.0, center=(0.3, 0.5), top1=0, score=0.99),
             crop("clip@2.00s", t=2.0, center=(0.3, 0.5), top1=0, score=0.99)]
    vecs = [A, at_cos(A, B, 0.80), at_cos(A, B, 0.80)]
    subs = run(metas, vecs, sim_primary=0.92)
    assert len(subs) == 1 and subs[0].indices == [0, 1, 2]
    # The same seed with a CONFIDENT vote for another species keeps them out.
    metas[0].top1_score = 0.9
    subs = run(metas, vecs, sim_primary=0.92)
    assert subs[0].indices == [0]


def test_zoomed_solo_frames_anchor_to_the_primary_regardless_of_cross_cosine():
    """A zoomed frame with one bird in it shows the tracked bird: cross-camera cosine to
    the wide seed is uninformative (0.60 here) and is not consulted."""
    metas = [crop("snapshot+box", pre=True, top1=0, score=0.2),
             crop("clip@1.00s", t=1.0, center=(0.5, 0.9), zoomed=True, top1=1, score=0.99),
             crop("clip@2.00s", t=2.0, center=(0.5, 0.9), zoomed=True, top1=1, score=0.99)]
    zoom_a = at_cos(A, B, 0.60)
    subs = run(metas, [A, zoom_a, sample(zoom_a)])
    assert len(subs) == 1 and subs[0].indices == [0, 1, 2] and subs[0].anchored
    assert all("tracked bird" in n for n in subs[0].notes[1:])
    assert subs[0].solo_frames == 2


def test_zoomed_solo_frame_that_confidently_disagrees_is_not_anchored():
    metas = [crop("snapshot+box", pre=True, top1=0, score=0.9),
             crop("clip@1.00s", t=1.0, center=(0.5, 0.9), zoomed=True, top1=1, score=0.9)]
    subs = run(metas, [A, at_cos(A, B, 0.60)], include_dropped=True)
    assert subs[0].indices == [0]
    assert not subs[1].kept and "reassessed" in subs[1].notes[0]


def test_cross_camera_join_needs_agreeing_confident_votes_and_sim_cross():
    """A WIDE box (from the boxless snapshot) against a ZOOMED primary: cross-camera
    cosine counts only with agreeing confident votes, at ``sim_cross``."""
    wide = crop("snapshot", t=None, center=(0.3, 0.6), rank=50, top1=0, score=0.9, boxes=2)
    zoomed = [crop("clip@1.00s", t=1.0, center=(0.5, 0.9), zoomed=True, rank=90, top1=0),
              crop("clip@2.00s", t=2.0, center=(0.5, 0.9), zoomed=True, rank=90, top1=0)]
    metas = [wide, *zoomed]
    subs = run(metas, [at_cos(A, B, 0.95), A, sample(A)], sim_cross=0.90)
    assert len(subs) == 1 and subs[0].indices == [0, 1, 2]
    assert "cross-camera" in subs[0].notes[0]
    subs = run(metas, [at_cos(A, B, 0.85), A, sample(A)], sim_cross=0.90)
    assert subs[0].indices == [1, 2] and subs[1].indices == [0]   # a second bird beside it
    wide.top1_score = 0.2   # no opinion: similarity alone never crosses cameras
    subs = run(metas, [at_cos(A, B, 0.95), A, sample(A)], sim_cross=0.90, include_dropped=True)
    assert subs[0].indices == [1, 2] and not subs[1].kept
    wide.top1_score = 0.9
    subs = run(metas, [at_cos(A, B, 0.95), A, sample(A)], sim_cross=None)
    assert subs[0].indices == [1, 2] and subs[1].indices == [0]


def test_zoomed_cluster_naming_frigates_species_is_adopted_below_sim_cross():
    """Every zoomed frame held two birds (no solo anchoring) and the zoomed cluster is
    too unlike the wide seed for a cross-camera join — but it names what Frigate's crop
    names, so it is the tracked bird, anchored."""
    seed = crop("snapshot+box", pre=True, top1=0, score=0.9)
    zoomed = crop("clip@1.00s", t=1.0, center=(0.5, 0.9), zoomed=True, top1=0, score=0.9, boxes=2)
    subs = run([seed, zoomed], [A, at_cos(A, B, 0.85)], sim_cross=0.90)
    assert subs[0].indices == [0, 1] and subs[0].anchored
    assert "adopted" in subs[0].notes[1]
    # Confident seed, zoomed cluster confidently naming ANOTHER species: the PTZ was on
    # someone else; it stays a second bird.
    zoomed.top1 = 1
    subs = run([seed, zoomed], [A, at_cos(A, B, 0.85)], sim_cross=0.90)
    assert subs[0].indices == [0] and subs[1].indices == [1]


def test_frame_wise_assignment_gives_the_primary_slot_to_the_higher_ranked_box():
    """Two boxes in one frame, both alike to the primary within the tie window: the
    higher-ranked one is the tracked bird; the other is a second bird beside it."""
    metas = [crop("thumbnail", pre=True, top1=0),
             crop("clip@1.00s", t=1.0, rank=10, center=(0.2, 0.5), top1=0, boxes=2),
             crop("clip@1.00s", t=1.0, rank=90, center=(0.8, 0.5), top1=0, boxes=2)]
    subs = run(metas, [A, at_cos(A, B, 0.96), at_cos(A, C, 0.95)], min_secondary_crops=1)
    assert subs[0].indices == [0, 2]
    assert subs[1].indices == [1] and subs[1].co_occurring == 1


def test_adoption_fallback_when_every_zoomed_frame_holds_two_birds():
    """No solo frame anchors anything and no cross-camera vote agrees: the heaviest
    zoomed cluster is adopted as the tracked bird, flagged unanchored; the other bird
    beside it is a reported secondary."""
    seed = crop("snapshot+box", pre=True, top1=0, score=0.2)
    metas = [seed]
    vecs = [A]
    for k in range(3):
        metas.append(crop(f"clip@{k}.00s", t=float(k), rank=90, center=(0.3, 0.9),
                          zoomed=True, top1=0, score=0.2, boxes=2))
        metas.append(crop(f"clip@{k}.00s", t=float(k), rank=40, center=(0.8, 0.9),
                          zoomed=True, top1=1, score=0.9, boxes=2))
        vecs += [sample(at_cos(A, B, 0.6)), sample(B)]
    subs = run(metas, vecs)
    assert subs[0].indices == [0, 1, 3, 5] and not subs[0].anchored
    assert subs[1].indices == [2, 4, 6] and subs[1].co_occurring == 3
    # With a confident seed vote, the zoomed cluster that names the same species is the
    # one adopted — even when it is the lighter one — and the primary stays anchored.
    seed.top1_score = 0.9
    seed.top1 = 1
    subs = run(metas, vecs)
    assert subs[0].indices == [0, 2, 4, 6] and subs[0].anchored


def test_same_species_cluster_never_beside_the_primary_is_drift_not_a_bird():
    metas = [crop("thumbnail", pre=True, top1=0),
             crop("clip@1.00s", t=1.0, center=(0.3, 0.5), top1=0),
             crop("clip@2.00s", t=2.0, center=(0.3, 0.5), top1=0)]
    drift = at_cos(A, B, 0.60)   # too unlike the seed to merge, never in a frame with it
    subs = run(metas, [A, drift, sample(drift)], include_dropped=True)
    assert subs[0].indices == [0]
    assert not subs[1].kept and "never beside the tracked bird" in subs[1].notes[0]


def test_other_species_cluster_is_kept_without_co_occurrence_when_it_coheres():
    metas = [crop("thumbnail", pre=True, top1=0),
             crop("clip@1.00s", t=1.0, center=(0.3, 0.5), top1=1),
             crop("clip@2.00s", t=2.0, center=(0.3, 0.5), top1=1)]
    subs = run(metas, [A, sample(B), sample(B)])
    assert [s.indices for s in subs] == [[0], [1, 2]]
    assert subs[1].co_occurring == 0


def test_co_occurring_counts_frames_shared_with_the_primary():
    metas = [crop("thumbnail", pre=True, top1=0),
             crop("clip@1.00s", t=1.0, center=(0.3, 0.5), top1=0, boxes=2),
             crop("clip@1.00s", t=1.0, center=(0.8, 0.5), top1=1, boxes=2),
             crop("clip@2.00s", t=2.0, center=(0.3, 0.5), top1=0, boxes=2),
             crop("clip@2.00s", t=2.0, center=(0.8, 0.5), top1=1, boxes=2),
             crop("clip@3.00s", t=3.0, center=(0.8, 0.5), top1=1)]
    subs = run(metas, [A, sample(A), sample(B), sample(A), sample(B), sample(B)])
    assert subs[0].indices == [0, 1, 3] and subs[0].co_occurring == 0
    assert subs[1].indices == [2, 4, 5] and subs[1].co_occurring == 2
    assert subs[1].solo_frames == 1
