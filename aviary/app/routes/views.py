"""HTML pages: dashboard, recent detections, species detail."""

from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db, visits
# The helper, not the module: this file's /kept route is itself named `kept`.
from ..kept import view_pad
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


def _hydrate(request: Request, items: list[dict]) -> list[dict]:
    """Turn the feed's raw visit rows into card view-models; detections pass through.

    Each visit member is annotated with every species present in its event — the tracked
    bird plus any other bird the identifier found and named — so the visit's species
    strip lists a cardinal AND the wren beside it even when the wren never had a Frigate
    event of its own. One query for the whole page.
    """
    pad = view_pad(request.app.state.settings)
    now = time.time()
    members = [m for it in items if it.get("kind") == "visit" for m in it.get("members") or []]
    present = db.species_present([m["id"] for m in members]) if members else {}
    for m in members:
        m["present"] = present.get(m["id"], [])
    return [visits.card_model(it, pad, now) if it.get("kind") == "visit" else it
            for it in items]


def _newest_in(items: list[dict]) -> float:
    """The newest detection start among feed items — inside visits too, because the
    live-refresh marker compares against detections, not visit starts."""
    newest = 0.0
    for it in items:
        newest = max(newest, float(it.get("start_time") or 0.0))
        for m in it.get("members") or []:
            newest = max(newest, float(m.get("start_time") or 0.0))
    return newest


def _feed_page(
    request: Request,
    source: Optional[str],
    species: Optional[str],
    before: Optional[float],
    since: Optional[float],
    zone: Optional[str] = None,
) -> tuple[list[dict], Optional[float]]:
    """One page of feed items (visits + ungrouped detections) plus the next ``before``
    cursor (None = no more). Visits arrive as card view-models."""
    items = db.feed_page(
        limit=PAGE_SIZE + 1, source=source, species=species, before=before, since=since,
        zone=zone,
    )
    has_more = len(items) > PAGE_SIZE
    items = items[:PAGE_SIZE]
    next_before = items[-1]["start_time"] if has_more and items else None
    return _hydrate(request, items), next_before


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
    latest = _hydrate(request, db.feed_page(limit=1, source=src))
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
    zone: Optional[str] = None,
) -> dict:
    src = _norm_source(source)
    range_key = _norm_range(range_key, default="all")
    since = _since(range_key)
    detections, next_before = _feed_page(request, src, species, before, since, zone)
    older_url = None
    if next_before is not None:
        q: dict = {"before": f"{next_before:.6f}"}
        if src:
            q["source"] = src
        if species:
            q["species"] = species
        if zone:
            q["zone"] = zone
        if range_key != "all":
            q["range"] = range_key
        older_url = f"{ingress_url(request, 'recent')}?{urlencode(q)}"
    return {
        "request": request,
        "page": "recent",
        "source": src or "all",
        "species": species,
        "zone": zone,
        "range": range_key,
        "groups": _day_groups(detections),
        "next_before": next_before,
        "older_url": older_url,
        "paged": before is not None,
        "species_options": db.distinct_species(),
        # Only offered when zones actually exist — a zoneless setup keeps a clean bar.
        "zone_options": db.distinct_zones(),
        "newest": _newest_in(detections),
    }


_RECAP_RANGES = ("day", "week", "month", "year", "custom")


def _midnight(d: date) -> float:
    """Local midnight, the same boundary as the dashboard's "today" stats (_since)."""
    return time.mktime((d.year, d.month, d.day, 0, 0, 0, 0, 0, -1))


def _parse_date(value: Optional[str], default: date) -> date:
    """YYYY-MM-DD or the default — a hand-edited URL should degrade, not 400."""
    try:
        return date.fromisoformat(value) if value else default
    except (TypeError, ValueError):
        return default


def _month_day(d: date) -> str:
    return f"{d:%b} {d.day}"


