"""View-model shaping for the Recap page and the species index's attendance signals.

Pure functions over what ``db`` returns — no I/O — so the percent maths, the tier
thresholds and the highlight picks are unit-testable without a database, the same way
``visits.card_model`` keeps card shaping out of the routes.

Timeline geometry is expressed in percent of the window's real length (a DST day is 23
or 25 hours), so a tick, the hour strip above it and the sunrise marker all use one
scale and line up in CSS without JavaScript.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from . import solar

# Attendance tiers: days with a detection over days since the species first appeared.
# A species needs a fortnight of history before it is classified at all — a bird seen
# twice in its first three days is "new", not "regular".
MIN_HISTORY_DAYS = 14
# A gap this long before a window's first detection earns "back after N days".
RETURN_GAP_DAYS = 14
TIERS = (
    ("daily", 0.70, "most days"),
    ("regular", 0.30, "several days a week"),
    ("occasional", 0.10, "a few days a month"),
    ("rare", 0.0, "seldom"),
)

# A row with more ticks than this collapses to density bins: hundreds of 2px marks read
# as a smear anyway, and the DOM cost on a BirdNET-heavy day is real.
MAX_TICKS_PER_ROW = 150
DENSITY_BINS = 96  # 15-minute bins

AXIS = ("00", "06", "12", "18", "24")


# ------------------------------------------------------------------- attendance

def tier(first_seen: Optional[float], days_active: int, now: float) -> Optional[dict]:
    """Regularity tier for a species, or None while it has under two weeks of history."""
    if not first_seen or days_active <= 0:
        return None
    try:
        history = (date.fromtimestamp(now) - date.fromtimestamp(float(first_seen))).days + 1
    except (OverflowError, OSError, ValueError):
        return None
    if history < MIN_HISTORY_DAYS:
        return None
    ratio = min(1.0, days_active / history)
    for key, threshold, label in TIERS:
        if ratio >= threshold:
            return {"key": key, "label": key, "hint": label, "ratio": round(ratio, 3),
                    "history_days": history, "days_active": days_active}
    return None


def back_after(first_in_window: Optional[float], prev_before: Optional[float]) -> Optional[int]:
    """Whole days of silence before this window's first detection, when it was a long one."""
    if not first_in_window or not prev_before:
        return None
    gap = (date.fromtimestamp(float(first_in_window)) - date.fromtimestamp(float(prev_before))).days
    return gap if gap >= RETURN_GAP_DAYS else None


def sparkline(daily: dict[str, int], today: date, days: int = 7) -> tuple[list[int], int]:
    """The last ``days`` local days as counts (oldest first) plus their max, from a
    {YYYY-MM-DD: count} map that only lists days with activity."""
    series = [int(daily.get((today - timedelta(days=days - 1 - i)).isoformat(), 0))
              for i in range(days)]
    return series, max(series) if series else 0


# --------------------------------------------------------------------- geometry

def pct(t: float, start: float, end: float) -> float:
    """Position of ``t`` in [start, end) as a percent, clamped to the ribbon."""
    span = end - start
    if span <= 0:
        return 0.0
    return round(min(100.0, max(0.0, (t - start) / span * 100.0)), 2)


def _tick_title(t: float, source: str, n: int) -> str:
    if source == "frigate":
        return f"{solar.clock(t)} · seen" + (f" (visit of {n} clips)" if n > 1 else "")
    return f"{solar.clock(t)} · heard"


def day_ribbon(ticks: list[dict], day_start: float, day_end: float) -> dict:
    """A species' day as ticks, or as 15-minute density bins when there are too many."""
    if len(ticks) <= MAX_TICKS_PER_ROW:
        return {"mode": "ticks", "ticks": [
            {"pct": pct(t["t"], day_start, day_end),
             "cls": "seen" if t["source"] == "frigate" else "heard",
             "title": _tick_title(t["t"], t["source"], t.get("n", 1))}
            for t in ticks]}
    bins = [{"seen": 0, "heard": 0} for _ in range(DENSITY_BINS)]
    for t in ticks:
        i = min(DENSITY_BINS - 1, int(pct(t["t"], day_start, day_end) / 100.0 * DENSITY_BINS))
        bins[i]["seen" if t["source"] == "frigate" else "heard"] += 1
    peak = max(b["seen"] + b["heard"] for b in bins) or 1
    w = round(100.0 / DENSITY_BINS, 3)
    out = []
    for i, b in enumerate(bins):
        total = b["seen"] + b["heard"]
        if not total:
            continue
        lo = day_start + (day_end - day_start) * i / DENSITY_BINS
        hi = day_start + (day_end - day_start) * (i + 1) / DENSITY_BINS
        out.append({
            "pct": round(i * w, 3), "w": w,
            "a": round(0.25 + 0.75 * total / peak, 2),
            "cls": "seen" if b["seen"] >= b["heard"] else "heard",
            "title": f"{solar.clock(lo)}–{solar.clock(hi)} · {b['seen']} seen, {b['heard']} heard",
        })
    return {"mode": "bins", "bins": out}


