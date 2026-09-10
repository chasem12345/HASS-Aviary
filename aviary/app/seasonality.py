"""When a species is around: regional presence by month, from iNaturalist.

The species page's Seasonality card answers "is this bird here in winter?" with data
rather than a range map. iNaturalist's observation histogram (``/v1/observations/histogram``
with ``interval=month_of_year``) counts research-grade observations of a taxon within a
radius of a point, by the month they were observed — a good proxy for presence, free of
any API key, and Aviary already knows the taxon id (``species_info``) and the location
(``solar``, from Home Assistant). One request per species, cached for a month; nothing
here is fetched at ingest time.

Month counts are skewed toward migration peaks (people photograph arrivals), so the
label derives *presence* from a low share of the peak month plus a small absolute floor,
not from the shape of the curve. The AVONET migration class shown beside it is
whole-species and comes from the bundled table (``traits``), not from here.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Optional

import httpx

from . import db, solar, species_info

log = logging.getLogger("aviary.seasonality")

_HISTOGRAM = "https://api.inaturalist.org/v1/observations/histogram"
# A regional circle: big enough to smooth out a thin county, small enough that a coastal
# species does not appear inland. Documented in DOCS.md; an option if anyone asks.
RADIUS_KM = 150.0
_TTL_OK = 30 * 86400
_TTL_FAIL = 3 * 86400
# A cached curve is for the location it was fetched at; a move this far refetches.
_MOVE_KM = 25.0

# Presence rule (see module docstring).
MIN_TOTAL = 30          # below this the region simply has too few records to say
PRESENT_SHARE = 0.04    # of the peak month
PRESENT_MIN = 3         # observations

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_client: Optional[httpx.AsyncClient] = None


def init_client() -> None:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(12.0), follow_redirects=True,
            headers={"User-Agent": species_info.USER_AGENT, "Accept": "application/json"},
        )


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# ----------------------------------------------------------------------- labels

def _runs(present: list[bool]) -> list[tuple[int, int]]:
    """Contiguous runs of present months as (start, end) indexes, circular over December
    → January, in calendar order of their start."""
    n = len(present)
    if all(present):
        return [(0, n - 1)]
    if not any(present):
        return []
    # Start scanning just after an absent month so a run crossing December is one run.
    first_absent = next(i for i in range(n) if not present[i])
    runs: list[tuple[int, int]] = []
    start: Optional[int] = None
    for k in range(1, n + 1):
        i = (first_absent + k) % n
        if present[i] and start is None:
            start = i
        elif not present[i] and start is not None:
            runs.append((start, (i - 1) % n))
            start = None
    if start is not None:
        runs.append((start, first_absent - 1 if first_absent > 0 else n - 1))
    runs.sort(key=lambda r: r[0])
    return runs


def _range_label(run: tuple[int, int]) -> str:
    a, b = run
    return MONTHS[a] if a == b else f"{MONTHS[a]}–{MONTHS[b]}"


def season_label(months: list[int]) -> dict:
    """Plain-English seasonality from 12 monthly counts (January first).

    Returns ``kind`` (resident | summer | winter | passage | seasonal | sparse), ``label``,
    ``present`` (12 booleans), ``peak_month`` (1-12, or None) and ``total``.
    """
    counts = [max(0, int(v or 0)) for v in (months or [])][:12]
    counts += [0] * (12 - len(counts))
    total = sum(counts)
    peak = max(counts)
    peak_month = counts.index(peak) + 1 if peak else None
    if total < MIN_TOTAL or not peak:
        return {"kind": "sparse", "label": "Too few regional records to say",
                "present": [False] * 12, "peak_month": peak_month, "total": total}
    floor = max(PRESENT_MIN, PRESENT_SHARE * peak)
    present = [c >= floor for c in counts]
    runs = _runs(present)
    n_present = sum(present)
    if n_present >= 11:
        return {"kind": "resident", "label": "Year-round resident", "present": present,
                "peak_month": peak_month, "total": total}
    if len(runs) == 1:
        (a, b), = runs
        span = {(a + k) % 12 for k in range(((b - a) % 12) + 1)}
        rng = _range_label((a, b))
        if 5 in span or 6 in span:          # June / July
            return {"kind": "summer", "label": f"Summer visitor · {rng}", "present": present,
                    "peak_month": peak_month, "total": total}
        if 11 in span or 0 in span:         # December / January
            return {"kind": "winter", "label": f"Winter visitor · {rng}", "present": present,
                    "peak_month": peak_month, "total": total}
        return {"kind": "seasonal", "label": f"Seasonal · {rng}", "present": present,
                "peak_month": peak_month, "total": total}
    ranges = " & ".join(_range_label(r) for r in runs)
    if len(runs) == 2:
        return {"kind": "passage", "label": f"Passage migrant · {ranges}", "present": present,
                "peak_month": peak_month, "total": total}
    return {"kind": "seasonal", "label": f"Seasonal · {ranges}", "present": present,
            "peak_month": peak_month, "total": total}


# ------------------------------------------------------------------------ cache

def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _fresh(row: dict, lat: float, lon: float) -> bool:
    """A cached row still stands if its TTL holds and it was fetched near here."""
    age = time.time() - float(row.get("fetched_at") or 0)
    if age >= (_TTL_OK if row.get("ok") else _TTL_FAIL):
        return False
    if row.get("lat") is None or row.get("lon") is None:
        return False
    return _km(float(row["lat"]), float(row["lon"]), lat, lon) <= _MOVE_KM


def _public(row: dict) -> Optional[dict]:
    if not row.get("ok") or not row.get("months"):
        return None
    out = season_label(list(row["months"]))
    out.update({"months": list(row["months"]), "radius_km": int(row.get("radius_km") or RADIUS_KM)})
    return out


async def _fetch_months(taxon_id: int, lat: float, lon: float) -> Optional[list[int]]:
    if _client is None:
        return None
    try:
        resp = await _client.get(_HISTOGRAM, params={
            "taxon_id": taxon_id, "lat": f"{lat:.4f}", "lng": f"{lon:.4f}",
            "radius": int(RADIUS_KM), "interval": "month_of_year", "date_field": "observed",
            "verifiable": "true", "quality_grade": "research",
        })
        if resp.status_code != 200:
            log.info("iNaturalist histogram returned %s for taxon %s", resp.status_code, taxon_id)
            return None
        data = ((resp.json() or {}).get("results") or {}).get("month_of_year") or {}
        return [int(data.get(str(m)) or 0) for m in range(1, 13)]
    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
        log.info("iNaturalist histogram failed for taxon %s: %s", taxon_id, exc)
        return None


async def resolve(common_name: str, scientific_name: Optional[str] = None) -> Optional[dict]:
    """Regional presence for a species near the Home Assistant location, or None when
    there is no location, no taxon, or the request failed (a failure is cached briefly)."""
    loc = solar.location()
    if loc is None:
        return None
    cached = db.get_species_season(common_name)
    if cached and _fresh(cached, loc.lat, loc.lon):
        return _public(cached)
    row = {"common_name": common_name, "taxon_id": None, "lat": loc.lat, "lon": loc.lon,
           "radius_km": RADIUS_KM, "months": None, "total": None,
           "fetched_at": time.time(), "ok": 0}
    taxon = await species_info.taxon_id(common_name, scientific_name)
    if taxon:
        row["taxon_id"] = taxon
        months = await _fetch_months(taxon, loc.lat, loc.lon)
        if months is not None:
            row.update({"months": months, "total": sum(months), "ok": 1})
    db.put_species_season(row)
    return _public(row)
