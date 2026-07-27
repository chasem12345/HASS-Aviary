"""Reference recordings per species, from iNaturalist observations.

BirdNET-Go gives you the audio it classified; this gives you a *known* recording of the
same species to compare it against — the quickest way to judge whether a classification
is real before deciding to blacklist it.

Only research-grade observations (identification confirmed by the community) under a
reusable licence are considered. Results are cached in SQLite (``species_audio``) with
the same TTLs as ``species_info``, so a species hits the API at most once a month. No API
key is needed. All failures are soft — the species page just omits the card.

The audio file itself is never stored: ``routes/media.py`` streams it from iNaturalist on
demand through the existing Range-aware proxy, matching how every other media asset in
Aviary is handled.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

from . import db, species_info

log = logging.getLogger("aviary.species_audio")

_TTL_OK = 30 * 86400      # refresh a good hit monthly
_TTL_FAIL = 3 * 86400     # retry misses in a few days, not every page load

_INAT_OBSERVATIONS = "https://api.inaturalist.org/v1/observations"
_OBSERVATION_PAGE = "https://www.inaturalist.org/observations/{}"

# Licences we're willing to re-serve. Attribution is rendered for all of them (a
# condition of every CC-BY variant), so the only ones excluded are those that reserve
# all rights or forbid redistribution. Passed as iNaturalist's `sound_license` filter
# *and* re-checked on each result, so a typo in the parameter name can't silently let
# all-rights-reserved audio through.
_LICENSES = ("cc0", "cc-by", "cc-by-nc", "cc-by-sa", "cc-by-nc-sa")

_client: Optional[httpx.AsyncClient] = None


def init_client() -> None:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0),
            follow_redirects=True,
            # Same descriptive User-Agent as species_info; iNaturalist asks that API
            # clients identify themselves.
            headers={"User-Agent": species_info.USER_AGENT, "Accept": "application/json"},
        )


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def resolve(common_name: str, scientific_name: Optional[str] = None) -> dict:
    """Return cached reference-audio metadata, fetching + caching on a miss."""
    common_name = (common_name or "").strip()
    if not common_name or common_name.lower() == "bird":
        return _public({"common_name": common_name, "ok": 0})

    cached = db.get_species_audio(common_name)
    if cached and _fresh(cached):
        return _public(cached)

    row = await _fetch(common_name, (scientific_name or "").strip() or None)
    try:
        db.put_species_audio(row)
    except Exception:  # noqa: BLE001 - caching is best-effort
        log.exception("Failed to cache species_audio for %s", common_name)
    return _public(row)


async def file_url(common_name: str, scientific_name: Optional[str] = None) -> Optional[str]:
    """The upstream audio URL for a species, or None. Used by the media proxy."""
    info = await resolve(common_name, scientific_name)
    return info.get("file_url") if info.get("ok") else None


def _fresh(row: dict) -> bool:
    age = time.time() - (row.get("fetched_at") or 0)
    return age < (_TTL_OK if row.get("ok") else _TTL_FAIL)


async def _fetch(common: str, sci: Optional[str]) -> dict:
    row = {
        "common_name": common, "taxon_id": None, "sound_id": None,
        "observation_id": None, "file_url": None, "content_type": None,
        "license_code": None, "attribution": None,
        "fetched_at": time.time(), "ok": 0,
    }
    if _client is None:
        return row

    # Reuses the taxon lookup the About card already performs and caches.
    taxon_id = await species_info.taxon_id(common, sci)
    if not taxon_id:
        log.debug("No iNaturalist taxon for %r; no reference audio.", common)
        return row
    row["taxon_id"] = taxon_id

    sound, observation_id = await _find_sound(taxon_id)
    if not sound:
        return row

    row.update({
        "sound_id": sound.get("id"),
        "observation_id": observation_id,
        "file_url": sound.get("file_url"),
        "content_type": sound.get("file_content_type"),
        "license_code": sound.get("license_code"),
        "attribution": sound.get("attribution"),
        "ok": 1,
    })
    return row


async def _find_sound(taxon_id: int) -> tuple[Optional[dict], Optional[int]]:
    """Best available sound for a taxon, as ``(sound, observation_id)``.

    ``order_by=votes`` surfaces recordings the community has actually faved, which is a
    decent proxy for "a clear example of this bird" rather than a distant noisy one.

    Only ``sound_license`` is filtered on, not ``license`` — the latter constrains the
    observation's own licence, which says nothing about the audio and only shrinks the
    candidate pool.
    """
    try:
        resp = await _client.get(
            _INAT_OBSERVATIONS,
            params={
                "taxon_id": taxon_id,
                "sounds": "true",
                "quality_grade": "research",
                "sound_license": ",".join(_LICENSES),
                "order_by": "votes",
                "per_page": 5,
                "locale": "en",
            },
        )
        if resp.status_code != 200:
            log.debug("iNaturalist observations returned %s for taxon %s",
                      resp.status_code, taxon_id)
            return None, None
        results = (resp.json() or {}).get("results") or []
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("iNaturalist observations lookup failed for taxon %s: %s", taxon_id, exc)
        return None, None

    for obs in results:
        for sound in obs.get("sounds") or []:
            # A sound with no direct file URL can't be played (some are external
            # embeds); one outside the licence set must not be re-served even if the
            # upstream filter let it through.
            if not sound.get("file_url"):
                continue
            if (sound.get("license_code") or "").lower() not in _LICENSES:
                log.debug("Skipping sound %s: licence %r not reusable",
                          sound.get("id"), sound.get("license_code"))
                continue
            return sound, obs.get("id")
    return None, None


def _public(row: dict) -> dict:
    """Shape returned to callers. ``media_url`` is filled in by the route (it needs the
    ingress prefix); ``file_url`` stays server-side."""
    ok = bool(row.get("ok"))
    obs_id = row.get("observation_id")
    return {
        "ok": ok,
        "common_name": row.get("common_name"),
        "file_url": row.get("file_url"),
        "content_type": row.get("content_type"),
        "license_code": row.get("license_code"),
        "attribution": row.get("attribution"),
        "observation_url": _OBSERVATION_PAGE.format(obs_id) if obs_id else None,
    }