def _hour_bar(h: int, seen: int, heard: int, peak: int) -> dict:
    total = seen + heard
    parts = []
    if seen:
        parts.append(f"{seen} seen")
    if heard:
        parts.append(f"{heard} heard")
    return {
        "pct": round(h / 24 * 100, 3), "w": round(100 / 24, 3),
        "h": round(100.0 * total / peak, 1) if peak else 0.0,
        "seen_share": round(100.0 * seen / total, 1) if total else 0.0,
        "seen": seen, "heard": heard, "count": total,
        "title": f"{h:02d}:00–{(h + 1) % 24:02d}:00 · " + (", ".join(parts) or "nothing"),
    }


def hour_ribbon(hours: list[dict]) -> dict:
    """A species' hour-of-day histogram (multi-day windows). Only non-empty hours are
    emitted — most rows are sparse."""
    peak = max((h["seen"] + h["heard"] for h in hours), default=0)
    return {"mode": "hours", "bars": [
        _hour_bar(h["hour"], h["seen"], h["heard"], peak)
        for h in hours if h["seen"] + h["heard"]]}


def strip_from_ticks(ticks_by_name: dict[str, list[dict]], day_start: float,
                     day_end: float) -> list[dict]:
    """The day-wide hour strip, from the same ticks the rows show. Bucketed by position
    on the ribbon rather than by clock hour so bars and ticks share one scale."""
    seen, heard = [0] * 24, [0] * 24
    for ticks in ticks_by_name.values():
        for t in ticks:
            i = min(23, int(pct(t["t"], day_start, day_end) / 100.0 * 24))
            if t["source"] == "frigate":
                seen[i] += 1
            else:
                heard[i] += 1
    peak = max(s + h for s, h in zip(seen, heard)) or 0
    return [_hour_bar(h, seen[h], heard[h], peak) for h in range(24)]


def strip_from_hours(hours_by_name: dict[str, list[dict]]) -> list[dict]:
    """The day-wide hour strip for a multi-day window: every species' histogram summed."""
    seen, heard = [0] * 24, [0] * 24
    for hours in hours_by_name.values():
        for b in hours:
            seen[b["hour"]] += b["seen"]
            heard[b["hour"]] += b["heard"]
    peak = max(s + h for s, h in zip(seen, heard)) or 0
    return [_hour_bar(h, seen[h], heard[h], peak) for h in range(24)]


# ------------------------------------------------------------------ sun overlay

def shading_gradient(sun: solar.SunTimes) -> str:
    """One CSS gradient painting night, the dawn-chorus window and dusk across the
    ribbon — set once on the list container, inherited by every row's ribbon.

    The chorus window is always shaded (it is what the dawn card is counting, fallback
    or not); night and dusk only when the sun is actually known.
    """
    s, e = sun.day_start, sun.day_end
    stops: list[tuple[float, float, str]] = []  # (from%, to%, colour var)
    chorus = (pct(sun.chorus_start, s, e), pct(sun.chorus_end, s, e))
    if sun.known and sun.sunrise is not None and sun.sunset is not None:
        dawn = pct(sun.dawn if sun.dawn is not None else sun.chorus_start, s, e)
        dusk = pct(sun.dusk if sun.dusk is not None else sun.sunset, s, e)
        sunset = pct(sun.sunset, s, e)
        stops.append((0.0, min(dawn, chorus[0]), "var(--ribbon-night)"))
        stops.append((min(dawn, chorus[0]), chorus[1], "var(--ribbon-dawn)"))
        stops.append((max(chorus[1], sunset - pct(s + 3600, s, e)), dusk, "var(--ribbon-dusk)"))
        stops.append((dusk, 100.0, "var(--ribbon-night)"))
    else:
        stops.append((chorus[0], chorus[1], "var(--ribbon-dawn)"))
    # Hard stops: each shaded band is "colour lo%, colour hi%" with transparent gaps between
    # bands only where there is a gap, so adjacent bands share one boundary stop.
    parts: list[str] = []
    cursor = 0.0
    for lo, hi, colour in stops:
        if hi <= lo:
            continue
        if lo > cursor:
            parts.append(f"transparent {cursor}%, transparent {lo}%")
        parts.append(f"{colour} {lo}%, {colour} {hi}%")
        cursor = hi
    if cursor < 100.0:
        parts.append(f"transparent {cursor}%, transparent 100%")
    return "linear-gradient(90deg, " + ", ".join(parts) + ")"


def markers(sun: solar.SunTimes) -> list[dict]:
    """Sunrise/sunset lines for the ribbons; empty when the sun is unknown."""
    if not sun.known:
        return []
    out = []
    for kind, t in (("sunrise", sun.sunrise), ("sunset", sun.sunset)):
        if t is not None:
            out.append({"kind": kind, "pct": pct(t, sun.day_start, sun.day_end),
                        "label": solar.clock(t)})
    return out


def window_labels(sun: solar.SunTimes) -> dict:
    return {
        "sunrise": solar.clock(sun.sunrise), "sunset": solar.clock(sun.sunset),
        "chorus_start": solar.clock(sun.chorus_start),
        "chorus_end": solar.clock(sun.chorus_end), "known": sun.known,
    }


