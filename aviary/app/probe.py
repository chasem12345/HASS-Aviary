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
* **It learns only from labels a person typed.** Training on the model's own unreviewed
  guesses is how a classifier teaches itself its own mistakes; ``identify_learn_from_auto``
  opts back in to learning from confident automatic answers.
* **Every example is traceable.** The probe keeps which detection (and which bird in it)
  each vector came from, so a bad match can be walked back to a card, and a single
  example can be excluded from learning or re-embedded without deleting anything.
"""

from __future__ import annotations

import base64
import logging
import math
import threading
from dataclasses import asdict, dataclass, replace
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

# How alike two examples from ONE visit must be to count as the same frame twice. Frigate's
# tracker splits a bird's stay into many event ids, and each of them stores a frame — so
# a single 90-second bath visit can contribute twenty near-identical vectors, any three
# of which fill a top-3 pool and make that one visit the species' whole definition.
_DUP_SIMILARITY = 0.97
# ...and after de-duplication, at most this many examples per (species, visit). One below
# _TOP_K on purpose: one visit can never fill the pool by itself. Repeat visitors on other
# days are independent evidence and are kept.
_MAX_PER_VISIT = 2
# An example is "suspicious" when it sits nearer another species' examples than its own
# (leave-one-VISIT-out, so its own visit's clones cannot vouch for it) by more than this.
_SUSPICIOUS_MARGIN = 0.0

_lock = threading.Lock()
_examples: dict[str, np.ndarray] = {}   # examples in USE, one unit row each
_refs: dict[str, np.ndarray] = {}       # reference photos, one unit row each
_counts: dict[str, float] = {}          # effective (weighted) example count per species
_raw_counts: dict[str, int] = {}        # examples in use, for display
_model: str = ""
# Provenance. _meta[species] is row-aligned with _examples[species]; _all[species] holds
# every labelled example (used or not) for the browser and the audit.
_meta: dict[str, list["Example"]] = {}
_all: dict[str, list[tuple["Example", np.ndarray]]] = {}
_summary: dict = {}
_learn_from_auto = False


@dataclass(frozen=True)
class Example:
    """One learnable example and where it came from."""
    species: str
    detection_id: int
    subject_idx: int                 # 0 = the detection itself; 1+ = another bird in view
    visit_id: Optional[int]
    start_time: Optional[float]
    source_ref: Optional[str]
    label_source: str                # manual | auto
    target: Optional[str]            # species the frame was chosen for; None = legacy
    excluded: bool
    suspicious: Optional[dict]       # {margin, species, at} from the last audit, or None
    used: bool = True
    dropped_reason: Optional[str] = None   # excluded | duplicate | visit_cap

    def as_dict(self) -> dict:
        d = asdict(self)
        d["untargeted"] = (self.target or "").lower() != self.species.lower()
        return d


def configure(learn_from_auto: bool = False) -> None:
    """Whether the identifier's own passing answers count as examples (default: no)."""
    global _learn_from_auto
    _learn_from_auto = bool(learn_from_auto)


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


def _decode_examples(rows: list[dict]) -> tuple[list[tuple[Example, np.ndarray]], int]:
    """db.learning_examples rows -> (Example, unit vector) pairs, same dimension guard."""
    dim: Optional[int] = None
    out: list[tuple[Example, np.ndarray]] = []
    skipped = 0
    for r in rows:
        vec = decode(r.get("embedding") or "")
        if vec is None or (dim is not None and vec.size != dim):
            skipped += 1
            continue
        dim = dim or vec.size
        suspicious = None
        if r.get("suspicious_at"):
            suspicious = {"margin": r.get("suspicious_margin"),
                          "species": r.get("suspicious_species"), "at": r.get("suspicious_at")}
        out.append((Example(
            species=(r.get("species") or "").strip(), detection_id=int(r["detection_id"]),
            subject_idx=int(r.get("subject_idx") or 0), visit_id=r.get("visit_id"),
            start_time=r.get("start_time"), source_ref=r.get("source_ref"),
            label_source=r.get("label_source") or "manual", target=r.get("target"),
            excluded=bool(r.get("excluded_at")), suspicious=suspicious,
        ), vec))
    return out, skipped