def _recap_window(rng: str, anchor: date, from_: Optional[str], to: Optional[str],
                  today: date) -> dict:
    """The [start, end) date window for a recap range, plus its prev/next anchors.

    A window that reaches today is cut off at tomorrow's midnight, which is what makes
    "month" read as month-to-date and "year" as year-to-date while they are current;
    past months and years are whole. ``next`` is None once it would start after today.
    """
    tomorrow = today + timedelta(days=1)
    if rng == "week":
        start = anchor - timedelta(days=anchor.weekday())  # Monday
        full_end = start + timedelta(days=7)
        prev, nxt = start - timedelta(days=7), full_end
    elif rng == "month":
        start = anchor.replace(day=1)
        full_end = (start + timedelta(days=32)).replace(day=1)
        prev, nxt = (start - timedelta(days=1)).replace(day=1), full_end
    elif rng == "year":
        start = date(anchor.year, 1, 1)
        full_end = date(anchor.year + 1, 1, 1)
        prev, nxt = date(anchor.year - 1, 1, 1), full_end
    elif rng == "custom":
        f = min(_parse_date(from_, anchor), today)
        t = min(_parse_date(to, f), today)
        if t < f:
            f, t = t, f
        start, full_end = f, t + timedelta(days=1)
        span = (full_end - start).days
        prev, nxt = start - timedelta(days=span), full_end
    else:  # day
        start, full_end = anchor, anchor + timedelta(days=1)
        prev, nxt = start - timedelta(days=1), full_end
    end = min(full_end, tomorrow)
    to_date = full_end > tomorrow and rng != "day"
    return {
        "start": start, "end": end, "to_date": to_date,
        "prev": prev, "next": nxt if nxt <= today else None,
        "days": (end - start).days,
    }


def _recap_label(rng: str, w: dict, today: date) -> str:
    start, end_incl = w["start"], w["end"] - timedelta(days=1)
    suffix = " (to date)" if w["to_date"] else ""
    if rng == "day":
        if start == today:
            return "Today"
        if start == today - timedelta(days=1):
            return "Yesterday"
        return start.strftime("%A, %B %d, %Y").replace(" 0", " ")
    if rng == "week":
        return f"Week of {_month_day(start)}, {start.year}{suffix}"
    if rng == "month":
        return f"{start:%B} {start.year}{suffix}"
    if rng == "year":
        return f"{start.year}{suffix}"
    if start.year != end_incl.year:
        return f"{_month_day(start)}, {start.year} – {_month_day(end_incl)}, {end_incl.year}"
    if start == end_incl:
        return start.strftime("%A, %B %d, %Y").replace(" 0", " ")
    return f"{_month_day(start)} – {_month_day(end_incl)}, {start.year}"