# ------------------------------------------------------------------- page parts

def dawn_card(rows: list[dict], ticks_by_name: dict[str, list[dict]],
              sun: solar.SunTimes) -> list[dict]:
    """Species with a detection inside the dawn-chorus window, by first appearance."""
    out = []
    for r in rows:
        inside = [t for t in ticks_by_name.get(r["common_name"], [])
                  if sun.chorus_start <= t["t"] < sun.chorus_end]
        if not inside:
            continue
        first = min(t["t"] for t in inside)
        out.append({
            "common_name": r["common_name"],
            "scientific_name": r.get("scientific_name"),
            "snapshot_ref": r.get("snapshot_ref"),
            "seen": sum(1 for t in inside if t["source"] == "frigate"),
            "heard": sum(1 for t in inside if t["source"] != "frigate"),
            "first": first, "first_label": solar.clock(first),
            "pct": pct(first, sun.day_start, sun.day_end),
        })
    out.sort(key=lambda d: d["first"])
    return out


def _count_sub(r: dict) -> str:
    parts = []
    if r.get("seen"):
        parts.append(f"seen ×{r['seen']}")
    if r.get("heard"):
        parts.append(f"heard ×{r['heard']}")
    return " · ".join(parts)


def highlights(rows: list[dict], multi: bool, days: int, new_count: int) -> list[dict]:
    """The stat tiles above the list. Rows carry ``tier`` already (set by the view)."""
    if not rows:
        return []
    tiles = []
    busiest = max(rows, key=lambda r: (r["count"], r["last_time"]))
    tiles.append({"label": "Busiest", "name": busiest["common_name"],
                  "value": busiest["count"], "sub": _count_sub(busiest)})
    if multi:
        regular = max(rows, key=lambda r: (r.get("days_active") or 0, r["count"]))
        if (regular.get("days_active") or 0) > 1:
            tiles.append({"label": "Most regular", "name": regular["common_name"],
                          "value": f"{regular['days_active']}/{days}",
                          "sub": "days present"})
        tiles.append({"label": "New species", "name": None, "value": new_count,
                      "sub": "first-ever this period", "anchor": "new" if new_count else None})
    else:
        first = min(rows, key=lambda r: r["first_time"])
        last = max(rows, key=lambda r: r["last_time"])
        tiles.append({"label": "First bird", "name": first["common_name"],
                      "value": solar.clock(first["first_time"]), "sub": ""})
        tiles.append({"label": "Last bird", "name": last["common_name"],
                      "value": solar.clock(last["last_time"]), "sub": ""})
    # Rarest: the established species that turns up least — never a first-timer (that is
    # "new!") and never a regular, or the tile would crown someone on a quiet day.
    candidates = [r for r in rows if r.get("tier") and not r.get("is_first_ever")
                  and r["tier"]["key"] in ("rare", "occasional")]
    if candidates:
        rare = min(candidates, key=lambda r: r["tier"]["ratio"])
        tiles.append({"label": "Rarest visitor", "name": rare["common_name"],
                      "value": rare["tier"]["label"],
                      "sub": f"{rare['tier']['days_active']} of {rare['tier']['history_days']} days ever"})
    return tiles


def sort_rows(rows: list[dict], sort: str) -> list[dict]:
    if sort == "first":
        return sorted(rows, key=lambda r: (r["first_time"], -r["count"]))
    return sorted(rows, key=lambda r: (-r["count"], -r["last_time"]))


# ------------------------------------------------------------- species page line

_PHASE_WORD = {"dawn": "Dawn", "morning": "Morning", "midday": "Midday",
               "afternoon": "Afternoon", "evening": "Evening", "night": "Night"}


def typical_hours(hours: list[dict], sun: solar.SunTimes, stats: dict,
                  min_total: int = 10) -> Optional[dict]:
    """"Dawn singer · peaks 05–07" from an all-time hour histogram; None when too thin.

    The peak range is the run of hours around the busiest one that each hold at least
    half its count. The phase word comes from where that run's centre falls relative to
    today's sun, so a species that peaks at 05:00 is a dawn bird in June and a night one
    in December — which is the truth of it.
    """
    counts = [int(h.get("count") or 0) for h in hours]
    if len(counts) != 24 or sum(counts) < min_total:
        return None
    peak_h = max(range(24), key=lambda h: counts[h])
    peak = counts[peak_h]
    if not peak:
        return None
    lo = hi = peak_h
    while lo > 0 and counts[lo - 1] >= peak / 2:
        lo -= 1
    while hi < 23 and counts[hi + 1] >= peak / 2:
        hi += 1
    centre = sun.day_start + ((lo + hi + 1) / 2) * 3600
    phase = solar.phase_label(centre, sun)
    noun = "singer" if (stats.get("birdnet_total") or 0) > (stats.get("frigate_total") or 0) else "visitor"
    return {"label": f"{_PHASE_WORD[phase]} {noun}", "range": f"{lo:02d}–{(hi + 1):02d}",
            "phase": phase, "peak_hour": peak_h}
