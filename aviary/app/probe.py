"""Learn each species' appearance from your own confirmed birds.

BioCLIP is used zero-shot: an image embedding is compared against text embeddings of
species names. That is what makes an arbitrary regional species list possible, and it is
also where most of the model's accuracy is left unused. On NABirds the same frozen
embeddings score 74.9% zero-shot and 92.4% under a trained probe — a +17.5 point gap that
costs no new model, no new hardware, and no extra inference.

This closes that gap with a nearest-centroid classifier: one mean embedding per species,
built from detections a human has confirmed. Chosen over logistic regression because it
works from a *single* example, needs no training loop or hyperparameters, and updates
incrementally — the published one-shot numbers show most of the gain arrives early.

Two properties are deliberate and worth preserving through any future change:

* **It blends, never replaces.** A species with no examples scores purely zero-shot, so a
  bird you have never seen stays identifiable. A probe that quietly stopped finding new
  species would be worse than no probe at all.
* **It learns only from confirmed labels.** Training on the model's own unreviewed guesses
  is how a classifier teaches itself its own mistakes.
"""

from __future__ import annotations

import base64
import logging
import math
import threading
from typing import Optional

import numpy as np

from . import db

log = logging.getLogger("aviary.probe")

# A reference photo is the right species in the wrong domain: posed, well lit, filling the
# frame, nothing like a feeder camera at 20 metres. Worth having — it is what makes the
# probe useful on day one — but one real frame from your own camera should outweigh
# several of them.
_REFERENCE_WEIGHT = 0.3

# Examples needed before the probe is trusted as much as it can be. Below this its
# influence scales up linearly, so one lucky example cannot swing a result.
_FULL_TRUST_AT = 5.0

# Ceiling on the probe's share of the blend. Kept below 1.0 so the zero-shot signal always
# retains a vote: the species list is regional and complete, whereas the centroids only
# ever cover birds that have already been confirmed.
_MAX_BLEND = 0.7

# Softmax temperature over cosine similarities. CLIP-space cosines between same-species
# images cluster in a narrow band (~0.5-0.9), so a plain softmax would be nearly uniform;
# this spreads them into something comparable with the zero-shot probabilities.
_TEMPERATURE = 25.0

# The probe must be *close* to a centroid, not merely closer than the others.
#
# Softmax is relative: a bird the probe has never seen is roughly equidistant from every
# centroid, and softmax happily amplifies whichever random one wins into a confident-looking
# 0.7. Left unchecked that lets the probe override a correct zero-shot answer for a species
# it has no examples of — the exact failure this design is supposed to make impossible.
#
# So there is an absolute floor as well. Below _MIN_SIMILARITY the probe abstains entirely
# and the zero-shot answer stands untouched; between the floor and _STRONG_SIMILARITY its
# influence ramps up. In CLIP space, same-species images sit around 0.6-0.9, different
# species around 0.3-0.5, and unrelated images near zero.
_MIN_SIMILARITY = 0.45
_STRONG_SIMILARITY = 0.70

_lock = threading.Lock()
_centroids: dict[str, np.ndarray] = {}
_counts: dict[str, float] = {}          # effective (weighted) example count per species
_raw_counts: dict[str, int] = {}        # confirmed detections only, for display
_model: str = ""


def decode(embedding: str) -> Optional[np.ndarray]:
    """base64 float16 -> float32 unit vector. None if it can't be read."""
    try:
        raw = base64.b64decode(embedding)
        vec = np.frombuffer(raw, dtype=np.float16).astype(np.float32)
    except (ValueError, TypeError):
        return None
    if vec.size == 0:
        return None
    norm = float(np.linalg.norm(vec))
    if not norm or not math.isfinite(norm):
        return None
    return vec / norm


