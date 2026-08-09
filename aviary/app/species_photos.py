"""Reference photos per species, from iNaturalist taxon photos.

The camera snapshot answers "what did we catch"; these answer "what is this bird supposed
to look like" — the visual counterpart to ``species_audio``, and the thing that makes an
unconfirmed detection judgeable. They sit alongside the snapshot rather than replacing it,
so a species that has been seen still has something to compare against.

Photos come from ``GET /v1/taxa/{id}``'s ``taxon_photos`` array — the same endpoint
``species_info`` already queries for family/order — reusing the cached iNaturalist taxon
id. Only reusably-licensed photos are kept: iNaturalist returns ``license_code: null`` for
all-rights-reserved photos, which are the most common kind, so the filter is doing real
work rather than being a formality. Attribution is stored and must always be displayed.

Results are cached in SQLite (``species_photos``) with the same TTLs as ``species_info``.
The image bytes are never stored: ``routes/media.py`` streams them from iNaturalist on
demand through the existing proxy, matching every other media asset in Aviary. All
failures are soft — the page just omits the strip.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

from . import db, species_info

log = logging.getLogger("aviary.species_photos")

_TTL_OK = 30 * 86400      # refresh a good hit monthly
_TTL_FAIL = 3 * 86400     # retry misses in a few days, not every page load

_INAT_TAXON = "https://api.inaturalist.org/v1/taxa/{}"
_PHOTO_PAGE = "https://www.inaturalist.org/photos/{}"

# How many to keep. Enough to show plumage variation (male/female, juvenile) without
# turning the species page into a gallery.
_MAX_PHOTOS = 3

# Same set as species_audio. iNaturalist uses `null` for "all rights reserved", which is
# excluded by never matching here.
_LICENSES = ("cc0", "cc-by", "cc-by-nc", "cc-by-sa", "cc-by-nc-sa")

_client: Optional[httpx.AsyncClient] = None


def init_client() -> None:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0),
            follow_redirects=True,
            headers={"User-Agent": species_info.USER_AGENT, "Accept": "application/json"},
        )


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def resolve(common_name: str, scientific_name: Optional[str] = None) -> list[dict]:
    """Cached reference photos for a species, fetching on a miss. Never raises."""
    common_name = (common_name or "").strip()
    if not common_name or common_name.lower() == "bird":
        return []

    cached = db.get_species_photos(common_name)
    if cached and _fresh(cached[0]):
        return [_public(r) for r in cached if r.get("ok")]

    rows = await _fetch(common_name, (scientific_name or "").strip() or None)
    try:
        db.put_species_photos(common_name, rows)
    except Exception:  # noqa: BLE001 - caching is best-effort
        log.exception("Failed to cache species_photos for %s", common_name)
    return [_public(r) for r in rows if r.get("ok")]


async def file_url(common_name: str, position: int,
                   scientific_name: Optional[str] = None) -> Optional[str]:
    """Upstream image URL for one photo, or None. Used by the media proxy."""
    photos = db.get_species_photos(common_name)
    if not photos or not _fresh(photos[0]):
        await resolve(common_name, scientific_name)
        photos = db.get_species_photos(common_name)
    for row in photos:
        if row.get("position") == position and row.get("ok"):
            return row.get("file_url")
    return None


def _fresh(row: dict) -> bool:
    age = time.time() - (row.get("fetched_at") or 0)
    return age < (_TTL_OK if row.get("ok") else _TTL_FAIL)


def _miss(common: str) -> list[dict]:
    """A single not-ok row, so the failure itself is cached against the short TTL."""
    return [{
        "common_name": common, "position": 0, "photo_id": None, "file_url": None,
        "thumb_url": None, "license_code": None, "attribution": None, "source_url": None,
        "fetched_at": time.time(), "ok": 0,
    }]


async def _fetch(common: str, sci: Optional[str]) -> list[dict]:
    if _client is None:
        return _miss(common)

    # Reuses the taxon lookup the About card already performs and caches.
    taxon_id = await species_info.taxon_id(common, sci)
    if not taxon_id:
        log.debug("No iNaturalist taxon for %r; no reference photos.", common)
        return _miss(common)

    try:
        resp = await _client.get(_INAT_TAXON.format(taxon_id))
        if resp.status_code != 200:
            log.debug("iNaturalist taxon %s returned %s", taxon_id, resp.status_code)
            return _miss(common)
        results = (resp.json() or {}).get("results") or []
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("iNaturalist taxon lookup failed for %s: %s", taxon_id, exc)
        return _miss(common)
    if not results:
        return _miss(common)

    now = time.time()
    rows: list[dict] = []
    for entry in results[0].get("taxon_photos") or []:
        photo = entry.get("photo") or {}
        license_code = (photo.get("license_code") or "").lower()
        # No licence at all means all rights reserved — never re-serve those.
        if license_code not in _LICENSES:
            continue
        # medium is the display size; without a URL there is nothing to show.
        file_url_ = photo.get("medium_url") or photo.get("url")
        if not file_url_:
            continue
        attribution = (photo.get("attribution") or "").strip()
        if not attribution:
            # The UI hides any photo it can't credit, so don't cache one either.
            continue
        photo_id = photo.get("id")
        rows.append({
            "common_name": common,
            "position": len(rows),
            "photo_id": str(photo_id) if photo_id is not None else None,
            "file_url": file_url_,
            "thumb_url": photo.get("square_url") or file_url_,
            "license_code": license_code,
            "attribution": attribution,
            "source_url": _PHOTO_PAGE.format(photo_id) if photo_id is not None else None,
            "fetched_at": now,
            "ok": 1,
        })
        if len(rows) >= _MAX_PHOTOS:
            break
    return rows or _miss(common)


def _public(row: dict) -> dict:
    """Shape returned to callers. ``media_url`` is filled in by the route (it needs the
    ingress prefix); ``file_url`` stays server-side."""
    return {
        "position": row.get("position"),
        "attribution": row.get("attribution"),
        "license_code": row.get("license_code"),
        "source_url": row.get("source_url"),
    }
