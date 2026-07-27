"""Per-species ecological traits (diet, habitat, foraging) from a bundled AVONET subset.

Unlike the Wikipedia/iNaturalist lookups in ``species_info``, this is purely local: a
gzipped CSV shipped with the add-on (``data/avonet_diet.csv.gz``, ~88 KB, 10,661 species).
No network, no API key, no cache TTL — so it works on a fresh install and offline.

Keyed on eBird-taxonomy scientific names, which is what BirdNET-Go emits, so lookups match
without synonym handling. Frigate-only species have no scientific name recorded and
therefore no traits; callers treat a miss as "just don't show the field".

Data: AVONET (Tobias et al. 2022), CC BY 4.0 — see data/AVONET-CITATION.txt.
"""

from __future__ import annotations

import csv
import gzip
import logging
import os
import sys
import threading
from typing import Optional

log = logging.getLogger("aviary.traits")

_DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "avonet_diet.csv.gz")

# AVONET's trophic niche is the closest thing it has to "what does it eat". The terms are
# jargon, so each maps to a plain description; the original term is kept alongside for
# anyone who wants it. Values match AVONET's Trophic.Niche vocabulary exactly.
_FOOD = {
    "invertivore": "Insects & invertebrates",
    "granivore": "Seeds & grain",
    "frugivore": "Fruit",
    "nectarivore": "Nectar",
    "vertivore": "Small vertebrates",
    "aquatic predator": "Fish & aquatic prey",
    "scavenger": "Carrion",
    "herbivore terrestrial": "Plants & foliage",
    "herbivore aquatic": "Aquatic plants",
    "omnivore": "Varied — omnivore",
}

# Primary.Lifestyle describes where a bird forages, which is the natural companion to what
# it eats.
_FORAGING = {
    "insessorial": "Perching",
    "terrestrial": "On the ground",
    "aerial": "In flight",
    "aquatic": "In water",
    "generalist": "Generalist",
}

_table: Optional[dict[str, tuple[str, str, str, str]]] = None
_lock = threading.Lock()


def _load() -> dict[str, tuple[str, str, str, str]]:
    """Read the bundled table into memory once, on first lookup.

    Loaded lazily so startup stays fast (it runs before the web server binds its port),
    and under a lock because species pages are served concurrently. Category strings are
    interned — there are only a handful of distinct values across 10k rows, so this keeps
    the table to roughly the size of the species names alone.
    """
    global _table
    with _lock:
        if _table is not None:
            return _table
        table: dict[str, tuple[str, str, str, str]] = {}
        try:
            with gzip.open(_DATA_PATH, "rt", encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    name = (row.get("species") or "").strip().lower()
                    if not name:
                        continue
                    table[name] = (
                        sys.intern(row.get("niche") or ""),
                        sys.intern(row.get("level") or ""),
                        sys.intern(row.get("lifestyle") or ""),
                        sys.intern(row.get("habitat") or ""),
                    )
        except (OSError, ValueError) as exc:
            # A missing or corrupt table must not break species pages.
            log.warning("Could not load bundled traits table: %s", exc)
        else:
            log.info("Loaded ecological traits for %d species.", len(table))
        _table = table
        return _table


def lookup(scientific_name: Optional[str]) -> Optional[dict]:
    """Traits for a scientific name, or None when the species isn't in the table.

    Returns display-ready strings: ``food`` is the plain-English description, ``niche``
    the AVONET term behind it.
    """
    if not scientific_name:
        return None
    row = _load().get(scientific_name.strip().lower())
    if row is None:
        return None
    niche, level, lifestyle, habitat = row
    key = niche.lower()
    out = {
        "food": _FOOD.get(key) or (niche or None),
        "niche": niche or None,
        "trophic_level": level or None,
        "foraging": _FORAGING.get(lifestyle.lower()) or (lifestyle or None),
        "habitat": habitat or None,
    }
    return out if any(out.values()) else None
