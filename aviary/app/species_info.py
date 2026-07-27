"""Per-species reference info: a Wikipedia blurb plus iNaturalist taxonomy.

Results are cached in SQLite (``species_info`` table) and refreshed monthly, so each
species hits the external APIs at most once per TTL. Both sources are free and need no
key; we send a descriptive User-Agent per Wikimedia's policy. All failures are soft —
the species page just omits the About card.
"""

from __future__ import annotations

import logging
import time
from typing import Optional
from urllib.parse import quote

import httpx

from . import db

log = logging.getLogger("aviary.species_info")

# Wikimedia asks for a descriptive User-Agent with contact/URL.
USER_AGENT ="Aviary/HomeAssistantAddon (+https://github.com/chasem12345/HASS-Aviary)"
_TTL_OK = 30 * 86400      # refresh good info monthly
_TTL_FAIL = 3 * 86400     # retry misses in a few days, not every load

_WIKI = "https://en.wikipedia.org/api/rest_v1/page/summary/{}"
_INAT_SEARCH = "https://api.inaturalist.org/v1/taxa"
_INAT_TAXON = "https://api.inaturalist.org/v1/taxa/{}"

_client: Optional[httpx.AsyncClient] = None


def init_client() -> None:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def resolve(common_name: str, scientific_name: Optional[str] = None) -> dict:
    """Return cached species info, fetching + caching on a miss / stale entry."""
    common_name = (common_name or "").strip()
    if not common_name or common_name.lower() == "bird":
        return _public({"common_name": common_name, "ok": 0})

    cached = db.get_species_info(common_name)
    if cached and _fresh(cached):
        return _public(cached)

    row = await _fetch(common_name, (scientific_name or "").strip() or None)
    try:
        db.put_species_info(row)
    except Exception:  # noqa: BLE001 - caching is best-effort
        log.exception("Failed to cache species_info for %s", common_name)
    return _public(row)


def _fresh(row: dict) -> bool:
    age = time.time() - (row.get("fetched_at") or 0)
    return age < (_TTL_OK if row.get("ok") else _TTL_FAIL)


async def taxon_id(common_name: str, scientific_name: Optional[str] = None) -> Optional[int]:
    """iNaturalist taxon id for a species, resolving + caching the info row on a miss.

    The taxon lookup already happens for the About card, so this reuses that cached
    result instead of hitting the taxa endpoint again (see ``species_audio``).
    """
    cached = db.get_species_info(common_name)
    if cached and cached.get("inat_taxon_id"):
        return int(cached["inat_taxon_id"])
    if cached and _fresh(cached):
        # Fresh row with no taxon id: iNaturalist genuinely didn't resolve this name.
        return None
    await resolve(common_name, scientific_name)
    row = db.get_species_info(common_name)
    return int(row["inat_taxon_id"]) if row and row.get("inat_taxon_id") else None


async def _fetch(common: str, sci: Optional[str]) -> dict:
    row = {
        "common_name": common, "scientific_name": sci, "descriptor": None,
        "extract": None, "wiki_url": None, "family": None, "order": None,
        "conservation": None, "fetched_at": time.time(), "ok": 0,
        "inat_taxon_id": None,
    }
    if _client is None:
        return row

    # Wikipedia: scientific name resolves to the species article most reliably;
    # fall back to the common name.
    for title in [t for t in (sci, common) if t]:
        wiki = await _wiki(title)
        if wiki:
            row["descriptor"] = row["descriptor"] or wiki.get("descriptor")
            row["wiki_url"] = row["wiki_url"] or wiki.get("url")
            if wiki.get("extract"):
                row["extract"] = wiki["extract"]
                break

    inat = await _inat(sci or common)
    if inat:
        row["family"] = inat.get("family")
        row["order"] = inat.get("order")
        row["conservation"] = inat.get("conservation")
        row["inat_taxon_id"] = inat.get("id")
        if not row["scientific_name"] and inat.get("name"):
            row["scientific_name"] = inat["name"]

    row["ok"] = 1 if (row["extract"] or row["family"]) else 0
    return row


async def _wiki(title: str) -> Optional[dict]:
    try:
        resp = await _client.get(_WIKI.format(quote(title, safe="")))
        if resp.status_code != 200:
            return None
        d = resp.json()
        if d.get("type") == "disambiguation":
            return None
        return {
            "descriptor": d.get("description"),
            "extract": d.get("extract"),
            "url": (d.get("content_urls") or {}).get("desktop", {}).get("page"),
        }
    except (httpx.HTTPError, ValueError):
        return None


async def _inat(name: str) -> Optional[dict]:
    try:
        resp = await _client.get(
            _INAT_SEARCH,
            params={
                "q": name, "rank": "species", "per_page": 1, "locale": "en",
                # Aviary only ever deals in birds; constraining the search stops a bird's
                # common name from matching an unrelated insect/plant taxon (which would
                # also mean the wrong reference recording — see species_audio).
                "iconic_taxa": "Aves",
            },
        )
        if resp.status_code != 200:
            return None
        results = (resp.json() or {}).get("results") or []
        if not results:
            return None
        top = results[0]
        out = {
            "id": top.get("id"),
            "name": top.get("name"),
            "family": None,
            "order": None,
            "conservation": _conservation(top.get("conservation_status")),
        }
        # Ancestors (family/order names) live on the taxon detail endpoint.
        tid = top.get("id")
        if tid:
            det_resp = await _client.get(_INAT_TAXON.format(tid), params={"locale": "en"})
            if det_resp.status_code == 200:
                det = ((det_resp.json() or {}).get("results") or [{}])[0]
                out["conservation"] = out["conservation"] or _conservation(
                    det.get("conservation_status")
                )
                for anc in det.get("ancestors") or []:
                    if anc.get("rank") == "family":
                        out["family"] = anc.get("name")
                    elif anc.get("rank") == "order":
                        out["order"] = anc.get("name")
        return out
    except (httpx.HTTPError, ValueError):
        return None


def _conservation(cs: Optional[dict]) -> Optional[str]:
    if not cs:
        return None
    label = (cs.get("status_name") or cs.get("status") or "").strip()
    return label.title() or None if label else None


_PUBLIC_KEYS = (
    "common_name", "scientific_name", "descriptor", "extract",
    "wiki_url", "family", "order", "conservation",
)


def _public(row: dict) -> dict:
    out = {k: row.get(k) for k in _PUBLIC_KEYS}
    out["ok"] = bool(row.get("ok"))
    return out