@router.get("/recap", response_class=HTMLResponse)
def recap(
    request: Request,
    day: Optional[str] = Query(None),
    range_key: str = Query("day", alias="range"),
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
):
    """Recap: every species active in a window, with seen/heard counts.

    ``range`` is day (default) | week | month | year | custom; ``day`` is the anchor date
    the window is built around (YYYY-MM-DD, default today) — kept as the parameter name
    so pre-0.27 ``/recap?day=`` links still land on the same day. ``from``/``to`` bound a
    custom window. Anything unparseable or in the future degrades to a sane default
    rather than erroring. The month and year windows are month-/year-to-date while they
    contain today, which is the "how is this month going" view; past ones are whole.
    """
    today = date.today()
    rng = range_key if range_key in _RECAP_RANGES else "day"
    anchor = min(_parse_date(day, today), today)
    w = _recap_window(rng, anchor, from_, to, today)
    multi = w["days"] > 1

    gated = request.app.state.settings.require_species_confirmation
    rows = db.daily_recap(_midnight(w["start"]), _midnight(w["end"]), only_confirmed=gated)
    for r in rows:
        if multi:
            # Dates, not clock times: across a month "07:12 – 18:40" says nothing.
            first = date.fromtimestamp(r["first_time"])
            last = date.fromtimestamp(r["last_time"])
            r["first_label"] = _month_day(first)
            r["last_label"] = _month_day(last)
        else:
            r["first_label"] = time.strftime("%H:%M", time.localtime(r["first_time"]))
            r["last_label"] = time.strftime("%H:%M", time.localtime(r["last_time"]))

    def _nav(anchor_date: Optional[date]) -> Optional[str]:
        """Query string for a prev/next link: same range, moved anchor (custom: shifted
        from/to of the same length)."""
        if anchor_date is None:
            return None
        if rng == "custom":
            span = w["days"]
            f = anchor_date
            t = min(f + timedelta(days=span - 1), today)
            q = {"range": rng, "from": f.isoformat(), "to": t.isoformat()}
        else:
            q = {"range": rng, "day": anchor_date.isoformat()}
        return f"{ingress_url(request, 'recap')}?{urlencode(q)}"

    return render("recap.html", {
        "request": request,
        "page": "recap",
        "range": rng,
        "ranges": _RECAP_RANGES,
        "day": anchor.isoformat(),
        "today": today.isoformat(),
        "from": w["start"].isoformat(),
        "to": (w["end"] - timedelta(days=1)).isoformat(),
        "multi": multi,
        "days": w["days"],
        "day_label": _recap_label(rng, w, today),
        "prev_url": _nav(w["prev"]),
        "next_url": _nav(w["next"]),
        "rows": rows,
        # For a year in review the headline is what turned up for the first time.
        "new_rows": [r for r in rows if r.get("is_first_ever")] if multi else [],
        "totals": {
            "species": len(rows),
            "detections": sum(r["count"] for r in rows),
            "seen": sum(r["seen"] or 0 for r in rows),
            "heard": sum(r["heard"] or 0 for r in rows),
        },
    })


@router.get("/kept", response_class=HTMLResponse)
def kept(request: Request):
    """The kept-forever catalogue: every 📌-pinned clip, grouped by species.

    A curated shelf rather than a feed — full timestamps instead of relative ones
    (the cards render with absolute_time), because "when was this" is the point of
    keeping something.
    """
    rows = db.retained_detections()
    groups: list[dict] = []
    for det in rows:  # rows arrive species A→Z, newest first — one walk groups them
        if not groups or groups[-1]["species"] != det["common_name"]:
            groups.append({
                "species": det["common_name"],
                "scientific_name": det.get("scientific_name"),
                "items": [],
            })
        groups[-1]["items"].append(det)
    return render("kept.html", {
        "request": request,
        "page": "kept",
        "groups": groups,
        "total": len(rows),
    })


@router.get("/recent", response_class=HTMLResponse)
def recent(
    request: Request,
    source: Optional[str] = Query(None),
    species: Optional[str] = Query(None),
    zone: Optional[str] = Query(None),
    range_key: str = Query("all", alias="range"),
    before: Optional[float] = Query(None),
):
    ctx = _recent_ctx(request, source, species, range_key, before, zone)
    return render("recent.html", ctx)


@router.get("/recent/partial", response_class=HTMLResponse)
def recent_partial(
    request: Request,
    source: Optional[str] = Query(None),
    species: Optional[str] = Query(None),
    zone: Optional[str] = Query(None),
    range_key: str = Query("all", alias="range"),
    before: Optional[float] = Query(None),
    highlight_after: Optional[float] = Query(None),
):
    """Server-rendered detection groups for the Recent page's live refresh."""
    ctx = _recent_ctx(request, source, species, range_key, before, zone)
    ctx["highlight_after"] = highlight_after
    return render("_groups.html", ctx)


