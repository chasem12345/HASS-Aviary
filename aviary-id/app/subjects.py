"""Split one event's crops into individual birds ("subjects").

An event is one Frigate tracked object, but its media is not: the snapshot and every clip
frame show whatever else was in view, and the detector faithfully finds all of it. Fusing
every crop into one answer is right when they are all the same bird and exactly wrong when
they are not — a cardinal and a wren at the bath average to a 50/50 that names neither,
and the single embedding kept for learning is then whichever bird happened to win the
average, filed under whatever the human labels the card. This module decides which crops
are the SAME bird before anything is fused, so each bird is classified on its own crops
and learned from its own embedding.

Three kinds of evidence, strongest first:

1. **Frigate's own crops** (the thumbnail, the snapshot cropped to Frigate's box) are by
   definition the tracked object. They seed the primary subject.
2. **The tracked path** (``path_data``) says where the tracked bird was at each clip
   timestamp. A detector box on the path is the primary; a box clearly off it is some
   other bird and may never join the primary — a hard rule, where the old ranking only
   penalized it.
3. **Embedding similarity** groups everything the first two cannot place (frames outside
   the path's time range, zoomed footage that has no path, the far boxes themselves) into
   individuals. Two boxes in the same frame are two birds and can never share a subject.

Pure numpy, no torch: the whole thing is an n×n similarity over at most a dozen crops,
and it has to be unit-testable without a GPU.
"""

from __future__ import annotations

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


@dataclass
class Subject:
    indices: list[int]
    primary: bool
    # True when the primary was decided by Frigate's evidence (seeds/path); False when
    # it had to fall back to "the biggest cluster", which the caller may want to flag.
    anchored: bool = True
    # Why each crop landed here, for the debug output.
    notes: list[str] = field(default_factory=list)


def _same_origin(meta: list[CropMeta], i: int, cluster: list[int]) -> bool:
    return any(meta[j].origin == meta[i].origin for j in cluster)


def _mean_sim(sims: np.ndarray, i: int, cluster: list[int]) -> float:
    return float(sims[i, cluster].mean()) if cluster else -1.0


def _cluster_sim(sims: np.ndarray, a: list[int], b: list[int]) -> float:
    return float(sims[np.ix_(a, b)].mean()) if a and b else -1.0


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
) -> list[Subject]:
    """Group crops into subjects. The primary is always first; secondaries follow in
    order of evidence (summed rank). Crops that fail the evidence gate are in no subject.

    ``features`` are L2-normalized image embeddings, one row per crop, so the dot product
    is cosine similarity.
    """
    n = len(meta)
    if n == 0:
        return []
    feats = np.asarray(features, dtype=np.float32)
    if feats.ndim != 2 or feats.shape[0] != n:
        raise ValueError("features must be [n_crops, dim]")
    sims = feats @ feats.T

    seeds = [i for i in range(n) if meta[i].pre_cropped]
    anchored = [i for i in range(n) if not meta[i].pre_cropped
                and meta[i].anchor_dist is not None and meta[i].anchor_dist <= radius]
    far = [i for i in range(n) if not meta[i].pre_cropped
           and meta[i].anchor_dist is not None and meta[i].anchor_dist > radius]
    unknown = [i for i in range(n) if not meta[i].pre_cropped and meta[i].anchor_dist is None]
    notes: dict[int, str] = {i: "seed" for i in seeds}
    notes.update({i: "on path" for i in anchored})

    def note(i: int, text: str) -> None:
        notes[i] = f"{notes[i]}; {text}" if i in notes else text

    # Frigate's crop IS the tracked bird; the path is an estimate (the clip's start time
    # is inferred from padding). When the two disagree outright, trust the crop and let
    # the on-path boxes earn their place by similarity like everything else.
    if seeds and anchored and _cluster_sim(sims, seeds, anchored) < sim_split:
        for i in anchored:
            notes[i] = "on path, but unlike the seed — reassessed"
        unknown = anchored + unknown
        anchored = []

    primary: list[int] = seeds + anchored
    clusters: list[list[int]] = []       # secondary clusters, keyed by list index
    order = (sorted(unknown, key=lambda i: -meta[i].det_score)
             + sorted(far, key=lambda i: -meta[i].det_score))
    far_set = set(far)

    for i in order:
        options: list[tuple[int, float]] = []   # (key, mean sim); key -1 = primary
        if primary and i not in far_set and not _same_origin(meta, i, primary):
            options.append((-1, _mean_sim(sims, i, primary)))
        for ci, c in enumerate(clusters):
            if not _same_origin(meta, i, c):
                options.append((ci, _mean_sim(sims, i, c)))
        options = [o for o in options if o[1] >= sim_merge]
        if not options:
            clusters.append([i])
            note(i, "off path" if i in far_set else "new cluster")
            continue
        top = max(o[1] for o in options)
        ties = [o for o in options if top - o[1] <= _TIE]
        lookup = {ci: c for ci, c in enumerate(clusters)}
        lookup[-1] = primary
        key = ties[0][0] if len(ties) == 1 else _spatial_pick(meta, i, ties, lookup)
        if key == -1:
            primary.append(i)
            note(i, f"joined primary (sim {top:.2f})")
        else:
            clusters[key].append(i)
            note(i, f"joined cluster {key} (sim {top:.2f})")

    # Merge pass: clusters that look alike and never share a frame are one bird seen in
    # different frames. A cluster that looks like the primary IS the primary — the path
    # estimate was off, or it is a second bird of the same species, which is harmless
    # (same label, no poison). Greedy, best pair first, until nothing merges.
    def try_merge() -> bool:
        groups = [primary] + clusters if primary else list(clusters)
        best: Optional[tuple[float, int, int]] = None
        for a in range(len(groups)):
            for b in range(a + 1, len(groups)):
                if any(meta[x].origin == meta[y].origin for x in groups[a] for y in groups[b]):
                    continue
                s = _cluster_sim(sims, groups[a], groups[b])
                if s >= sim_merge and (best is None or s > best[0]):
                    best = (s, a, b)
        if best is None:
            return False
        _, a, b = best
        for i in groups[b]:
            notes[i] += f"; merged (sim {best[0]:.2f})"
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
        # No Frigate evidence at all (a boxless snapshot plus zoomed footage): the bird
        # with the most and best crops is the one this event is most likely about.
        if not clusters:
            return []
        clusters.sort(key=lambda c: -sum(meta[i].rank for i in c))
        primary = clusters.pop(0)
        for i in primary:
            notes[i] += "; primary by weight"

    def keep(c: list[int]) -> bool:
        if len(c) >= min_secondary_crops:
            return True
        return len(c) == 1 and meta[c[0]].det_score >= single_crop_det

    secondaries = [c for c in clusters if keep(c)]
    secondaries.sort(key=lambda c: -sum(meta[i].rank for i in c))
    secondaries = secondaries[: max(0, max_subjects - 1)]

    out = [Subject(indices=sorted(primary), primary=True, anchored=anchored_primary,
                   notes=[notes.get(i, "") for i in sorted(primary)])]
    for c in secondaries:
        out.append(Subject(indices=sorted(c), primary=False, anchored=True,
                           notes=[notes.get(i, "") for i in sorted(c)]))
    return out
