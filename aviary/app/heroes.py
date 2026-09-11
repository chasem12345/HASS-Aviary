"""The hero image for a species: which sighting's picture stands for the bird.

Pure selection over the candidate rows ``db.species_heroes`` returns (newest first). The
rules exist because of two ways the obvious "newest snapshot" goes wrong:

* A species that was only *also in view* in another bird's event inherits that event's
  snapshot in the sightings view — Frigate's thumbnail is of the tracked bird, so the
  Carolina Wren tile showed a House Finch. Such a sighting is usable only through the
  bird's own stored crop (``{event}-{idx}.jpg``), never through the event's thumbnail.
* Frigate expires footage; the add-on caches nothing except the identification crops,
  which never expire. So a stored crop beats a newer Frigate thumbnail, and among
  thumbnails a kept event (📌 or keepsake) beats one that may already be gone.

``has_crop`` is injected so the rules are testable without a crops directory.
"""

from __future__ import annotations

from typing import Callable, Optional

from . import crops, db


def _rep(row: dict) -> dict:
    """The shape the ``rep_src`` template macro consumes."""
    return {
        "id": row.get("id"),
        "source_ref": row["source_ref"],
        "subject_idx": int(row.get("subject_idx") or 0),
        "start_time": row.get("start_time"),
    }


def pick(candidates: list[dict], has_crop: Callable[[str, int], bool] = crops.exists) -> Optional[dict]:
    """Choose the hero among a species' candidate sightings (newest first).

    1. The newest sighting with the bird's own stored crop — primary or subject.
    2. Else the newest *primary* sighting with a snapshot that is kept (its thumbnail
       is guaranteed to exist at Frigate).
    3. Else the newest primary sighting with a snapshot (the template falls back to the
       species photo if Frigate has expired it).
    4. Else None: the species has no picture of its own, and the generic species photo
       is better than another bird's.
    """
    for row in candidates:
        if has_crop(row["source_ref"], int(row.get("subject_idx") or 0)):
            return _rep(row)
    primaries = [r for r in candidates
                 if not int(r.get("subject_idx") or 0) and int(r.get("has_snapshot") or 0)]
    for row in primaries:
        if row.get("retained_at") is not None or row.get("is_keepsake"):
            return _rep(row)
    if primaries:
        return _rep(primaries[0])
    return None


def for_species(names: list[str], start: Optional[float] = None,
                end: Optional[float] = None) -> dict[str, Optional[dict]]:
    """Hero per species name (missing when the species has no usable picture)."""
    picked = db.species_heroes(names, start=start, end=end)
    out: dict[str, Optional[dict]] = {}
    for name in names:
        hero = pick(picked.get(name, []))
        if hero is not None:
            out[name] = hero
    return out
