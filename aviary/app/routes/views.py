"""HTML pages: dashboard, recent detections, species detail."""

from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from .. import db
from . import THEMES, get_theme, ingress_url, render

router = APIRouter()

PAGE_SIZE = 48

_RANGE_SECONDS = {"today": None, "7d": 7 * 86400, "30d": 30 * 86400, "all": None}
# Fallback day-span per range for chart endpoints (per-day chart x-axis width).
_RANGE_DAYS = {"today": 1, "7d": 7, "30d": 30, "all": 3650}


def _norm_range(range_key: str, default: str = "7d") -> str:
    return range_key if range_key in _RANGE_SECONDS else default


def _since(range_key: str) -> Optional[float]:
    if range_key == "today":
        # Local midnight, not a rolling 24h window.
        return time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    secs = _RANGE_SECONDS.get(range_key)
    return time.time() - secs if secs else None


def _norm_source(source: Optional[str]) -> Optional[str]:
    return source if source in ("frigate", "birdnet") else None


def _day_groups(detections: list[dict]) -> list[dict]:
    """Group newest-first detections into contiguous local-day buckets."""
    today = date.today()
    yesterday = today - timedelta(days=1)
    groups: list[dict] = []
    for det in detections:
        try:
            day = date.fromtimestamp(float(det["start_time"]))
        except (TypeError, ValueError, OSError, OverflowError):
            day = today
        if day == today:
            label = "Today"
        elif day == yesterday:
            label = "Yesterday"
        else:
            label = f"{day.strftime('%B')} {day.day}"
            if day.year != today.year:
                label += f", {day.year}"
        if not groups or groups[-1]["label"] != label:
            groups.append({"label": label, "items": []})
        groups[-1]["items"].append(det)
    return groups


def _paged(
    source: Optional[str],
    species: Optional[str],
    before: Optional[float],
    since: Optional[float],
    unidentified: bool = False,
) -> tuple[list[dict], Optional[float]]:
    """One page of detections plus the next ``before`` cursor (None = no more)."""
    if unidentified:
        # The review queue ignores the source/species/range filters on purpose: every row
        # in it is a Frigate row with no species, so those filters would either match
        # everything or nothing.
        rows = db.unidentified_detections(limit=PAGE_SIZE + 1, before=before)
    else:
        rows = db.recent_detections(
            limit=PAGE_SIZE + 1, source=source, species=species, before=before, since=since
        )
    has_more = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    next_before = rows[-1]["start_time"] if has_more and rows else None
    return rows, next_before


# ------------------------------------------------------------------------------- pages

@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    source: Optional[str] = Query(None),
    range_key: str = Query("7d", alias="range"),
):
    src = _norm_source(source)
    range_key = _norm_range(range_key)
    since = _since(range_key)
    gated = request.app.state.settings.require_species_confirmation

    leaders = db.top_species(limit=10, source=src, since=since, only_confirmed=gated)
    latest = db.recent_detections(limit=1, source=src)
    ingestor = getattr(request.app.state, "ingestor", None)

    ctx = {
        "request": request,
        "page": "dashboard",
        "source": src or "all",
        "range": range_key,
        # Charts use the same boundary as the stat cards ("today" = local midnight).
        "chart_since": since or "",
        "chart_days": _RANGE_DAYS[range_key],
        "stats": db.summary_stats(source=src, since=since, only_confirmed=gated),
        "new_species": db.new_species_count(source=src, since=since, only_confirmed=gated),
        # Review queue size. Always the whole queue, not the filtered window — it's a
        # to-do count, not a statistic.
        "unconfirmed": db.unconfirmed_count() if gated else 0,
        "leaders": leaders,
        "thumbs": db.latest_snapshot_refs([s["common_name"] for s in leaders]),
        "latest": latest[0] if latest else None,
        "mqtt_enabled": request.app.state.settings.mqtt_enabled,
        "mqtt_connected": bool(ingestor and ingestor.connected),
    }
    return render("dashboard.html", ctx)


def _recent_ctx(
    request: Request,
    source: Optional[str],
    species: Optional[str],
    range_key: str,
    before: Optional[float],
    state: Optional[str] = None,
) -> dict:
    src = _norm_source(source)
    range_key = _norm_range(range_key, default="all")
    since = _since(range_key)
    # ?state=unidentified is the identification review queue. Like the species review
    # queue, it only means anything while the feature is on; with it off the parameter is
    # ignored rather than showing a permanently empty page.
    identifying = request.app.state.settings.identify_active
    reviewing = identifying and state == "unidentified"
    detections, next_before = _paged(src, species, before, since, unidentified=reviewing)
    older_url = None
    if next_before is not None:
        q: dict = {"before": f"{next_before:.6f}"}
        if reviewing:
            q["state"] = "unidentified"
        else:
            if src:
                q["source"] = src
            if species:
                q["species"] = species
            if range_key != "all":
                q["range"] = range_key
        older_url = f"{ingress_url(request, 'recent')}?{urlencode(q)}"
    return {
        "request": request,
        "page": "recent",
        "source": src or "all",
        "species": species,
        "range": range_key,
        "reviewing_ids": reviewing,
        "identifying": identifying,
        # A to-do count, so always the whole queue rather than the filtered window.
        "unidentified": db.unidentified_count() if identifying else 0,
        "groups": _day_groups(detections),
        "next_before": next_before,
        "older_url": older_url,
        "paged": before is not None,
        "species_options": db.distinct_species(),
        "newest": detections[0]["start_time"] if detections else 0,
    }


