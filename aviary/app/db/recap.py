"""Daily recap rows, ticks, hourly-by-species and regularity tiers."""

from __future__ import annotations
import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from ._conn import _connect
from ._sql import HEARD_COUNT, SEEN_COUNT_BY_SPECIES, _confirmed_clause, _named_clause

__all__ = [
    "daily_recap",
    "recap_ticks",
    "hourly_by_species",
    "_REGULARITY_TTL_S",
    "_regularity_cache",
    "species_regularity",
    "daily_counts_by_species",
]


def daily_recap(day_start: float, day_end: float,
                only_confirmed: bool = False) -> list[dict]:
    """Per-species activity within [day_start, day_end) — the Recap page's day view.

    The only aggregate query with BOTH time bounds: every other stat is "since", but a
    recap for last Tuesday needs Tuesday alone. ``is_first_ever`` marks species whose
    first detection ever falls inside the window ("new!" on the page) — computed from
    the species' all-time minimum, not the window's, so revisiting an old day doesn't
    re-badge everything that was merely new-to-that-window.
    """
    params: list = [day_start, day_end]
    where = ("WHERE start_time >= ? AND start_time < ?"
             + _named_clause() + _confirmed_clause(only_confirmed))
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT common_name,
                   MAX(scientific_name) AS scientific_name,
                   {SEEN_COUNT_BY_SPECIES} + {HEARD_COUNT} AS count,
                   {SEEN_COUNT_BY_SPECIES} AS seen,
                   {HEARD_COUNT}           AS heard,
                   MIN(start_time)      AS first_time,
                   MAX(start_time)      AS last_time,
                   -- Distinct local days with activity: the multi-day recap's
                   -- "how regular a visitor" signal.
                   COUNT(DISTINCT date(start_time, 'unixepoch', 'localtime')) AS days_active
                   -- No image column here: the row's picture comes from
                   -- species_heroes(start, end) via heroes.pick, so a species that was
                   -- only "also in view" shows its own crop, not the other bird.
            FROM species_sightings detections {where}
            GROUP BY common_name
            ORDER BY count DESC, last_time DESC
            """,
            params,
        ).fetchall()
        recap = [dict(r) for r in rows]
        if recap:
            marks = ",".join("?" for _ in recap)
            firsts = conn.execute(
                f"""
                SELECT common_name, MIN(start_time) AS first_ever
                FROM species_sightings detections
                WHERE common_name IN ({marks}) COLLATE NOCASE
                GROUP BY common_name
                """,
                [r["common_name"] for r in recap],
            ).fetchall()
            first_ever = {r["common_name"].lower(): r["first_ever"] for r in firsts}
            for row in recap:
                ever = first_ever.get(row["common_name"].lower())
                row["is_first_ever"] = bool(
                    ever is not None and day_start <= ever < day_end)
    return recap


def recap_ticks(day_start: float, day_end: float,
                only_confirmed: bool = False) -> dict[str, list[dict]]:
    """Every seen/heard unit in [day_start, day_end) as a timeline tick, per species.

    Backs the recap's day ribbons. One tick per *visit* (or per Frigate event without a
    visit) and one per BirdNET row — the same units ``daily_recap`` counts, collapsed in
    SQL so a busy birdbath visit of twenty tracked objects is one tick, not twenty. ``n``
    is how many detections the tick stands for (a visit's clip count); ``t`` is the
    unit's first detection. Bounded by one day's rows, so no cap here — the view collapses
    dense rows to density bins.
    """
    where = ("WHERE start_time >= ? AND start_time < ?"
             + _named_clause() + _confirmed_clause(only_confirmed))
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT common_name, source, MIN(start_time) AS t, COUNT(DISTINCT id) AS n
            FROM species_sightings detections {where}
            GROUP BY common_name, source,
                     CASE WHEN source = 'frigate' THEN COALESCE(visit_id, -id) ELSE id END
            ORDER BY common_name, t
            """,
            [day_start, day_end],
        ).fetchall()
    ticks: dict[str, list[dict]] = {}
    for r in rows:
        ticks.setdefault(r["common_name"], []).append(
            {"t": float(r["t"]), "source": r["source"], "n": int(r["n"])})
    return ticks


