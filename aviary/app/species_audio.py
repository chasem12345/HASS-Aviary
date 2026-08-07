"""Reference recordings per species, from xeno-canto with an iNaturalist fallback.

BirdNET-Go gives you the audio it classified; this gives you a *known* recording of the
same species to compare it against — the quickest way to judge whether a classification
is real before deciding to blacklist it.

Two providers, tried in order per variant:

* **xeno-canto** (needs a free API key in the ``xeno_canto_api_key`` option) — a curated
  archive where every recording carries a quality rating, a sound type and a list of
  other species audible in the clip. That metadata is the whole point: filtering to
  quality A and preferring recordings with nothing else audible is what keeps barking
  dogs and background birds out. Queried once for the ``song`` and once for the ``call``,
  so the species page can offer both.
* **iNaturalist** research-grade observation sounds — the fallback, used when there is no
  key, no scientific name, or nothing acceptable on xeno-canto. These are incidental
  recordings of a sighting with no quality metadata at all, so they are stored as the
  untyped ``any`` variant.

Only recordings under a reusable licence are considered, and the licence is re-checked on
each result rather than trusted to the upstream filter. Attribution is stored and must
always be displayed — a condition of every CC licence involved.

Results are cached in SQLite (``species_audio``, one row per species+kind) with the same
TTLs as ``species_info``, so a species hits the APIs at most once a month. All failures
are soft — the species page just omits what it couldn't resolve.

The audio file itself is never stored: ``routes/media.py`` streams it from the provider on
demand through the existing Range-aware proxy, matching how every other media asset in
Aviary is handled.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

from . import db, species_info
from .settings import Settings

log = logging.getLogger("aviary.species_audio")

_TTL_OK = 30 * 86400      # refresh a good hit monthly
_TTL_FAIL = 3 * 86400     # retry misses in a few days, not every page load

_INAT_OBSERVATIONS = "https://api.inaturalist.org/v1/observations"
_OBSERVATION_PAGE = "https://www.inaturalist.org/observations/{}"

_XC_RECORDINGS = "https://xeno-canto.org/api/3/recordings"

# Variants offered on the species page. 'song' and 'call' come from xeno-canto's `type`
# tag; 'any' is the untyped iNaturalist fallback, which is what every species used to get.
KIND_SONG = "song"
KIND_CALL = "call"
KIND_ANY = "any"
KINDS = (KIND_SONG, KIND_CALL, KIND_ANY)

# Quality ratings tried in order. xeno-canto rates A (best) to E; anything below B tends
# to be exactly the distant, noisy audio this whole module exists to avoid.
_XC_QUALITIES = ("A", "B")

# Clip length bounds, seconds. Very short clips are usually a fragment, very long ones are
# soundscapes where the target bird is one voice among many.
_XC_LEN_MIN = 3
_XC_LEN_MAX = 30

# Licences we're willing to re-serve. Attribution is rendered for all of them (a
# condition of every CC-BY variant), so the only ones excluded are those that reserve
# all rights or forbid redistribution. Passed as iNaturalist's `sound_license` filter
# *and* re-checked on each result, so a typo in the parameter name can't silently let
# all-rights-reserved audio through.
_LICENSES = ("cc0", "cc-by", "cc-by-nc", "cc-by-sa", "cc-by-nc-sa")

_client: Optional[httpx.AsyncClient] = None
_settings: Optional[Settings] = None


def configure(settings: Settings) -> None:
    global _settings
    _settings = settings
    if not settings.xeno_canto_api_key:
        log.info(
            "No xeno_canto_api_key set: reference recordings will come from iNaturalist "
            "observations. A free key from https://xeno-canto.org/account enables the "
            "curated song/call recordings."
        )


def init_client() -> None:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0),
            follow_redirects=True,
            # Same descriptive User-Agent as species_info; iNaturalist asks that API
            # clients identify themselves, and xeno-canto sits behind bot protection.
            headers={"User-Agent": species_info.USER_AGENT, "Accept": "application/json"},
        )


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def resolve(common_name: str, scientific_name: Optional[str] = None,
                  kind: str = KIND_SONG) -> dict:
    """Return cached reference-audio metadata for one variant, fetching on a miss."""
    common_name = (common_name or "").strip()
    if not common_name or common_name.lower() == "bird" or kind not in KINDS:
        return _public({"common_name": common_name, "kind": kind, "ok": 0})

    cached = db.get_species_audio(common_name, kind)
    if cached and _fresh(cached):
        return _public(cached)

    row = await _fetch(common_name, (scientific_name or "").strip() or None, kind)
    try:
        db.put_species_audio(row)
    except Exception:  # noqa: BLE001 - caching is best-effort
        log.exception("Failed to cache species_audio for %s (%s)", common_name, kind)
    return _public(row)


async def resolve_all(common_name: str, scientific_name: Optional[str] = None) -> dict:
    """Every variant that resolved, as ``{kind: info}``.

    xeno-canto's typed variants are preferred; the untyped iNaturalist fallback is only
    attempted when neither a song nor a call could be found, so a species with good
    xeno-canto coverage never also pays for an iNaturalist round trip.
    """
    variants = {}
    for kind in (KIND_SONG, KIND_CALL):
        info = await resolve(common_name, scientific_name, kind)
        if info["ok"]:
            variants[kind] = info
    if not variants:
        info = await resolve(common_name, scientific_name, KIND_ANY)
        if info["ok"]:
            variants[KIND_ANY] = info
    return variants


async def file_url(common_name: str, scientific_name: Optional[str] = None,
                   kind: str = KIND_SONG) -> Optional[str]:
    """The upstream audio URL for one variant, or None. Used by the media proxy."""
    info = await resolve(common_name, scientific_name, kind)
    return info.get("file_url") if info.get("ok") else None


def _fresh(row: dict) -> bool:
    age = time.time() - (row.get("fetched_at") or 0)
    return age < (_TTL_OK if row.get("ok") else _TTL_FAIL)


def _blank(common: str, kind: str) -> dict:
    return {
        "common_name": common, "kind": kind, "provider": None, "taxon_id": None,
        "sound_id": None, "source_url": None, "file_url": None, "content_type": None,
        "license_code": None, "attribution": None, "quality": None,
        "fetched_at": time.time(), "ok": 0,
    }


async def _fetch(common: str, sci: Optional[str], kind: str) -> dict:
    row = _blank(common, kind)
    if _client is None:
        return row

    if kind in (KIND_SONG, KIND_CALL):
        # xeno-canto is queried scientifically. Without a scientific name the only option
        # would be a fuzzy common-name match, which is precisely how you end up playing
        # the wrong bird — so skip rather than guess.
        if not (_settings and _settings.xeno_canto_api_key and sci):
            return row
        return await _fetch_xc(row, sci, kind)
    return await _fetch_inat(row, common, sci)


# ------------------------------------------------------------------------- xeno-canto

async def _fetch_xc(row: dict, sci: str, kind: str) -> dict:
    """Best xeno-canto recording of ``kind`` for a species, trying each quality in turn."""
    for quality in _XC_QUALITIES:
        recordings = await _xc_search(sci, kind, quality)
        best = _xc_best(recordings)
        if not best:
            continue
        rec_id = str(best.get("id") or "")
        row.update({
            "provider": "xeno-canto",
            "sound_id": rec_id or None,
            "source_url": (best.get("url") or "").strip() or None,
            "file_url": best.get("file"),
            "content_type": None,  # XC doesn't declare one; the proxy relays what it gets
            "license_code": _xc_license_code(best.get("lic")),
            "attribution": _xc_attribution(best, rec_id),
            "quality": (best.get("q") or "").strip().upper() or None,
            "ok": 1,
        })
        return row
    return row


async def _xc_search(sci: str, kind: str, quality: str) -> list[dict]:
    """Query the xeno-canto API. Any failure is a miss, never an exception.

    The key is passed as a query parameter, so nothing here may log the params dict.
    """
    query = (
        f'sp:"{sci}" grp:birds type:{kind} q:{quality} '
        f"len:{_XC_LEN_MIN}-{_XC_LEN_MAX}"
    )
    try:
        resp = await _client.get(
            _XC_RECORDINGS,
            params={
                "query": query,
                "key": _settings.xeno_canto_api_key,
                "per_page": 50,
            },
        )
        if resp.status_code != 200:
            log.debug("xeno-canto returned %s for %r (%s, q:%s)",
                      resp.status_code, sci, kind, quality)
            return []
        # xeno-canto sits behind bot protection that answers with an HTML interstitial
        # rather than JSON; treat anything unparseable as "no recordings" so the ladder
        # falls through to iNaturalist instead of breaking the page.
        return (resp.json() or {}).get("recordings") or []
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("xeno-canto lookup failed for %r (%s, q:%s): %s", sci, kind, quality, exc)
        return []


def _xc_best(recordings: list[dict]) -> Optional[dict]:
    """Pick the cleanest usable recording.

    The server-side filter only narrows the pool; this is what does the real work. A
    recording with an empty ``also`` has no other species audible in it, which is the
    single best predictor of a clip that sounds like one bird and nothing else.
    """
    usable = [
        r for r in recordings
        if r.get("file") and _xc_license_code(r.get("lic")) in _LICENSES
    ]
    if not usable:
        return None
    return min(usable, key=_xc_rank)


def _xc_rank(rec: dict) -> tuple:
    """Sort key, lower is better: clean clips first, then quality, then a middling length."""
    also = [s for s in (rec.get("also") or []) if str(s).strip()]
    quality = (rec.get("q") or "").strip().upper()
    # Mid-length clips are the most listenable: long enough to recognise, short enough
    # to be a single vocalisation rather than a soundscape.
    ideal = (_XC_LEN_MIN + _XC_LEN_MAX) / 2
    return (len(also), _XC_QUALITIES.index(quality) if quality in _XC_QUALITIES else 99,
            abs(_xc_seconds(rec) - ideal))


def _xc_seconds(rec: dict) -> float:
    """Clip length in seconds. ``length`` is "m:ss"; ``length_sec`` may not be present."""
    raw = rec.get("length_sec")
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    parts = str(rec.get("length") or "").split(":")
    try:
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + float(part)
        return seconds
    except (TypeError, ValueError):
        return 0.0


def _xc_license_code(lic: Optional[str]) -> Optional[str]:
    """Normalise xeno-canto's licence URL to the codes used in ``_LICENSES``.

    ``lic`` looks like ``//creativecommons.org/licenses/by-nc-sa/4.0/``; the fragment
    between "licenses" and the version is the code, with public-domain dedications
    spelled "publicdomain/zero".
    """
    text = (lic or "").strip().lower()
    if not text:
        return None
    if "publicdomain" in text or "/zero/" in text:
        return "cc0"
    parts = [p for p in text.split("/") if p]
    try:
        code = parts[parts.index("licenses") + 1]
    except (ValueError, IndexError):
        return None
    return f"cc-{code}" if code else None


def _xc_attribution(rec: dict, rec_id: str) -> Optional[str]:
    """Credit line: recordist, catalogue number and licence.

    iNaturalist hands us a ready-made attribution string; xeno-canto doesn't, so build
    the equivalent here. The UI hides the card entirely when this comes back empty.
    """
    recordist = (rec.get("rec") or "").strip()
    if not recordist:
        return None
    bits = [recordist, f"XC{rec_id}" if rec_id else "", "xeno-canto"]
    code = _xc_license_code(rec.get("lic"))
    if code:
        bits.append(code.upper())
    return " · ".join(b for b in bits if b)


# ------------------------------------------------------------------------ iNaturalist

async def _fetch_inat(row: dict, common: str, sci: Optional[str]) -> dict:
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
        "provider": "inaturalist",
        "sound_id": str(sound.get("id")) if sound.get("id") is not None else None,
        "source_url": _OBSERVATION_PAGE.format(observation_id) if observation_id else None,
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
    return {
        "ok": bool(row.get("ok")),
        "common_name": row.get("common_name"),
        "kind": row.get("kind"),
        "provider": row.get("provider"),
        "file_url": row.get("file_url"),
        "content_type": row.get("content_type"),
        "license_code": row.get("license_code"),
        "attribution": row.get("attribution"),
        "quality": row.get("quality"),
        "source_url": row.get("source_url"),
    }