def _select(items: list[tuple[Example, np.ndarray]]) -> list[tuple[Example, np.ndarray]]:
    """Decide which of one species' examples are USED: drop excluded ones, then within
    each visit drop near-duplicate frames and cap what one visit may contribute."""
    out: list[tuple[Example, np.ndarray]] = []
    groups: dict[int, list[tuple[Example, np.ndarray]]] = {}
    for ex, vec in items:
        if ex.excluded:
            out.append((replace(ex, used=False, dropped_reason="excluded"), vec))
        elif ex.visit_id is None:
            out.append((ex, vec))          # nothing says these are the same stay
        else:
            groups.setdefault(ex.visit_id, []).append((ex, vec))
    for members in groups.values():
        members.sort(key=lambda p: p[0].start_time or 0.0)
        kept: list[tuple[Example, np.ndarray]] = []
        for ex, vec in members:
            if any(float(vec @ kv) >= _DUP_SIMILARITY for _, kv in kept):
                out.append((replace(ex, used=False, dropped_reason="duplicate"), vec))
            else:
                kept.append((ex, vec))
        if len(kept) > _MAX_PER_VISIT:
            # Farthest-point pick: the first frame, then whichever is least like what is
            # already kept — the two most different looks at the bird, not the two first.
            chosen, rest = [kept[0]], kept[1:]
            while len(chosen) < _MAX_PER_VISIT and rest:
                pick = min(rest, key=lambda p: max(float(p[1] @ c[1]) for c in chosen))
                rest.remove(pick)
                chosen.append(pick)
            out.extend((replace(ex, used=False, dropped_reason="visit_cap"), vec)
                       for ex, vec in rest)
            kept = chosen
        out.extend(kept)
    out.sort(key=lambda p: -(p[0].start_time or 0.0))
    return out


@dataclass
class _Loaded:
    examples: dict[str, np.ndarray]
    meta: dict[str, list[Example]]
    all_rows: dict[str, list[tuple[Example, np.ndarray]]]
    refs: dict[str, np.ndarray]
    counts: dict[str, float]
    raw: dict[str, int]
    summary: dict


def _load(model: str) -> _Loaded:
    """Everything a rebuild (or an evaluation) needs, without touching module state."""
    rows = db.learning_examples(model, include_auto=_learn_from_auto)
    pairs, skipped_c = _decode_examples(rows)
    references, skipped_r = _collect(db.reference_embeddings(model))

    by_species: dict[str, list[tuple[Example, np.ndarray]]] = {}
    for ex, vec in pairs:
        by_species.setdefault(ex.species, []).append((ex, vec))
    examples: dict[str, np.ndarray] = {}
    meta: dict[str, list[Example]] = {}
    all_rows: dict[str, list[tuple[Example, np.ndarray]]] = {}
    dropped: dict[str, int] = {"excluded": 0, "duplicate": 0, "visit_cap": 0}
    for name, items in by_species.items():
        chosen = _select(items)
        all_rows[name] = chosen
        used = [(ex, vec) for ex, vec in chosen if ex.used]
        if used:
            examples[name] = np.stack([vec for _, vec in used])
            meta[name] = [ex for ex, _ in used]
        for ex, _ in chosen:
            if not ex.used:
                dropped[ex.dropped_reason or "excluded"] += 1

    refs = {name: np.stack(vecs) for name, vecs in references.items()}
    raw = {name: len(m) for name, m in meta.items()}
    counts: dict[str, float] = {}
    for name in set(examples) | set(refs):
        counts[name] = raw.get(name, 0) + _REFERENCE_WEIGHT * len(references.get(name, ()))
    summary = {
        "model": model,
        "species": len(counts),
        "labelled": len(pairs),
        "examples": int(sum(raw.values())),
        "excluded": dropped["excluded"],
        "deduped": dropped["duplicate"],
        "capped": dropped["visit_cap"],
        "auto_ignored": 0 if _learn_from_auto else db.auto_example_count(model),
        "learn_from_auto": _learn_from_auto,
        "reference_only": sum(1 for n in counts if not raw.get(n)),
        "skipped": skipped_c + skipped_r,
    }
    return _Loaded(examples, meta, all_rows, refs, counts, raw, summary)


