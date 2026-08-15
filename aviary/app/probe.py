"""Learn each species' appearance from your own confirmed birds.

BioCLIP is used zero-shot: an image embedding is compared against text embeddings of
species names. That is what makes an arbitrary regional species list possible, and it is
also where most of the model's accuracy is left unused. On NABirds the same frozen
embeddings score 74.9% zero-shot and 92.4% under a trained probe — a +17.5 point gap that
costs no new model, no new hardware, and no extra inference.

This closes that gap with a nearest-example (kNN) classifier over detections a human has
confirmed: a query is scored per species by the mean of its top-k most similar stored
examples. Chosen over a single per-species centroid deliberately — many feeder species
are strongly dimorphic (a male Northern Cardinal is crimson, the female warm brown, the
fledgling scruffier still), and averaging those into one prototype produces a vector that
resembles none of them. Nearest-example matching lets a shaded female match the stored
female frames directly. Chosen over logistic regression because it works from a *single*
example, needs no training loop or hyperparameters, and updates incrementally.

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

# Per-species score = mean similarity of the query's top-k stored examples. k > 1 so a
# single mislabeled frame cannot hand its species a perfect match; small so a species
# with a handful of examples is not punished for lacking depth.
_TOP_K = 3

# A reference photo is the right species in the wrong domain: posed, well lit, filling the
# frame, nothing like a feeder camera at 20 metres. They participate in matching with a
# small flat similarity penalty — enough that a real frame from your own camera wins any
# near-tie, without erasing their value for a species with no confirmations yet.
_REFERENCE_PENALTY = 0.05

# ...and they count for less evidence when deciding how much say the probe gets.
_REFERENCE_WEIGHT = 0.3

# Examples needed before the probe is trusted as much as it can be. Below this its
# influence scales up linearly, so one lucky example cannot swing a result.
_FULL_TRUST_AT = 5.0

# Ceiling on the probe's share of the blend. Kept below 1.0 so the zero-shot signal always
# retains a vote: the species list is regional and complete, whereas the stored examples
# only ever cover birds that have already been confirmed.
_MAX_BLEND = 0.7

# Softmax temperature over per-species similarities. CLIP-space cosines between
# same-species images cluster in a narrow band (~0.5-0.9), so a plain softmax would be
# nearly uniform; this spreads them into something comparable with the zero-shot
# probabilities.
_TEMPERATURE = 25.0

# The query must be *close* to a stored example, not merely closer than the others.
#
# Softmax is relative: a bird the probe has never seen is roughly equidistant from every
# species' examples, and softmax happily amplifies whichever random one wins into a
# confident-looking 0.7. Left unchecked that lets the probe override a correct zero-shot
# answer for a species it has no examples of — the exact failure this design is supposed
# to make impossible.
#
# So there is an absolute floor as well. Below _MIN_SIMILARITY the probe abstains entirely
# and the zero-shot answer stands untouched; between the floor and _STRONG_SIMILARITY its
# influence ramps up. In CLIP space, same-species images sit around 0.6-0.9, different
# species around 0.3-0.5, and unrelated images near zero. Under nearest-example scoring a
# repeat visitor photographed by the same camera typically lands in the 0.7-0.9 band —
# which is the point: the old single-centroid design pushed hard cases (a shaded fledgling
# against a male+female averaged prototype) down into the ramp and strangled the probe's
# weight on exactly the images it exists to rescue.
_MIN_SIMILARITY = 0.45
_STRONG_SIMILARITY = 0.70

_lock = threading.Lock()
_examples: dict[str, np.ndarray] = {}   # confirmed detections, one unit row each
_refs: dict[str, np.ndarray] = {}       # reference photos, one unit row each
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


def _collect(rows: list[tuple[str, str]]) -> tuple[dict[str, list[np.ndarray]], int]:
    """Decode (species, embedding) rows into per-species vector lists.

    Vectors whose dimension differs from the first one seen are skipped: two different
    models writing rows under the same key is the only way that happens, and mixing
    dimensions would crash every dot product downstream.
    """
    dim: Optional[int] = None
    out: dict[str, list[np.ndarray]] = {}
    skipped = 0
    for name, enc in rows:
        vec = decode(enc)
        if vec is None:
            skipped += 1
            continue
        if dim is None:
            dim = vec.size
        elif vec.size != dim:
            skipped += 1
            continue
        out.setdefault(name.strip(), []).append(vec)
    return out, skipped


def rebuild(model: str) -> dict:
    """Reload every stored example from the database. Returns a summary.

    Cheap enough to do outright rather than maintain incrementally: even a few thousand
    detections is a handful of megabytes, and a full reload has no staleness to reason
    about. Examples are held in memory only — they are derived data, and persisting them
    would add a cache-invalidation problem for no gain.
    """
    if not model:
        return {"species": 0, "examples": 0}

    confirmed, skipped_c = _collect(db.confirmed_embeddings(model))
    references, skipped_r = _collect(db.reference_embeddings(model))

    examples = {name: np.stack(vecs) for name, vecs in confirmed.items()}
    refs = {name: np.stack(vecs) for name, vecs in references.items()}
    raw = {name: len(vecs) for name, vecs in confirmed.items()}
    counts: dict[str, float] = {}
    for name in set(examples) | set(refs):
        counts[name] = (len(confirmed.get(name, ()))
                        + _REFERENCE_WEIGHT * len(references.get(name, ())))

    with _lock:
        global _examples, _refs, _counts, _raw_counts, _model
        _examples, _refs, _counts, _raw_counts, _model = (
            examples, refs, counts, raw, model)

    summary = {
        "model": model,
        "species": len(counts),
        "examples": int(sum(raw.values())),
        "reference_only": sum(1 for n in counts if not raw.get(n)),
        "skipped": skipped_c + skipped_r,
    }
    log.info(
        "Probe rebuilt: %d species from %d confirmed detection(s) "
        "(%d species on reference photos alone).",
        summary["species"], summary["examples"], summary["reference_only"],
    )
    return summary


def ready() -> bool:
    with _lock:
        return bool(_examples or _refs)


def model() -> str:
    """The embedding key the loaded examples belong to. Empty until first rebuild."""
    with _lock:
        return _model


def stats() -> dict:
    with _lock:
        return {
            "model": _model,
            "species": len(set(_examples) | set(_refs)),
            "examples": int(sum(_raw_counts.values())),
            "top": sorted(
                ({"species": k, "examples": v} for k, v in _raw_counts.items() if v),
                key=lambda x: x["examples"], reverse=True,
            )[:10],
        }


def examples_for(species: str) -> int:
    with _lock:
        return _raw_counts.get(species.strip(), 0)


def _score_pool(vec: np.ndarray,
                confirmed: Optional[np.ndarray],
                references: Optional[np.ndarray]) -> Optional[float]:
    """Mean of the top-k similarities against one species' stored examples.

    Top-k mean rather than pure max, so one mislabeled example cannot single-handedly
    claim a query; k shrinks to the pool size for sparse species, so a species with one
    example still scores (by that example alone).
    """
    sims: list[np.ndarray] = []
    if confirmed is not None and confirmed.size and confirmed.shape[1] == vec.size:
        sims.append(confirmed @ vec)
    if references is not None and references.size and references.shape[1] == vec.size:
        sims.append((references @ vec) - _REFERENCE_PENALTY)
    if not sims:
        return None
    pool = np.concatenate(sims)
    k = min(_TOP_K, pool.size)
    return float(np.sort(pool)[-k:].mean())


def _similarities(vec: np.ndarray) -> dict[str, float]:
    with _lock:
        examples = dict(_examples)
        refs = dict(_refs)
    out: dict[str, float] = {}
    for name in set(examples) | set(refs):
        score = _score_pool(vec, examples.get(name), refs.get(name))
        if score is not None:
            out[name] = score
    return out


def _weight_for(species: str) -> float:
    """How much of the blend the probe gets, from how much evidence backs this species."""
    with _lock:
        n = _counts.get(species, 0.0)
    return _MAX_BLEND * min(1.0, n / _FULL_TRUST_AT)


def blend(embedding: str, zero_shot: list[dict], model: str,
          exclude: Optional[set] = None) -> Optional[dict]:
    """Combine the zero-shot shortlist with nearest-example similarity.

    ``zero_shot`` is the slim candidate list — ``[{name, sci, code, score}, ...]`` (the
    caller normalizes the service's common_name/scientific_name shape first). Returns
    ``{name, sci, code, score, margin, candidates, probe_weight, probe_similarity,
    probe_examples}`` or None when there is nothing to add, in which case the caller
    keeps the original answer untouched.

    Species are scored from the union of both sources, not just the zero-shot shortlist:
    the whole value of the probe is being able to promote a species the text comparison
    ranked low, and intersecting first would throw exactly that away.
    """
    with _lock:
        if (not (_examples or _refs)) or model != _model:
            return None

    vec = decode(embedding or "")
    if vec is None:
        return None

    zero = {c["name"]: float(c.get("score") or 0.0) for c in zero_shot if c.get("name")}
    meta = {c["name"]: c for c in zero_shot if c.get("name")}

    sims = _similarities(vec)
    if exclude:
        # Exclusions arrive as common OR scientific names (the blacklist contributes
        # both, and the service matches either). The probe must honor both forms too:
        # matching common names only once let it re-promote a species the service had
        # just banned, and the two layers disagreeing about who is excluded produced a
        # genuinely baffling identification.
        drop = {e.strip().lower() for e in exclude}

        def _banned(name: str) -> bool:
            if name.lower() in drop:
                return True
            sci = (meta.get(name) or {}).get("sci") or ""
            return bool(sci) and sci.lower() in drop

        sims = {k: v for k, v in sims.items() if not _banned(k)}
    if not sims:
        return None

    # Abstain unless the image genuinely resembles something we have seen. See the note on
    # _MIN_SIMILARITY: without this, a bird with no examples gets assigned to whichever
    # species it is accidentally nearest, and can outvote a correct zero-shot answer.
    best_sim = max(sims.values())
    if best_sim < _MIN_SIMILARITY:
        log.debug("Probe abstaining: best similarity %.3f is below %.2f.",
                  best_sim, _MIN_SIMILARITY)
        return None
    closeness = min(1.0, (best_sim - _MIN_SIMILARITY)
                    / max(1e-6, _STRONG_SIMILARITY - _MIN_SIMILARITY))

    # Similarities -> a probability distribution comparable with the zero-shot one.
    names = list(sims)
    scaled = np.array([sims[n] for n in names], dtype=np.float32) * _TEMPERATURE
    scaled -= scaled.max()
    probs = np.exp(scaled)
    probs /= probs.sum()
    probe = dict(zip(names, probs.tolist()))

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

    Each confirmed embedding is scored against the example pools *without* it, so a
    species with a single example cannot trivially match itself. This deliberately touches
    no clips, crops or ffmpeg — it measures the classifier alone, which is what makes it
    comparable across pipeline changes rather than confounded by them. It is also the
    regression gate for every constant in this file.
    """
    confirmed, _ = _collect(db.confirmed_embeddings(model))
    references, _ = _collect(db.reference_embeddings(model))

    conf_mats = {name: np.stack(vecs) for name, vecs in confirmed.items()}
    ref_mats = {name: np.stack(vecs) for name, vecs in references.items()}
    species_names = set(conf_mats) | set(ref_mats)

    total = correct = 0
    per_species: dict[str, dict] = {}
    for species, vecs in confirmed.items():
        if len(vecs) < 2 and species not in ref_mats:
            # Nothing to hold out against: with one example and no reference photos, the
            # only pool available IS the test vector.
            continue
        for i, held in enumerate(vecs):
            best_name, best_score = None, -math.inf
            for other in species_names:
                pool = conf_mats.get(other)
                if other == species and pool is not None:
                    pool = np.delete(pool, i, axis=0) if len(pool) > 1 else None
                score = _score_pool(held, pool, ref_mats.get(other))
                if score is not None and score > best_score:
                    best_name, best_score = other, score
            if best_name is None:
                continue
            total += 1
            hit = best_name == species
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
