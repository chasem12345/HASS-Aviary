"""'Not a bird' matching (0.37.0): is this crop one the user already said was no bird?

The examples are embeddings of crops marked "Not a bird" — a leaf, a feeder part, a
squirrel Frigate called a bird. A new crop whose embedding sits at least
``identify_not_bird_similarity`` (cosine) from any of them is set aside instead of being
named. Nearest single example rather than a pooled score: one leaf that keeps triggering
Frigate is exactly the case, and a pool would dilute it with every other negative.

The vectors are cached per embedding key and dropped whenever an example is added or
removed. Thread-safe: identification workers call ``match`` from worker threads.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

import numpy as np

from . import db, probe

log = logging.getLogger("aviary.notbird")

_threshold = 0.0
_lock = threading.Lock()
_cache: dict[str, tuple[list[int], Optional[np.ndarray]]] = {}


def configure(threshold: float) -> None:
    """0 turns the gate off (examples are still recorded)."""
    global _threshold
    _threshold = max(0.0, min(1.0, float(threshold or 0.0)))


def threshold() -> float:
    return _threshold


def invalidate() -> None:
    with _lock:
        _cache.clear()


def _matrix(model: str) -> tuple[list[int], Optional[np.ndarray]]:
    with _lock:
        hit = _cache.get(model)
    if hit is not None:
        return hit
    ids: list[int] = []
    vecs: list[np.ndarray] = []
    for ex_id, emb in db.not_bird_vectors(model):
        v = probe.decode(emb)
        if v is not None:
            ids.append(ex_id)
            vecs.append(v)
    built = (ids, np.stack(vecs) if vecs else None)
    with _lock:
        _cache[model] = built
    return built


def match(embedding: Optional[str], model: Optional[str]) -> Optional[tuple[int, float]]:
    """(example id, cosine) of the nearest negative at or above the threshold, else None."""
    if _threshold <= 0 or not embedding or not model:
        return None
    vec = probe.decode(embedding)
    if vec is None:
        return None
    ids, mat = _matrix(model)
    if mat is None or mat.shape[1] != vec.shape[0]:
        return None
    sims = mat @ vec
    best = int(np.argmax(sims))
    sim = float(sims[best])
    return (ids[best], sim) if sim >= _threshold else None


def count(model: Optional[str] = None) -> int:
    if model:
        return len(_matrix(model)[0])
    return len(db.not_bird_list())