def rebuild(model: str) -> dict:
    """Reload every stored example from the database. Returns a summary.

    Cheap enough to do outright rather than maintain incrementally: even a few thousand
    detections is a handful of megabytes, and a full reload has no staleness to reason
    about. Examples are held in memory only — they are derived data, and persisting them
    would add a cache-invalidation problem for no gain.
    """
    if not model:
        return {"species": 0, "examples": 0}
    loaded = _load(model)
    with _lock:
        global _examples, _refs, _counts, _raw_counts, _model, _meta, _all, _summary
        _examples, _refs, _counts, _raw_counts, _model = (
            loaded.examples, loaded.refs, loaded.counts, loaded.raw, model)
        _meta, _all, _summary = loaded.meta, loaded.all_rows, loaded.summary
    s = loaded.summary
    log.info(
        "Probe rebuilt: %d species from %d example(s) in use (%d labelled; %d excluded, "
        "%d duplicate, %d over the per-visit cap; %d automatic answer(s) not learned from; "
        "%d species on reference photos alone).",
        s["species"], s["examples"], s["labelled"], s["excluded"], s["deduped"],
        s["capped"], s["auto_ignored"], s["reference_only"],
    )
    return s


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
            **_summary,
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

    # Two abstention guards. The blend exists to do exactly two things: RESCUE a
    # borderline answer (same winner, more confidence) and CORRECT the winner to the
    # probe's own best-evidenced guess. Anything else is the two signals conflicting,
    # and a conflict must resolve to the service's answer — it is the stronger prior
    # now that a supervised classifier leads the fused score — not to an average.
    zero_best = max(zero, key=zero.get) if zero else None

    # A merged winner that was NOBODY'S first choice only won by averaging: the blend
    # weight was earned by probe_best's evidence and must not be spent crowning some
    # third species (seen in the wild: service said Common Ground Dove, the probe's
    # well-evidenced pick was elsewhere, and Mourning Dove — zero examples — emerged
    # at 0.153 with a 0.008 margin, a guaranteed trip to the review queue).
    if best != zero_best and best != probe_best:
        log.debug("Probe abstaining: merged winner %s was neither the service's "
                  "answer (%s) nor the probe's (%s).", best, zero_best, probe_best)
        return None

    # Keeping the same winner while LOWERING its score adds no information — the
    # probe's softmax mass just diluted it — and can only demote a passing answer.
    if best == zero_best and best_score < zero.get(best, 0.0):
        log.debug("Probe abstaining: it would only deflate %s (%.3f -> %.3f).",
                  best, zero.get(best, 0.0), best_score)
        return None

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


# ------------------------------------------------------------------ browsing and audit

def species_summary() -> list[dict]:
    """Per species: how many examples it has, how many are in use, and what is flagged."""
    with _lock:
        all_rows, refs = dict(_all), dict(_refs)
    out = []
    for name in sorted(set(all_rows) | set(refs)):
        rows = [ex for ex, _ in all_rows.get(name, [])]
        out.append({
            "species": name,
            "labelled": len(rows),
            "used": sum(1 for ex in rows if ex.used),
            "excluded": sum(1 for ex in rows if ex.excluded),
            "suspicious": sum(1 for ex in rows if ex.suspicious),
            "untargeted": sum(1 for ex in rows if ex.used
                              and (ex.target or "").lower() != name.lower()),
            "manual": sum(1 for ex in rows if ex.label_source == "manual"),
            "auto": sum(1 for ex in rows if ex.label_source != "manual"),
            "reference": int(refs[name].shape[0]) if name in refs else 0,
        })
    out.sort(key=lambda s: (-s["labelled"], s["species"]))
    return out