@router.get("/unidentified", response_class=HTMLResponse)
def unidentified(request: Request, before: Optional[float] = Query(None),
                 scope: Optional[str] = Query(None)):
    """Detections Aviary could not put a name to.

    Its own page rather than a filter on Recent, and deliberately separate from the species
    review queue: these are not a species awaiting approval, they are a *detection* awaiting
    a species, and the action you take is different (re-identify, not confirm/reject).

    No source/species/range filters — every row here is a Frigate detection with no species,
    so those controls would match either everything or nothing. ``scope=subjects`` swaps in
    the other queue: detections whose tracked bird HAS a name but some other bird in view
    does not — the same cards, with the unnamed bird's shortlist on its chip.
    """
    subjects_scope = scope == "subjects"
    if subjects_scope:
        rows = db.detections_with_unidentified_subjects(limit=PAGE_SIZE + 1, before=before)
    else:
        rows = db.unidentified_detections(limit=PAGE_SIZE + 1, before=before)
    has_more = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    next_before = rows[-1]["start_time"] if has_more and rows else None
    older_url = None
    if next_before is not None:
        q = {"before": f"{next_before:.6f}"}
        if subjects_scope:
            q["scope"] = "subjects"
        older_url = f"{ingress_url(request, 'unidentified')}?{urlencode(q)}"
    counts = db.unidentified_counts()
    ctx = {
        "request": request,
        "page": "unidentified",
        "scope": "subjects" if subjects_scope else "detections",
        "groups": _day_groups(rows),
        "counts": counts,
        "next_before": next_before,
        "older_url": older_url,
        "paged": before is not None,
        "identify_enabled": request.app.state.settings.identify_active,
    }
    return render("unidentified.html", ctx)


@router.get("/detection/{det_id}", response_class=HTMLResponse, name="detection_detail")
def detection_detail(request: Request, det_id: int):
    """A single detection: its media (clip/snapshot or audio/spectrogram) plus metadata.

    The drill-down behind a notification tap — HA's panel hands the route tail after
    /<addon_slug> to the iframe, which base.html turns into a real navigation to here.
    A missing id (deleted detection, stale notification, hand-edited URL) redirects to
    Recent rather than 404ing, matching species_detail's "degrade, don't error" stance.
    """
    det = db.detection_by_id(det_id)
    if det is None:
        return RedirectResponse(ingress_url(request, "recent"), status_code=302)
    visit = None
    if det.get("visit_id"):
        raw = db.visit_by_id(int(det["visit_id"]))
        if raw is not None:
            raw["members"] = db.visit_members(raw["id"])
            visit = _hydrate(request, [{**raw, "kind": "visit"}])[0]
    return render("detection.html", {
        "request": request,
        # Recent is the closest nav home for a single detection, so the tab highlights there.
        "page": "recent",
        "d": det,
        "visit": visit,
        "present": db.species_present([det_id]).get(det_id, []),
    })


@router.get("/visit/{visit_id}", response_class=HTMLResponse, name="visit_detail")
def visit_detail(request: Request, visit_id: int):
    """One visit — a Frigate review item — with every event inside it.

    The drill-down behind a visit card's timestamp and the ``visit_path`` a notification
    carries. Missing (deleted members, stale link) redirects to Recent, like
    detection_detail.
    """
    raw = db.visit_by_id(visit_id)
    if raw is None:
        return RedirectResponse(ingress_url(request, "recent"), status_code=302)
    raw["members"] = db.visit_members(visit_id)
    if not raw["members"]:
        return RedirectResponse(ingress_url(request, "recent"), status_code=302)
    visit = _hydrate(request, [{**raw, "kind": "visit"}])[0]
    return render("visit.html", {
        "request": request,
        "page": "recent",
        "v": visit,
    })


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
    # Registry framing for the Dex theme. The number is attached to each row rather
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
    # "bird" is the absence of a species, not a species. It has no registry entry, no
    # reference photos and nothing to confirm, so send it to the page that does know what
    # to do with those detections rather than rendering an empty species page.
    if name.strip().lower() == db.UNNAMED:
        return RedirectResponse(ingress_url(request, "unidentified"), status_code=302)
    stats = db.species_stats(name)
    # Only offer a video/audio filter when the species has both; default to video.
    has_both = bool(stats.get("frigate_total")) and bool(stats.get("birdnet_total"))
    sel = "all"
    if has_both:
        raw = source if source in ("frigate", "birdnet", "all") else None
        sel = raw or "frigate"
    src = _norm_source(sel)  # 'all' -> None (both streams)

    detections, next_before = _feed_page(request, src, name, before, None)
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