def rebuild(model: str) -> dict:
    """Recompute every centroid from the database. Returns a summary.

    Cheap enough to do outright rather than maintain incrementally: even a few thousand
    detections is a handful of megabytes, and a full rebuild has no staleness to reason
    about. Centroids are held in memory only — they are derived data, and persisting them
    would add a cache-invalidation problem for no gain.
    """
    if not model:
        return {"species": 0, "examples": 0}

    sums: dict[str, np.ndarray] = {}
    weights: dict[str, float] = {}
    raw: dict[str, int] = {}
    skipped = 0

    def add(name: str, encoded: str, weight: float, is_real: bool) -> None:
        nonlocal skipped
        vec = decode(encoded)
        if vec is None:
            skipped += 1
            return
        key = name.strip()
        if key not in sums or sums[key].shape != vec.shape:
            if key in sums:
                # Dimensions differ, which means two different models wrote rows under the
                # same model string. Trust the newer shape rather than crashing on a
                # broadcast error.
                log.warning("Embedding dimension changed for %s; resetting its centroid.", key)
            sums[key] = np.zeros_like(vec)
            weights[key] = 0.0
            raw.setdefault(key, 0)
        sums[key] += vec * weight
        weights[key] += weight
        if is_real:
            raw[key] = raw.get(key, 0) + 1

    for name, enc in db.confirmed_embeddings(model):
        add(name, enc, 1.0, True)
    for name, enc in db.reference_embeddings(model):
        add(name, enc, _REFERENCE_WEIGHT, False)

    built: dict[str, np.ndarray] = {}
    for name, total in sums.items():
        norm = float(np.linalg.norm(total))
        if norm > 0 and math.isfinite(norm):
            built[name] = total / norm

    with _lock:
        global _centroids, _counts, _raw_counts, _model
        _centroids, _counts, _raw_counts, _model = built, weights, raw, model

    summary = {
        "model": model,
        "species": len(built),
        "examples": int(sum(raw.values())),
        "reference_only": sum(1 for n in built if not raw.get(n)),
        "skipped": skipped,
    }
    log.info(
        "Probe rebuilt: %d species from %d confirmed detection(s) "
        "(%d species on reference photos alone).",
        summary["species"], summary["examples"], summary["reference_only"],
    )
    return summary


def ready() -> bool:
    with _lock:
        return bool(_centroids)


def stats() -> dict:
    with _lock:
        return {
            "model": _model,
            "species": len(_centroids),
            "examples": int(sum(_raw_counts.values())),
            "top": sorted(
                ({"species": k, "examples": v} for k, v in _raw_counts.items() if v),
                key=lambda x: x["examples"], reverse=True,
            )[:10],
        }


def examples_for(species: str) -> int:
    with _lock:
        return _raw_counts.get(species.strip(), 0)


def _similarities(vec: np.ndarray) -> dict[str, float]:
    with _lock:
        items = list(_centroids.items())
    return {
        name: float(np.dot(vec, centroid))
        for name, centroid in items
        if centroid.shape == vec.shape
    }


def _weight_for(species: str) -> float:
    """How much of the blend the probe gets, from how much evidence backs this species."""
    with _lock:
        n = _counts.get(species, 0.0)
    return _MAX_BLEND * min(1.0, n / _FULL_TRUST_AT)


