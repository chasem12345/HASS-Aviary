"""Per-species reference info: a Wikipedia blurb plus iNaturalist taxonomy.

Results are cached in SQLite (``species_info`` table) and refreshed monthly, so each
species hits the external APIs at most once per TTL. Both sources are free and need no
key; we send a descriptive User-Agent per Wikimedia's policy. All failures are soft —
the species page just omits the About card.
"""

from __future__ import annotations

import json
import logging
import re
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
# The article body as plain text with "== Heading ==" markers, for the About card's
# expandable sections. The REST summary above is the lead paragraph only, which for many
# species is two sentences.
_WIKI_EXTRACT = "https://en.wikipedia.org/w/api.php"
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
        "inat_taxon_id": None, "sections": None,
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
                # The rest of the article, by section, from the same title.
                sections = await _wiki_sections(wiki.get("title") or title)
                row["sections"] = json.dumps(sections) if sections else None
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
            # The resolved article title (after redirects), for the section fetch.
            "title": d.get("title"),
        }
    except (httpx.HTTPError, ValueError):
        return None


# Article sections worth showing, in display order, with the heading spellings Wikipedia
# uses for them. The first heading matching an alias wins; missing ones are skipped.
_SECTIONS = (
    ("Description", ("description", "identification", "appearance")),
    ("Voice", ("vocalization", "vocalizations", "vocalisation", "vocalisations", "voice",
               "song", "calls", "song and calls", "vocal behavior")),
    ("Range & habitat", ("distribution and habitat", "distribution", "range", "habitat",
                         "habitat and distribution", "range and habitat")),
    ("Diet", ("diet", "feeding", "food", "food and feeding", "diet and feeding", "foraging")),
    ("Behaviour", ("behavior", "behaviour", "behavior and ecology", "behaviour and ecology",
                   "ecology", "ecology and behavior", "ecology and behaviour")),
    ("Breeding", ("breeding", "reproduction", "nesting", "breeding and nesting")),
    ("Migration", ("migration", "movements", "migration and movements")),
)
_SECTION_MAX_CHARS = 520
_SECTION_MAX_SENTENCES = 3
_HEADING_RE = re.compile(r"^={2,3}\s*(.+?)\s*={2,3}\s*$", re.M)
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'])")


def _trim(text: str) -> str:
    """The first few sentences of a section, cut at a sentence boundary."""
    text = " ".join(text.split())
    if not text:
        return ""
    sentences = _SENTENCE_END_RE.split(text)
    out = ""
    for sent in sentences[:_SECTION_MAX_SENTENCES]:
        candidate = (out + " " + sent).strip()
        if out and len(candidate) > _SECTION_MAX_CHARS:
            break
        out = candidate
    if len(out) > _SECTION_MAX_CHARS:
        out = out[:_SECTION_MAX_CHARS].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return out


def parse_sections(extract: str) -> list[dict]:
    """[{"title", "text"}] for the article sections the About card shows.

    Only level-2/3 headings are considered (``== Heading ==``); a level-3 sub-heading's
    text is folded into its parent when the parent matched, so "Behavior / Feeding" ends
    up under Behaviour when there is no Diet section of its own.
    """
    if not extract:
        return []
    matches = list(_HEADING_RE.finditer(extract))
    chunks: dict[str, str] = {}
    order: list[str] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(extract)
        heading = m.group(1).strip().lower()
        body = extract[start:end].strip()
        if not body:
            continue
        if heading not in chunks:
            chunks[heading] = body
            order.append(heading)
        else:
            chunks[heading] += "\n" + body
    out = []
    for title, aliases in _SECTIONS:
        for alias in aliases:
            if alias in chunks:
                text = _trim(chunks[alias])
                if text:
                    out.append({"title": title, "text": text})
                break
    return out


async def _wiki_sections(title: str) -> list[dict]:
    """Plain-text sections of the species article, trimmed for the card. [] on failure."""
    if _client is None or not title:
        return []
    try:
        resp = await _client.get(_WIKI_EXTRACT, params={
            "action": "query", "prop": "extracts", "explaintext": 1,
            "exsectionformat": "wiki", "redirects": 1, "format": "json",
            "formatversion": 2, "titles": title,
        })
        if resp.status_code != 200:
            return []
        pages = ((resp.json() or {}).get("query") or {}).get("pages") or []
        extract = (pages[0] or {}).get("extract") if pages else None
        return parse_sections(extract or "")
    except (httpx.HTTPError, ValueError, IndexError, TypeError):
        return []


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
    # Stored as JSON text; the page wants the list. Never let a bad value 500 the card.
    raw = row.get("sections")
    try:
        parsed = json.loads(raw) if isinstance(raw, str) and raw else (raw or [])
    except ValueError:
        parsed = []
    out["sections"] = parsed if isinstance(parsed, list) else []
    return out