def hourly_by_species(start: float, end: float,
                      only_confirmed: bool = False) -> dict[str, list[dict]]:
    """Hour-of-day histogram per species over [start, end) — the multi-day recap's ribbon.

    24 zero-filled buckets per species, seen counting visits and heard counting rows,
    like everything else on the page. Local hours, like ``hourly_activity``.
    """
    where = ("WHERE start_time >= ? AND start_time < ?"
             + _named_clause() + _confirmed_clause(only_confirmed))
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT common_name,
                   CAST(strftime('%H', start_time, 'unixepoch', 'localtime') AS INTEGER) AS hour,
                   {SEEN_COUNT_BY_SPECIES} AS seen,
                   {HEARD_COUNT}           AS heard
            FROM species_sightings detections {where}
            GROUP BY common_name, hour
            """,
            [start, end],
        ).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        buckets = out.setdefault(
            r["common_name"], [{"hour": h, "seen": 0, "heard": 0} for h in range(24)])
        h = int(r["hour"]) % 24
        buckets[h]["seen"] += int(r["seen"] or 0)
        buckets[h]["heard"] += int(r["heard"] or 0)
    return out


# species_regularity() scans the whole table (a date() per row); regularity moves by
# days, so a couple of minutes of staleness is invisible and saves the scan on every
# recap step and species-index load.
_REGULARITY_TTL_S = 120.0


_regularity_cache: dict[tuple, tuple[float, dict]] = {}


def species_regularity(before: Optional[float] = None,
                       only_confirmed: bool = False) -> dict[str, dict]:
    """All-time attendance per species, keyed by lower-cased common name.

    ``first_seen``/``last_seen`` (epochs), ``days_active`` (distinct local days with a
    detection) and — when ``before`` is given — ``prev_before``: the last detection
    strictly before that instant, which is what "back after N days" on a recap window
    compares against. The regularity tiers (daily / regular / occasional / rare) are
    derived from days_active over the span since first_seen by ``recap.tier``.
    """
    key = (int(before or 0), bool(only_confirmed))
    now = time.time()
    hit = _regularity_cache.get(key)
    if hit and now - hit[0] < _REGULARITY_TTL_S:
        return hit[1]
    where = "WHERE 1=1" + _named_clause() + _confirmed_clause(only_confirmed)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT common_name,
                   MIN(start_time) AS first_seen,
                   MAX(start_time) AS last_seen,
                   COUNT(DISTINCT date(start_time, 'unixepoch', 'localtime')) AS days_active,
                   MAX(CASE WHEN start_time < ? THEN start_time END) AS prev_before
            FROM species_sightings detections {where}
            GROUP BY common_name
            """,
            [before if before is not None else 0.0],
        ).fetchall()
    out = {
        r["common_name"].lower(): {
            "first_seen": r["first_seen"], "last_seen": r["last_seen"],
            "days_active": int(r["days_active"] or 0), "prev_before": r["prev_before"],
        }
        for r in rows
    }
    _regularity_cache[key] = (now, out)
    return out


def daily_counts_by_species(since: float,
                            only_confirmed: bool = False) -> dict[str, dict[str, int]]:
    """{species: {YYYY-MM-DD: count}} from ``since`` — the species index's sparklines.

    Same units as the tiles' counts (visits + heard rows); local days; only days with
    activity are present, so the caller zero-fills its window.
    """
    where = "WHERE start_time >= ?" + _named_clause() + _confirmed_clause(only_confirmed)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT common_name,
                   date(start_time, 'unixepoch', 'localtime') AS day,
                   {SEEN_COUNT_BY_SPECIES} + {HEARD_COUNT} AS count
            FROM species_sightings detections {where}
            GROUP BY common_name, day
            """,
            [since],
        ).fetchall()
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        out.setdefault(r["common_name"], {})[r["day"]] = int(r["count"] or 0)
    return out