def _outlier_scores(species: str) -> dict[tuple[int, int], dict]:
    """How each example of one species would score as a QUERY: against its own species'
    pool with its own visit held out, and against every other species' pool.

    Scored the way the probe scores (mean of the top-k similarities, ``_score_pool``),
    not by a single nearest neighbour: one mislabelled frame pulls its true species'
    examples toward it just as hard, so nearest-neighbour flags both sides. Pool scores
    are asymmetric — the two wren frames filed under cardinal look like wrens (the wren
    pool is full of their kind), while a real wren still scores higher against the wren
    pool than against a cardinal pool that holds only two look-alikes. Own visit held out
    so a visit's near-clones cannot vouch for each other. Reference photos are left out:
    they are the right species in the wrong domain and would flag every real frame.
    """
    with _lock:
        rows = list(_all.get(species, []))
        same_pool = _examples.get(species)
        same_meta = list(_meta.get(species, []))
        others = {n: m for n, m in _examples.items() if n != species}
    if not rows:
        return {}

    def pooled(sims: np.ndarray) -> Optional[float]:
        if sims.size == 0:
            return None
        k = min(_TOP_K, sims.size)
        return float(np.sort(sims)[-k:].mean())

    out: dict[tuple[int, int], dict] = {}
    for ex, vec in rows:
        own = None
        if same_pool is not None and same_meta:
            mask = np.array([
                not (m.detection_id == ex.detection_id and m.subject_idx == ex.subject_idx)
                and not (ex.visit_id is not None and m.visit_id == ex.visit_id)
                for m in same_meta])
            own = pooled((same_pool @ vec)[mask])
        other = other_species = None
        for name, mat in others.items():
            score = pooled(mat @ vec)
            if score is not None and (other is None or score > other):
                other, other_species = score, name
        suspicious = False
        if other is not None:
            if own is None:
                suspicious = other >= _STRONG_SIMILARITY
            else:
                suspicious = other - own > _SUSPICIOUS_MARGIN
        out[(ex.detection_id, ex.subject_idx)] = {
            "own_score": None if own is None else round(own, 3),
            "other_score": None if other is None else round(other, 3),
            "other_species": other_species,
            "suspicious_now": suspicious,
        }
    return out


def examples(species: str) -> list[dict]:
    """Every labelled example of one species, newest first, with live outlier numbers.

    ``suspicious`` is the persisted verdict of the last audit (or None); ``suspicious_now``
    is recomputed against the pool as it stands.
    """
    scores = _outlier_scores(species)
    with _lock:
        rows = [ex for ex, _ in _all.get(species, [])]
    return [{**ex.as_dict(), **scores.get((ex.detection_id, ex.subject_idx), {})}
            for ex in rows]


def flag_suspicious() -> dict:
    """Audit every species and persist which examples look mislabelled. Returns a summary.

    On demand, never on rebuild: it is N² in the number of examples. Rebuilds afterwards
    so the flags show in the browser immediately.
    """
    with _lock:
        names = sorted(_all)
        model_key = _model
    flagged: list[tuple[int, int, float, str]] = []
    per_species: list[dict] = []
    for name in names:
        scores = _outlier_scores(name)
        hits = []
        for (det_id, idx), s in scores.items():
            if s["suspicious_now"]:
                margin = (s["other_score"] or 0.0) - (s["own_score"] or 0.0)
                flagged.append((det_id, idx, round(margin, 3), s["other_species"] or ""))
                hits.append({"detection_id": det_id, "subject_idx": idx, **s})
        if hits:
            per_species.append({"species": name, "flagged": len(hits), "examples": hits})
    db.set_suspicious(flagged)
    if model_key:
        rebuild(model_key)
    log.info("Learning audit: %d of the examples in use look mislabelled.", len(flagged))
    return {"flagged": len(flagged), "species": per_species}


def evaluate(model: str) -> dict:
    """Leave-one-out accuracy over the examples in use: zero-shot's peer, measured.

    Each example is scored against the pools *without* it, so a species with a single
    example cannot trivially match itself. Uses exactly the pool a rebuild would (manual
    labels only unless configured otherwise, de-duplicated and capped per visit), so the
    number describes the probe as it runs. This deliberately touches no clips, crops or
    ffmpeg — it measures the classifier alone, which is what makes it comparable across
    pipeline changes rather than confounded by them. It is also the regression gate for
    every constant in this file.
    """
    loaded = _load(model)
    conf_mats, ref_mats = loaded.examples, loaded.refs
    species_names = set(conf_mats) | set(ref_mats)

    total = correct = 0
    per_species: dict[str, dict] = {}
    for species, pool_all in conf_mats.items():
        if pool_all.shape[0] < 2 and species not in ref_mats:
            # Nothing to hold out against: with one example and no reference photos, the
            # only pool available IS the test vector.
            continue
        for i in range(pool_all.shape[0]):
            held = pool_all[i]
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
        "pool": "manual+auto" if _learn_from_auto else "manual-only",
        "species": sorted(
            ({"species": k, **v} for k, v in per_species.items()),
            key=lambda x: x["n"], reverse=True,
        ),
        "note": ("Leave-one-out over the examples in use. Species with a single example "
                 "and no reference photos are skipped — there is nothing to hold out."),
    }
