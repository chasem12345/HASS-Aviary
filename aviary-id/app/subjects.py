"""Split one event's crops into individual birds ("subjects").

An event is one Frigate tracked object, but its media is not: the snapshot and every clip
frame show whatever else was in view, and the detector faithfully finds all of it. Fusing
every crop into one answer is right when they are all the same bird and exactly wrong when
they are not — a cardinal and a wren at the bath average to a 50/50 that names neither,
and the single embedding kept for learning is then whichever bird happened to win the
average, filed under whatever the human labels the card. This module decides which crops
are the SAME bird before anything is fused, so each bird is classified on its own crops
and learned from its own embedding.

Evidence, strongest first:

1. **Frigate's own crops** (the thumbnail, the snapshot cropped to Frigate's box) are by
   definition the tracked object. They seed the primary subject.
2. **The tracked path** (``path_data``) says where the tracked bird was at each clip
   timestamp. A detector box on the path is the primary; a box clearly off it is some
   other bird and may never join the primary — a hard rule, where the old ranking only
   penalized it.
3. **Zoomed footage** (a PTZ camera's recordings, no path) is aimed at the tracked bird's
   zone, so a zoomed frame in which the detector found exactly ONE bird shows the tracked
   bird — unless that crop and Frigate's crop confidently name different species. Frames
   with two or more boxes are assigned by similarity, one box per bird.
4. **Embedding similarity** groups everything the first three cannot place. Two boxes in
   the same frame are two birds and can never share a subject.

What the classifier thinks each crop is takes part only when it is sure: a crop's top-1
is a *vote* at or above ``vote_min`` and no opinion below it. A blurred, infrared or
two-bird crop of Frigate's that scores 15 % on some sparrow must not veto ten clear
zoomed frames of a cardinal, which is exactly what an unconditional vote did.

Similarity across cameras (a wide-camera seed against zoomed frames) is measured
separately: the same bird lands anywhere from 0.70 to 0.99 and two species from 0.64 to
0.89, so cross-camera cosine alone never links two crops; it needs agreeing confident
votes and ``sim_cross`` on top. Within one camera the bars are as before.

A second bird must be *evidenced*: seen beside the tracked bird in a frame (two boxes),
found off the tracked path, or — when it never shared a frame — several cohesive crops
confidently voting for a different species. A single crop that never shared a frame is
appearance drift of the same bird, not a bird, and is reported as unassigned.

Pure numpy, no torch: the whole thing is an n×n similarity over at most a dozen crops,
and it has to be unit-testable without a GPU.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

# Two clusters whose mean similarities to a crop are this close are a tie, and geometry
# (where the crop sits relative to each cluster's nearest-in-time crop) breaks it.
_TIE = 0.03


@dataclass
class CropMeta:
    """Everything the partition needs to know about one crop, minus the pixels."""
    origin: str                      # "thumbnail" | "snapshot+box" | "clip@1.75s" | ...
    det_score: float
    rank: float
    pre_cropped: bool                # Frigate's own crop of the tracked object
    center: Optional[tuple[float, float]] = None   # box bottom-center, normalized
    anchor_dist: Optional[float] = None            # distance to the tracked path, if known
    t: Optional[float] = None                      # clip offset in seconds, if a clip frame
    # Vocabulary index of this crop's own top-1 species, when the caller has classified
    # it, and that top-1's probability. The crop VOTES for its species only at or above
    # ``vote_min``; below it (or with top1 None) it has no opinion and similarity alone
    # decides. Defaults to a confident vote so a caller that sets top1 alone is heard.
    top1: Optional[int] = None
    top1_score: float = 1.0
    # From a zoomed PTZ recording: a different camera from Frigate's crops (cross-camera
    # cosine is not comparable to same-camera cosine) and no path anchors.
    zoomed: bool = False
    # Detector boxes in this crop's frame after de-duplication. 1 = the bird was alone in
    # frame; 2+ = another bird shared it (proof of a second bird).
    frame_boxes: int = 1


@dataclass
class Subject:
    indices: list[int]
    primary: bool
    # True when the primary was decided by Frigate's evidence (seeds/path/zoom); False
    # when it had to fall back to "the biggest cluster", which the caller may want to flag.
    anchored: bool = True
    # Why each crop landed here, for the debug output.
    notes: list[str] = field(default_factory=list)
    # Minimum pairwise cosine inside the subject; None for a single crop. Low cohesion on
    # a primary is the "two birds were fused into one answer" signal.
    cohesion: Optional[float] = None
    # False for a cluster that failed the evidence gate or the SUBJECT_MAX cap. Reported
    # only with ``include_dropped``; never classified as a bird of its own.
    kept: bool = True
    # Frames this subject shares with the primary — the two birds were in view together.
    # 0 for the primary itself. Zero on a secondary means it was never seen beside the
    # tracked bird and was kept on other evidence (off-path, or a confident other species).
    co_occurring: int = 0
    # Its clip crops that were the only box in their frame.
    solo_frames: int = 0


def _cohesion(sims: np.ndarray, cluster: list[int]) -> Optional[float]:
    if len(cluster) < 2:
        return None
    block = sims[np.ix_(cluster, cluster)]
    return float(block[np.triu_indices(len(cluster), k=1)].min())


def _spatial_pick(meta: list[CropMeta], i: int, options: list[tuple[int, float]],
                  clusters: dict[int, list[int]]) -> int:
    """Tie-break between clusters: the one whose nearest-in-time clip crop is closest in
    the frame. Falls back to the higher similarity when geometry is unavailable."""
    me = meta[i]
    if me.center is None or me.t is None:
        return max(options, key=lambda o: o[1])[0]
    best_key, best_d = None, None
    for key, _ in options:
        near = [j for j in clusters[key] if meta[j].center is not None and meta[j].t is not None]
        if not near:
            continue
        j = min(near, key=lambda j: abs(meta[j].t - me.t))
        d = ((meta[j].center[0] - me.center[0]) ** 2 + (meta[j].center[1] - me.center[1]) ** 2) ** 0.5
        if best_d is None or d < best_d:
            best_key, best_d = key, d
    return best_key if best_key is not None else max(options, key=lambda o: o[1])[0]


def partition(
    meta: list[CropMeta],
    features: np.ndarray,
    *,
    radius: float = 0.15,
    sim_merge: float = 0.75,
    sim_split: float = 0.55,
    min_secondary_crops: int = 2,
    single_crop_det: float = 0.5,
    max_subjects: int = 3,
    sim_primary: Optional[float] = None,
    sim_secondary: Optional[float] = None,
    vote_min: float = 0.5,
    sim_cross: Optional[float] = 0.90,
    include_dropped: bool = False,
) -> list[Subject]:
    """Group crops into subjects. The primary is always first; secondaries follow in
    order of evidence (summed rank). Crops that fail the evidence gate are in no subject
    unless ``include_dropped`` is set, in which case they trail the list as ``kept=False``.

    ``features`` are L2-normalized image embeddings, one row per crop, so the dot product
    is cosine similarity.

    Bars, same camera: a crop (or cluster) that AGREES with a cluster about the species —
    its confident vote against the cluster's modal confident vote — or has no opinion,
    joins at ``sim_merge``; one that DISAGREES must clear ``sim_primary`` (the primary) or
    ``sim_secondary`` (another bird). Both default to ``sim_merge`` and are never lower.
    Why agreement rather than similarity alone: on a feeder camera two different birds
    seconds apart share the background, the light and the perch, and their cosines land
    0.85–0.91 — above the 0.75 that keeps one bird's frames together.

    Bars, across cameras (a wide-camera seed against zoomed frames): only agreeing
    confident votes at ``sim_cross`` join; ``None`` disables cross joins. Measured
    same-bird cross-camera cosine overlaps different-species cosine almost entirely.

    Votes need ``vote_min``: a top-1 below it is no opinion. Similarity to a cluster is
    averaged over the cluster's members from the SAME camera when it has any.
    """
    n = len(meta)
    if n == 0:
        return []
    feats = np.asarray(features, dtype=np.float32)
    if feats.ndim != 2 or feats.shape[0] != n:
        raise ValueError("features must be [n_crops, dim]")
    sims = feats @ feats.T
    primary_bar = sim_merge if sim_primary is None else max(sim_merge, sim_primary)
    secondary_bar = sim_merge if sim_secondary is None else max(sim_merge, sim_secondary)

    notes: dict[int, str] = {}

    def note(i: int, text: str) -> None:
        notes[i] = f"{notes[i]}; {text}" if i in notes else text

    def vote(i: int) -> Optional[int]:
        m = meta[i]
        return m.top1 if (m.top1 is not None and m.top1_score >= vote_min) else None

    def modal(cluster: list[int]) -> Optional[int]:
        votes = [vote(j) for j in cluster if vote(j) is not None]
        if not votes:
            return None
        return max(sorted(set(votes)), key=votes.count)

    def agrees(a: Optional[int], b: Optional[int]) -> bool:
        return a is None or b is None or a == b

    def domain(i: int) -> str:
        return "zoom" if meta[i].zoomed else "wide"

    def sim_to(i: int, cluster: list[int]) -> tuple[float, bool]:
        """Mean cosine of crop ``i`` to a cluster, over same-camera members when any;
        the flag says the comparison had to cross cameras."""
        same = [j for j in cluster if domain(j) == domain(i)]
        if same:
            return float(sims[i, same].mean()), False
        return float(sims[i, cluster].mean()), True

    def cluster_sim(a: list[int], b: list[int]) -> tuple[float, bool]:
        pairs = [(x, y) for x in a for y in b if domain(x) == domain(y)]
        if pairs:
            return float(np.mean([sims[x, y] for x, y in pairs])), False
        return float(sims[np.ix_(a, b)].mean()), True

    def clears(s: float, is_primary: bool, va: Optional[int], vb: Optional[int],
               cross: bool) -> bool:
        if cross:
            if sim_cross is None or va is None or vb is None or va != vb:
                return False
            return s >= sim_cross
        if agrees(va, vb):
            return s >= sim_merge
        return s >= (primary_bar if is_primary else secondary_bar)

    def shares_frame(i: int, cluster: list[int]) -> bool:
        return any(meta[j].origin == meta[i].origin for j in cluster)

    def join_note(i: int, group: str, s: float, cross: bool, is_primary: bool,
                  other_vote: Optional[int]) -> None:
        strict = primary_bar if is_primary else secondary_bar
        how = ""
        if cross:
            how = ", cross-camera, same species"
        elif s < strict:
            v = vote(i)
            how = (", same species" if v is not None and other_vote is not None
                   else ", no species opinion")
        note(i, f"joined {group} (sim {s:.2f}{how})")

    seeds = [i for i in range(n) if meta[i].pre_cropped]
    on_path = [i for i in range(n) if not meta[i].pre_cropped
               and meta[i].anchor_dist is not None and meta[i].anchor_dist <= radius]
    far = [i for i in range(n) if not meta[i].pre_cropped
           and meta[i].anchor_dist is not None and meta[i].anchor_dist > radius]
    rest = [i for i in range(n) if not meta[i].pre_cropped and meta[i].anchor_dist is None]
    for i in seeds:
        notes[i] = "seed"
    for i in on_path:
        notes[i] = "on path"

    # Frigate's crop IS the tracked bird; the path is an estimate (the clip's start time
    # is inferred from padding), and at a bath two birds sit inside the anchor radius of
    # the same path point. So every on-path box is checked against the seed on its own:
    # one that does not look like Frigate's crop is not the tracked bird and must earn a
    # place by similarity like everything else. Wide camera only by construction — a
    # zoomed frame has no path.
    if seeds and on_path:
        still = []
        for i in on_path:
            if float(sims[i, seeds].mean()) < sim_split:
                notes[i] = "on path, but unlike the seed — reassessed"
                rest.append(i)
            else:
                still.append(i)
        on_path = still
    primary: list[int] = seeds + on_path

    # Zoomed footage: the PTZ is aimed at the tracked bird's zone, so a frame with exactly
    # one bird in it shows the tracked bird. The exception is a confident disagreement —
    # Frigate's crop says wren, this says cardinal — which means the PTZ was on another
    # bird; such a crop earns its place by similarity like any other. A solo crop that
    # disagrees with the OTHER solo frames is treated the same way (two birds taking
    # turns alone in the zoomed view).
    solo = [i for i in rest if meta[i].zoomed and meta[i].frame_boxes == 1]
    if primary and solo:
        seed_vote = modal(seeds) if seeds else modal(primary)
        block_vote = modal(solo)
        anchored_now: set[int] = set()
        for i in solo:
            v = vote(i)
            if v is not None and seed_vote is not None and v != seed_vote:
                note(i, "zoomed, alone in frame, but names another species than Frigate's crop — reassessed")
                continue
            if v is not None and block_vote is not None and v != block_vote:
                note(i, "zoomed, alone in frame, but names another species than the other solo frames — reassessed")
                continue
            primary.append(i)
            anchored_now.add(i)
            note(i, "zoomed, alone in frame → tracked bird")
        rest = [i for i in rest if i not in anchored_now]

    # Frame-wise assignment. Frames in rank order; within a frame every box is offered to
    # every group it clears the bar for, then boxes are placed best-similarity first with
    # one box per group per frame (two boxes in one frame are two birds). Ties within
    # _TIE go to the higher-ranked box, then to geometry. Far (off-path) boxes are never
    # offered the primary.
    clusters: list[list[int]] = []
    far_set = set(far)
    frames: dict[str, list[int]] = defaultdict(list)
    for i in rest + far:
        frames[meta[i].origin].append(i)
    for origin in sorted(frames, key=lambda o: -max(meta[i].rank for i in frames[o])):
        boxes = sorted(frames[origin], key=lambda i: (-meta[i].rank, i))
        taken: set[int] = set()
        if primary and shares_frame(boxes[0], primary):
            taken.add(-1)
        for ci, c in enumerate(clusters):
            if shares_frame(boxes[0], c):
                taken.add(ci)
        offers: dict[tuple[int, int], tuple[float, bool]] = {}
        for i in boxes:
            if primary and -1 not in taken and i not in far_set:
                s, cross = sim_to(i, primary)
                if clears(s, True, vote(i), modal(primary), cross):
                    offers[(i, -1)] = (s, cross)
            for ci, c in enumerate(clusters):
                if ci in taken:
                    continue
                s, cross = sim_to(i, c)
                if clears(s, False, vote(i), modal(c), cross):
                    offers[(i, ci)] = (s, cross)
        remaining = list(boxes)
        while remaining:
            options = [(v[0], i, g) for (i, g), v in offers.items()
                       if i in remaining and g not in taken]
            if not options:
                break
            top = max(o[0] for o in options)
            close = [o for o in options if top - o[0] <= _TIE]
            s, i, g = max(close, key=lambda o: (meta[o[1]].rank, -o[1], o[0]))
            ties = [(gg, ss) for (ss, ii, gg) in close if ii == i]
            if len(ties) > 1:
                lookup: dict[int, list[int]] = {ci: c for ci, c in enumerate(clusters)}
                lookup[-1] = primary
                g = _spatial_pick(meta, i, ties, lookup)
                s = dict(ties)[g]
            target = primary if g == -1 else clusters[g]
            other_vote = modal(target)
            target.append(i)
            join_note(i, "primary" if g == -1 else f"cluster {g}", s, offers[(i, g)][1],
                      g == -1, other_vote)
            taken.add(g)
            remaining.remove(i)
        for i in remaining:
            clusters.append([i])
            note(i, "off path" if i in far_set else "new cluster")

    # Merge pass: clusters that look alike and never share a frame are one bird seen in
    # different frames. A cluster that looks like the primary IS the primary — the path
    # estimate was off, or it is a second bird of the same species, which is harmless
    # (same label, no poison). Same bars as above. Greedy, best pair first.
    def try_merge() -> bool:
        groups = [primary] + clusters if primary else list(clusters)
        best: Optional[tuple[float, int, int]] = None
        for a in range(len(groups)):
            for b in range(a + 1, len(groups)):
                if any(meta[x].origin == meta[y].origin for x in groups[a] for y in groups[b]):
                    continue
                s, cross = cluster_sim(groups[a], groups[b])
                if clears(s, bool(primary) and a == 0, modal(groups[a]), modal(groups[b]), cross) \
                        and (best is None or s > best[0]):
                    best = (s, a, b)
        if best is None:
            return False
        s, a, b = best
        for i in groups[b]:
            note(i, f"merged (sim {s:.2f})")
        groups[a].extend(groups[b])
        del groups[b]
        if primary:
            clusters[:] = groups[1:]
        else:
            clusters[:] = groups
        return True

    while try_merge():
        pass

    anchored_primary = bool(primary)
    if not primary:
        # No Frigate evidence at all (a boxless snapshot plus footage without a path): the
        # bird with the most and best crops is the one this event is most likely about.
        if not clusters:
            return []
        clusters.sort(key=lambda c: -sum(meta[i].rank for i in c))
        primary = clusters.pop(0)
        anchored_primary = False
        for i in primary:
            note(i, "primary by weight")
    else:
        # Zoomed footage that never anchored (every zoomed frame held two birds, or every
        # solo frame disagreed with Frigate's crop): adopt the zoomed cluster that names
        # what Frigate's crop names; with no opinion on either side, the heaviest one —
        # flagged unanchored. A confident seed that matches nothing leaves the zoomed
        # birds as others: the PTZ was looking at someone else.
        zoom_clusters = [ci for ci, c in enumerate(clusters) if all(meta[i].zoomed for i in c)]
        if zoom_clusters and not any(meta[i].zoomed for i in primary):
            primary_vote = modal(primary)

            def weight(ci: int) -> float:
                return sum(meta[i].rank for i in clusters[ci])

            matching = [ci for ci in zoom_clusters
                        if primary_vote is not None and modal(clusters[ci]) == primary_vote]
            pick: Optional[int] = None
            if matching:
                pick = max(matching, key=weight)
                why = "zoomed cluster adopted: names what Frigate's crop names"
            else:
                heaviest = max(zoom_clusters, key=weight)
                if primary_vote is None or modal(clusters[heaviest]) is None:
                    pick = heaviest
                    why = "zoomed cluster adopted by weight (no confident species on either side)"
                    anchored_primary = False
            if pick is not None:
                for i in clusters[pick]:
                    note(i, why)
                primary.extend(clusters.pop(pick))

    # Evidence gate. A second bird is one that was SEEN to be a second bird: beside the
    # tracked bird in a frame, or off the tracked path — and then it needs a few crops, or
    # one the detector was sure of that also confidently names a species. A cluster that
    # never shared a frame is kept only as several cohesive crops confidently naming a
    # species other than the primary's; anything less is the same bird's appearance
    # drift, reported as unassigned rather than as a bird worth naming.
    primary_origins = {meta[j].origin for j in primary}
    primary_vote = modal(primary)
    secondaries: list[list[int]] = []
    dropped: list[list[int]] = []
    for c in clusters:
        det = max(meta[i].det_score for i in c)
        c_vote = modal(c)
        cooc = bool({meta[i].origin for i in c} & primary_origins)
        evidenced = cooc or any(meta[i].frame_boxes >= 2 for i in c) or any(i in far_set for i in c)
        if evidenced:
            if len(c) >= min_secondary_crops:
                secondaries.append(c)
            elif len(c) == 1 and det >= single_crop_det and vote(c[0]) is not None:
                secondaries.append(c)
            elif len(c) == 1 and det >= single_crop_det:
                for i in c:
                    note(i, f"dropped: {len(c)} crop(s) < {min_secondary_crops}, "
                            "no confident species")
                dropped.append(c)
            else:
                for i in c:
                    note(i, f"dropped: {len(c)} crop(s) < {min_secondary_crops}, "
                            f"det {det:.2f} < {single_crop_det:.2f}")
                dropped.append(c)
            continue
        coh = _cohesion(sims, c)
        if (len(c) >= min_secondary_crops and (coh is None or coh >= sim_merge)
                and c_vote is not None and primary_vote is not None and c_vote != primary_vote):
            secondaries.append(c)
        else:
            for i in c:
                note(i, "dropped: never beside the tracked bird"
                        + ("" if len(c) >= min_secondary_crops else
                           f", {len(c)} crop(s) < {min_secondary_crops}")
                        + ("" if c_vote is not None and primary_vote is not None
                           and c_vote != primary_vote else ", no confident other species"))
            dropped.append(c)
    secondaries.sort(key=lambda c: -sum(meta[i].rank for i in c))
    for c in secondaries[max(0, max_subjects - 1):]:
        for i in c:
            note(i, "dropped: over SUBJECT_MAX")
        dropped.append(c)
    secondaries = secondaries[: max(0, max_subjects - 1)]

    def build(c: list[int], *, is_primary: bool, anchored: bool, kept: bool) -> Subject:
        idx = sorted(c)
        shared = 0 if is_primary else len({meta[i].origin for i in idx} & primary_origins)
        solo_n = sum(1 for i in idx if not meta[i].pre_cropped and meta[i].frame_boxes == 1
                     and meta[i].t is not None)
        return Subject(indices=idx, primary=is_primary, anchored=anchored,
                       notes=[notes.get(i, "") for i in idx],
                       cohesion=_cohesion(sims, idx), kept=kept,
                       co_occurring=shared, solo_frames=solo_n)

    out = [build(primary, is_primary=True, anchored=anchored_primary, kept=True)]
    out += [build(c, is_primary=False, anchored=True, kept=True) for c in secondaries]
    if include_dropped:
        out += [build(c, is_primary=False, anchored=True, kept=False) for c in dropped]
    return out
