"""Build the candidate species vocabulary the classifier scores against.

This is the single biggest accuracy lever in the service. BioCLIP is a zero-shot model:
it scores an image against whatever label set you hand it, so narrowing that set from
~11,000 world taxa to the few hundred species that actually occur where the camera is
removes most of the opportunities to be confidently wrong.

Source of truth is the eBird API — a regional species list intersected with the eBird
taxonomy. Both responses are cached on disk (``cache_dir``) so a restart doesn't re-fetch
5 MB of taxonomy, and so a service restart still works when eBird is unreachable.
Without an API key we fall back to a bundled list of common North American yard birds.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from .settings import Settings

log = logging.getLogger("aviary_id.species")

_EBIRD_BASE = "https://api.ebird.org/v2"
_SPPLIST_URL = _EBIRD_BASE + "/product/spplist/{region}"
_TAXONOMY_URL = _EBIRD_BASE + "/ref/taxonomy/ebird?fmt=json"

# The eBird taxonomy changes once a year, so it can be cached far longer than the
# regional list (which we refresh on ebird_refresh_days to pick up new local records).
_TAXONOMY_TTL = 180 * 86400

_FALLBACK_FILE = os.path.join(os.path.dirname(__file__), "data", "na_fallback_species.json")

# Every bird shares these three ranks. BioCLIP was trained on full taxonomic strings, so
# including them costs nothing and keeps our label format matching its training
# distribution rather than approximating it.
_FIXED_RANKS = ("Animalia", "Chordata", "Aves")


@dataclass(frozen=True)
class Species:
    sci_name: str
    com_name: str
    order: str = ""
    family: str = ""
    # Absent for bundled-fallback entries; see the note in na_fallback_species.json.
    species_code: Optional[str] = None

    def label(self, fmt: str = "common") -> str:
        """The text fed to the encoder for this species.

        The format matters more than it looks, and getting it wrong shows up as confusion
        *within* a family rather than as general inaccuracy. With the full taxonomic string,
        Northern Cardinal, Summer Tanager and Scarlet Tanager share ``Animalia Chordata Aves
        Passeriformes Cardinalidae`` — about two thirds of the prompt is identical text, so
        the handful of characters that actually distinguish them is swamped.

        pybioclip uses two distinct approaches and does not mix them: its
        ``TreeOfLifeClassifier`` uses full taxonomy with no prompt ensemble, while
        ``CustomLabelsClassifier`` — the closer analogue to a curated regional list — uses
        the 80-template ensemble over plain label text. ``common`` follows the latter.

        * ``common``          -> "Northern Cardinal"
        * ``binomial``        -> "Cardinalis cardinalis"
        * ``binomial_common`` -> "Cardinalis cardinalis (Northern Cardinal)"
        * ``taxonomy``        -> the full rank chain, pybioclip's ``join_names`` form
        """
        if fmt == "binomial":
            return self.sci_name
        if fmt == "binomial_common":
            return f"{self.sci_name} ({self.com_name})"
        if fmt == "taxonomy":
            parts = [*_FIXED_RANKS]
            if self.order:
                parts.append(self.order)
            if self.family:
                parts.append(self.family)
            parts.append(self.sci_name)
            parts.append(self.com_name.lower())
            return " ".join(parts)
        return self.com_name

    def as_dict(self) -> dict:
        return {
            "species_code": self.species_code,
            "scientific_name": self.sci_name,
            "common_name": self.com_name,
            "order": self.order,
            "family": self.family,
        }


# --------------------------------------------------------------------------- disk cache

def _cache_path(settings: Settings, name: str) -> str:
    return os.path.join(settings.cache_dir, name)


def _read_cache(path: str, max_age: float) -> Optional[dict]:
    """Return a cached payload if it exists and is younger than ``max_age`` seconds."""
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return None
    if age > max_age:
        log.debug("Cache %s is %.1f days old; refreshing.", path, age / 86400)
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as exc:
        log.warning("Could not read cache %s: %s", path, exc)
        return None


def _read_cache_any_age(path: str) -> Optional[dict]:
    """Read a cache regardless of age — the last resort when eBird is unreachable.

    A stale species list is enormously better than no species list: the alternative is
    dropping to the generic bundled fallback and losing regional accuracy entirely
    because of a transient network failure.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_cache(path: str, payload: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        # Atomic replace so a crash mid-write can't leave a truncated cache that then
        # fails to parse on every subsequent start.
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("Could not write cache %s: %s", path, exc)


# ------------------------------------------------------------------------------- eBird

async def _ebird_get(client: httpx.AsyncClient, url: str, api_key: str):
    resp = await client.get(url, headers={"X-eBirdApiToken": api_key})
    resp.raise_for_status()
    return resp.json()


async def _fetch_taxonomy(client: httpx.AsyncClient, settings: Settings) -> Optional[dict]:
    """The full eBird taxonomy, keyed by speciesCode. ~17k entries, ~5 MB."""
    path = _cache_path(settings, "ebird_taxonomy.json")
    cached = _read_cache(path, _TAXONOMY_TTL)
    if cached:
        return cached

    try:
        rows = await _ebird_get(client, _TAXONOMY_URL, settings.ebird_api_key)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("eBird taxonomy fetch failed: %s", exc)
        return _read_cache_any_age(path)

    by_code = {
        row["speciesCode"]: {
            "sciName": row.get("sciName", ""),
            "comName": row.get("comName", ""),
            "order": row.get("order", ""),
            "family": row.get("familySciName", ""),
            "category": row.get("category", ""),
        }
        for row in rows
        if isinstance(row, dict) and row.get("speciesCode")
    }
    log.info("Fetched eBird taxonomy: %d taxa.", len(by_code))
    _write_cache(path, by_code)
    return by_code


async def _fetch_region_codes(client: httpx.AsyncClient, settings: Settings) -> Optional[list[str]]:
    path = _cache_path(settings, f"ebird_spplist_{settings.ebird_region}.json")
    cached = _read_cache(path, settings.ebird_refresh_days * 86400)
    if cached:
        return cached.get("codes")

    url = _SPPLIST_URL.format(region=settings.ebird_region)
    try:
        codes = await _ebird_get(client, url, settings.ebird_api_key)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("eBird species list for %s failed: %s", settings.ebird_region, exc)
        stale = _read_cache_any_age(path)
        return stale.get("codes") if stale else None

    if not isinstance(codes, list):
        log.warning("eBird species list for %s was not a list.", settings.ebird_region)
        return None
    log.info("Fetched eBird species list for %s: %d codes.", settings.ebird_region, len(codes))
    _write_cache(path, {"codes": codes})
    return codes


async def _load_from_ebird(settings: Settings) -> Optional[list[Species]]:
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        codes = await _fetch_region_codes(client, settings)
        if not codes:
            return None
        taxonomy = await _fetch_taxonomy(client, settings)
        if not taxonomy:
            return None

    out: list[Species] = []
    missing = 0
    for code in codes:
        entry = taxonomy.get(code)
        if not entry:
            missing += 1
            continue
        # Subspecies, hybrids, "slash" records (e.g. "Cooper's/Sharp-shinned Hawk") and
        # spuhs are all real eBird categories but useless as classifier targets — they
        # would compete with the true species for probability mass.
        if entry.get("category") != "species":
            continue
        if not entry.get("sciName") or not entry.get("comName"):
            continue
        out.append(Species(
            sci_name=entry["sciName"],
            com_name=entry["comName"],
            order=entry.get("order", ""),
            family=entry.get("family", ""),
            species_code=code,
        ))
    if missing:
        log.debug("%d region codes had no taxonomy entry (stale taxonomy cache?).", missing)
    return out or None


# ---------------------------------------------------------------------------- fallback

def _load_fallback() -> list[Species]:
    with open(_FALLBACK_FILE, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return [
        Species(
            sci_name=row["sciName"],
            com_name=row["comName"],
            order=row.get("order", ""),
            family=row.get("family", ""),
        )
        for row in payload["species"]
    ]


# ------------------------------------------------------------------------- overrides

def _apply_overrides(species: list[Species], settings: Settings) -> list[Species]:
    """Apply EXCLUDE_SPECIES / EXTRA_SPECIES, matching either name case-insensitively."""
    if settings.exclude_species:
        drop = {n.lower() for n in settings.exclude_species}
        # Snapshot the names present BEFORE filtering: an EXCLUDE_SPECIES entry that
        # matched nothing is almost always a typo, and silently doing nothing leaves the
        # user believing a species is suppressed when it is not.
        present = {s.com_name.lower() for s in species} | {s.sci_name.lower() for s in species}
        before = len(species)
        species = [
            s for s in species
            if s.com_name.lower() not in drop and s.sci_name.lower() not in drop
        ]
        removed = before - len(species)
        if removed:
            log.info("Excluded %d species by configuration.", removed)
        for name in sorted(drop - present):
            log.warning(
                "EXCLUDE_SPECIES entry %r matched no species in the vocabulary; check "
                "the spelling against the eBird common or scientific name.", name
            )

    if settings.extra_species:
        have = {s.com_name.lower() for s in species} | {s.sci_name.lower() for s in species}
        fallback_index = {}
        for s in _load_fallback():
            fallback_index[s.com_name.lower()] = s
            fallback_index[s.sci_name.lower()] = s
        for name in settings.extra_species:
            key = name.lower()
            if key in have:
                continue
            found = fallback_index.get(key)
            if found:
                species.append(found)
                log.info("Added extra species %r.", found.com_name)
            else:
                # Deliberately not fabricating taxonomy for an unknown name: a wrong
                # order/family in the label string would degrade that species' embedding.
                log.warning(
                    "EXTRA_SPECIES entry %r is not in the eBird taxonomy or the bundled "
                    "list; skipping. Use the exact eBird common or scientific name.", name
                )
    return species


# ----------------------------------------------------------------------------- public

async def load_species(settings: Settings) -> tuple[list[Species], str]:
    """Resolve the active vocabulary. Returns (species, source-description)."""
    source = "bundled fallback"
    species: Optional[list[Species]] = None

    if settings.ebird_enabled:
        species = await _load_from_ebird(settings)
        if species:
            source = f"eBird {settings.ebird_region}"
        else:
            log.warning(
                "Falling back to the bundled species list. Regional accuracy will be "
                "lower — check EBIRD_API_KEY and EBIRD_REGION."
            )
    elif settings.ebird_api_key or settings.ebird_region:
        # One of the pair set without the other is a misconfiguration that would
        # otherwise degrade silently to the fallback list.
        log.warning(
            "EBIRD_API_KEY and EBIRD_REGION must BOTH be set to use a regional list; "
            "using the bundled fallback."
        )

    if not species:
        species = _load_fallback()

    species = _apply_overrides(species, settings)

    # Deduplicate on scientific name — extras can collide with the regional list, and a
    # duplicate label would split probability mass across two identical entries.
    seen: set[str] = set()
    unique: list[Species] = []
    for s in species:
        key = s.sci_name.lower()
        if key not in seen:
            seen.add(key)
            unique.append(s)

    unique.sort(key=lambda s: s.com_name)
    log.info("Species vocabulary: %d species (source: %s).", len(unique), source)
    return unique, source