@router.get("/recent", response_class=HTMLResponse)
def recent(
    request: Request,
    source: Optional[str] = Query(None),
    species: Optional[str] = Query(None),
    range_key: str = Query("all", alias="range"),
    before: Optional[float] = Query(None),
    state: Optional[str] = Query(None),
):
    ctx = _recent_ctx(request, source, species, range_key, before, state)
    return render("recent.html", ctx)


@router.get("/recent/partial", response_class=HTMLResponse)
def recent_partial(
    request: Request,
    source: Optional[str] = Query(None),
    species: Optional[str] = Query(None),
    range_key: str = Query("all", alias="range"),
    before: Optional[float] = Query(None),
    highlight_after: Optional[float] = Query(None),
    state: Optional[str] = Query(None),
):
    """Server-rendered detection groups for the Recent page's live refresh."""
    ctx = _recent_ctx(request, source, species, range_key, before, state)
    ctx["highlight_after"] = highlight_after
    return render("_groups.html", ctx)


@router.get("/species", response_class=HTMLResponse)
def species_index(
    request: Request,
    source: Optional[str] = Query(None),
    range_key: str = Query("all", alias="range"),
    new: int = Query(0),
    state: Optional[str] = Query(None),
):
    src = _norm_source(source)
    range_key = _norm_range(range_key, default="all")
    since = _since(range_key)
    only_new = bool(new)
    gated = request.app.state.settings.require_species_confirmation
    # ?state=unconfirmed is the review queue. It only means anything while the gate is on;
    # with it off there is no queue, so the parameter is ignored rather than showing an
    # empty page.
    reviewing = gated and state == "unconfirmed"
    species = db.species_list(
        source=src, since=since, only_new=only_new,
        only_confirmed=gated and not reviewing,
        only_unconfirmed=reviewing,
    )
    # Registry framing for the Pokedex theme. The number is attached to each row rather
    # than reordering here, because the default theme's ordering (count DESC) must not
    # change — the dex template sorts by dex_no itself.
    dex = db.species_dex_numbers(only_confirmed=gated)
    for s in species:
        s["dex_no"] = dex.get(s["common_name"], 0)
    ctx = {
        "request": request,
        "page": "species",
        "source": src or "all",
        "range": range_key,
        "only_new": only_new,
        "reviewing": reviewing,
        "gated": gated,
        "species": species,
        "since": since,
        "thumbs": db.latest_snapshot_refs([s["common_name"] for s in species]),
        "registry": db.registry_stats(only_confirmed=gated),
    }
    return render("species_index.html", ctx)


@router.get("/species/{name}", response_class=HTMLResponse)
def species_detail(
    request: Request,
    name: str,
    source: Optional[str] = Query(None),
    before: Optional[float] = Query(None),
):
    stats = db.species_stats(name)
    # Only offer a video/audio filter when the species has both; default to video.
    has_both = bool(stats.get("frigate_total")) and bool(stats.get("birdnet_total"))
    sel = "all"
    if has_both:
        raw = source if source in ("frigate", "birdnet", "all") else None
        sel = raw or "frigate"
    src = _norm_source(sel)  # 'all' -> None (both streams)

    detections, next_before = _paged(src, name, before, None)
    older_url = None
    if next_before is not None:
        base = ingress_url(request, "species_detail", name=name)
        q: dict = {"before": f"{next_before:.6f}"}
        if has_both and sel != "all":
            q["source"] = sel
        older_url = f"{base}?{urlencode(q)}"
    gated = request.app.state.settings.require_species_confirmation
    ctx = {
        "request": request,
        "page": "species",
        "species": name,
        "stats": stats,
        "source": sel,
        "has_both": has_both,
        # Drives the review banner. With the gate off nothing is pending, so no banner.
        "gated": gated,
        "confirmed": (not gated) or db.is_species_confirmed(name),
        "thumb": db.latest_snapshot_refs([name]).get(name),
        "groups": _day_groups(detections),
        "next_before": next_before,
        "older_url": older_url,
        "paged": before is not None,
    }
    ctx.update(_registry_position(name, only_confirmed=gated))
    return render("species.html", ctx)


def _registry_position(name: str, only_confirmed: bool = False) -> dict:
    """This species' registry number plus its neighbours, for dex prev/next stepping.

    Neighbours traverse the whole registry in first-detection order, not whatever filter
    the user was browsing. Either side is None at the ends, and all three are None for a
    species with no detections (e.g. an old link to something since removed) — which is
    also where an unconfirmed species lands, so the entry renders as ``No.???`` until it
    is approved.
    """
    numbers = db.species_dex_numbers(only_confirmed=only_confirmed)
    ordered = sorted(numbers, key=lambda n: (numbers[n], n))
    try:
        idx = ordered.index(name)
    except ValueError:
        return {"dex_no": None, "dex_prev": None, "dex_next": None}

    def entry(i: int) -> Optional[dict]:
        if not 0 <= i < len(ordered):
            return None
        return {"common_name": ordered[i], "dex_no": numbers[ordered[i]]}

    return {"dex_no": numbers[name], "dex_prev": entry(idx - 1), "dex_next": entry(idx + 1)}


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    """Runtime preferences: UI theme and the species blacklist.

    Everything here is stored in the add-on's database and applies immediately — unlike
    the add-on options, which need a restart.
    """
    settings = request.app.state.settings
    ctx = {
        "request": request,
        "page": "settings",
        "themes": THEMES,
        "current_theme": get_theme(),
        "blacklist": db.blacklist_entries(),
        "identify_configured": settings.identify_active,
        # The URL is shown so a misconfigured host is obvious at a glance. The token is
        # deliberately never exposed here — it is a credential.
        "identify_url": settings.identify_url,
    }
    return render("settings.html", ctx)