def blend(embedding: str, zero_shot: list[dict], model: str,
          exclude: Optional[set] = None) -> Optional[dict]:
    """Combine the zero-shot shortlist with centroid similarity.

    ``zero_shot`` is the service's candidate list — ``[{name, sci, code, score}, ...]``.
    Returns ``{name, sci, code, score, margin, candidates, probe_species, probe_examples}``
    or None when there is nothing to add, in which case the caller keeps the original
    answer untouched.

    Species are scored from the union of both sources, not just the zero-shot shortlist:
    the whole value of the probe is being able to promote a species the text comparison
    ranked sixth, and intersecting first would throw exactly that away.
    """
    with _lock:
        if not _centroids or model != _model:
            return None

    vec = decode(embedding or "")
    if vec is None:
        return None

    sims = _similarities(vec)
    if exclude:
        drop = {e.strip().lower() for e in exclude}
        sims = {k: v for k, v in sims.items() if k.lower() not in drop}
    if not sims:
        return None

    # Abstain unless the image genuinely resembles something we have seen. See the note on
    # _MIN_SIMILARITY: without this, a bird with no centroid gets assigned to whichever
    # species it is accidentally nearest, and can outvote a correct zero-shot answer.
    best_sim = max(sims.values())
    if best_sim < _MIN_SIMILARITY:
        log.debug("Probe abstaining: best similarity %.3f is below %.2f.",
                  best_sim, _MIN_SIMILARITY)
        return None
    closeness = min(1.0, (best_sim - _MIN_SIMILARITY)
                    / max(1e-6, _STRONG_SIMILARITY - _MIN_SIMILARITY))

    # Cosine similarities -> a probability distribution comparable with the zero-shot one.
    names = list(sims)
    scaled = np.array([sims[n] for n in names], dtype=np.float32) * _TEMPERATURE
    scaled -= scaled.max()
    probs = np.exp(scaled)
    probs /= probs.sum()
    probe = dict(zip(names, probs.tolist()))

    zero = {c["name"]: float(c.get("score") or 0.0) for c in zero_shot if c.get("name")}
    meta = {c["name"]: c for c in zero_shot if c.get("name")}

    # The blend weight comes from the evidence behind the probe's OWN best guess. Using the
    # zero-shot winner's count instead would mean a species with no examples could never be
    # corrected, which is the case this exists for.
    probe_best = max(probe, key=probe.get)
    # Two independent gates on how much say the probe gets: how many examples back this
    # species, and how close the match actually is. Both must be good for it to lead.
    weight = _weight_for(probe_best) * closeness
    if weight <= 0:
        return None

    merged: dict[str, float] = {}
    for name in set(zero) | set(probe):
        merged[name] = (1.0 - weight) * zero.get(name, 0.0) + weight * probe.get(name, 0.0)

    total = sum(merged.values())
    if total <= 0:
        return None
    for name in merged:
        merged[name] /= total

    ranked = sorted(merged.items(), key=lambda kv: kv[1], reverse=True)
    best, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    info = meta.get(best, {})

    return {
        "name": best,
        "sci": info.get("sci"),
        "code": info.get("code"),
        "score": best_score,
        "margin": best_score - second_score,
        "candidates": [
            {"name": n, "sci": meta.get(n, {}).get("sci"),
             "code": meta.get(n, {}).get("code"), "score": round(v, 4)}
            for n, v in ranked[:5]
        ],
        # Recorded so the UI can say what the answer was actually matched against, and so
        # a surprising result can be traced back to how much evidence backed it.
        "probe_weight": round(weight, 3),
        "probe_similarity": round(best_sim, 3),
        "probe_examples": examples_for(best),
    }


def evaluate(model: str) -> dict:
    """Leave-one-out accuracy over confirmed detections: zero-shot's peer, measured.

    Each confirmed embedding is scored against centroids rebuilt *without* it, so a species
    with a single example cannot trivially match itself. This deliberately touches no clips,
    crops or ffmpeg — it measures the classifier alone, which is what makes it comparable
    across pipeline changes rather than confounded by them.
    """
    rows = db.confirmed_embeddings(model)
    refs = db.reference_embeddings(model)

    by_species: dict[str, list[np.ndarray]] = {}
    for name, enc in rows:
        vec = decode(enc)
        if vec is not None:
            by_species.setdefault(name.strip(), []).append(vec)

    ref_sums: dict[str, np.ndarray] = {}
    for name, enc in refs:
        vec = decode(enc)
        if vec is None:
            continue
        key = name.strip()
        ref_sums[key] = ref_sums.get(key, np.zeros_like(vec)) + vec * _REFERENCE_WEIGHT

    total = correct = 0
    per_species: dict[str, dict] = {}
    for species, vecs in by_species.items():
        if len(vecs) < 2 and species not in ref_sums:
            # Nothing to hold out against: with one example and no reference photos, the
            # only centroid available IS the test vector.
            continue
        for i, held in enumerate(vecs):
            others = [v for j, v in enumerate(vecs) if j != i]
            centroids: dict[str, np.ndarray] = {}
            for other, ovecs in by_species.items():
                pool = others if other == species else ovecs
                if not pool and other not in ref_sums:
                    continue
                acc = np.sum(pool, axis=0) if pool else np.zeros_like(held)
                if other in ref_sums:
                    acc = acc + ref_sums[other]
                norm = float(np.linalg.norm(acc))
                if norm > 0:
                    centroids[other] = acc / norm
            if not centroids:
                continue
            best = max(centroids, key=lambda n: float(np.dot(held, centroids[n])))
            total += 1
            hit = best == species
            correct += hit
            entry = per_species.setdefault(species, {"n": 0, "correct": 0})
            entry["n"] += 1
            entry["correct"] += int(hit)

    return {
        "model": model,
        "evaluated": total,
        "correct": correct,
        "accuracy": round(correct / total, 4) if total else None,
        "species": sorted(
            ({"species": k, **v} for k, v in per_species.items()),
            key=lambda x: x["n"], reverse=True,
        ),
        "note": ("Leave-one-out over confirmed detections. Species with a single example "
                 "and no reference photos are skipped — there is nothing to hold out."),
    }
