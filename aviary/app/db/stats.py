"""Dashboard statistics and chart series."""

from __future__ import annotations
import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from ._conn import _connect
from ._sql import HEARD_COUNT, SEEN_COUNT, SEEN_COUNT_BY_SPECIES, _confirmed_clause, _named_clause, _source_clause

__all__ = [
    "summary_stats",
    "top_species",
    "detections_per_day",
    "hourly_activity",
    "monthly_counts",
    "species_frequency",
    "zone_baselines",
    "active_zone_occupants",
]


def summary_stats(source: Optional[str] = None, since: Optional[float] = None,
                  only_confirmed: bool = False) -> dict:
    params: list = []
    where = "WHERE 1=1"
    where += _source_clause(source, params)
    where += _confirmed_clause(only_confirmed)
    if since is not None:
        where += " AND start_time >= ?"
        params.append(since)
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT
                {SEEN_COUNT} + {HEARD_COUNT} AS total,
                -- Species count excludes unidentified rows, but `total` deliberately
                -- does NOT: a bird nobody could name was still a bird that showed up,
                -- and dropping it would understate the detection count for the day.
                COUNT(DISTINCT CASE WHEN common_name != 'bird' COLLATE NOCASE
                                    THEN common_name END) AS species,
                {SEEN_COUNT}                 AS frigate_total,
                {HEARD_COUNT}                AS birdnet_total
            FROM species_sightings detections {where}
            """,
            params,
        ).fetchone()
    return dict(row) if row else {}


def top_species(
    limit: int = 10,
    source: Optional[str] = None,
    since: Optional[float] = None,
    only_confirmed: bool = False,
) -> list[dict]:
    params: list = []
    where = "WHERE 1=1"
    where += _source_clause(source, params)
    where += _confirmed_clause(only_confirmed)
    where += _named_clause()
    if since is not None:
        where += " AND start_time >= ?"
        params.append(since)
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT common_name, scientific_name,
                   {SEEN_COUNT_BY_SPECIES} + {HEARD_COUNT} AS count,
                   MAX(start_time) AS last_seen,
                   {HEARD_COUNT}           AS heard,
                   {SEEN_COUNT_BY_SPECIES} AS seen,
                   MAX(CASE WHEN source = 'frigate' THEN start_time END) AS last_frigate,
                   MAX(CASE WHEN source = 'birdnet' THEN start_time END) AS last_birdnet
            FROM species_sightings detections {where}
            GROUP BY common_name
            ORDER BY count DESC, last_seen DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def detections_per_day(
    days: int = 30,
    source: Optional[str] = None,
    species: Optional[str] = None,
    since: Optional[float] = None,
) -> list[dict]:
    """Count per local day (SQLite localtime); ``since`` epoch overrides ``days``."""
    params: list = []
    if since is not None:
        where = "WHERE start_time >= ?"
        params.append(since)
    else:
        days = max(1, min(int(days), 3650))
        where = "WHERE start_time >= CAST(strftime('%s', 'now', ?) AS REAL)"
        params.append(f"-{days} days")
    where += _source_clause(source, params)
    if species:
        where += " AND common_name = ?"
        params.append(species)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT date(start_time, 'unixepoch', 'localtime') AS day,
                   {SEEN_COUNT} + {HEARD_COUNT} AS count
            FROM species_sightings detections {where}
            GROUP BY day
            ORDER BY day
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def hourly_activity(
    source: Optional[str] = None,
    since: Optional[float] = None,
    species: Optional[str] = None,
) -> list[dict]:
    """Count per hour-of-day (0-23), aggregated across all matching detections."""
    params: list = []
    where = "WHERE 1=1"
    where += _source_clause(source, params)
    if since is not None:
        where += " AND start_time >= ?"
        params.append(since)
    if species:
        where += " AND common_name = ?"
        params.append(species)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT CAST(strftime('%H', start_time, 'unixepoch', 'localtime') AS INTEGER) AS hour,
                   {SEEN_COUNT} + {HEARD_COUNT} AS count
            FROM species_sightings detections {where}
            GROUP BY hour
            ORDER BY hour
            """,
            params,
        ).fetchall()
    counts = {int(r["hour"]): r["count"] for r in rows}
    return [{"hour": h, "count": counts.get(h, 0)} for h in range(24)]


# ------------------------------------------------- statistics for Home Assistant

def species_frequency(window_start: float, recent_start: float,
                      only_confirmed: bool = False) -> list[dict]:
    """Per species: all-time and recent counts, and days active inside the window.

    One pass over the sightings view. ``days_active`` counts distinct LOCAL days with a
    sighting at/after ``window_start`` — the numerator of the rarity score — and the
    ``recent`` counters use the same seen/heard units as every tile in the UI (a "seen"
    is one visit, not one Frigate event). Both starts should be local midnights so the
    windows are whole days. ``confirmed`` is reported even with the gate off, so a caller
    can tell an unreviewed species apart.
    """
    where = "WHERE 1=1" + _named_clause() + _confirmed_clause(only_confirmed)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT common_name,
                   MAX(scientific_name) AS scientific_name,
                   {SEEN_COUNT_BY_SPECIES} AS seen_all_time,
                   {HEARD_COUNT}           AS heard_all_time,
                   COUNT(DISTINCT CASE WHEN start_time >= :recent AND source = 'frigate'
                                       THEN COALESCE(visit_id, -id) END) AS seen_recent,
                   COALESCE(SUM(source = 'birdnet' AND start_time >= :recent), 0) AS heard_recent,
                   COUNT(DISTINCT CASE WHEN start_time >= :window
                                       THEN date(start_time, 'unixepoch', 'localtime') END)
                                                          AS days_active,
                   MIN(start_time) AS first_seen,
                   MAX(start_time) AS last_seen,
                   EXISTS (SELECT 1 FROM species_confirmed sc
                           WHERE sc.common_name = detections.common_name COLLATE NOCASE)
                                                          AS confirmed
            FROM species_sightings detections {where}
            GROUP BY common_name
            ORDER BY common_name COLLATE NOCASE
            """,
            {"recent": recent_start, "window": window_start},
        ).fetchall()
    return [dict(r) for r in rows]


def zone_baselines(window_start: float, only_confirmed: bool = False) -> list[dict]:
    """(zone combination, species, visits) for named camera sightings in the window.

    Raw material for a zone's *baseline* — what an unnamed bird there most likely is,
    weighted by how many visits each species paid. Zones are stored comma-joined
    ("bath, platform"); the caller splits them.
    """
    where = ("WHERE source = 'frigate' AND zone IS NOT NULL AND zone != '' "
             "AND start_time >= ?" + _named_clause() + _confirmed_clause(only_confirmed))
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT zone, common_name, {SEEN_COUNT_BY_SPECIES} AS visits
            FROM species_sightings detections {where}
            GROUP BY zone, common_name
            """,
            (window_start,),
        ).fetchall()
    return [dict(r) for r in rows]


def active_zone_occupants(now: float, memory_s: float, stale_s: float = 7200.0) -> list[dict]:
    """Which species are — or, within ``memory_s``, recently were — in which zones.

    Three sources, each a row ``{zone, species, since, until, source}`` (``species`` None
    for a bird nobody has named yet, ``until`` None while live):

    * Frigate events in progress (``end_time IS NULL``) with a zone → ``frigate``; an event
      that ended less than ``memory_s`` ago → ``memory``. Frigate's own label rides on
      in-progress rows once its classifier names the bird.
    * Visits (Frigate review items) open or closed less than ``memory_s`` ago, with the
      species already settled for them (``visit_announced`` — which is how aviary-id's
      answer, landing after a fragment ended, still names the zone for the fragments
      that follow) → ``visit`` / ``memory``. A visit's zones are its own list plus its
      members'.

    ``stale_s`` caps how long an event or visit Frigate never closed can pin a zone.
    """
    out: list[dict] = []
    stale, mem = now - stale_s, now - memory_s
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT common_name, zone, start_time, end_time
            FROM detections
            WHERE source = 'frigate' AND zone IS NOT NULL AND zone != ''
              AND ((end_time IS NULL AND start_time >= :stale)
                   OR (end_time IS NOT NULL AND end_time >= :mem))
            """,
            {"stale": stale, "mem": mem},
        ).fetchall()
        for r in rows:
            live = r["end_time"] is None
            name = r["common_name"] if r["common_name"] != "bird" else None
            out.append({"zone": r["zone"], "species": name, "since": r["start_time"],
                        "until": None if live else r["end_time"] + memory_s,
                        "source": "frigate" if live else "memory"})
        visits = conn.execute(
            """
            SELECT v.id, v.zones, v.start_time, v.end_time, v.updated_at, va.common_name
            FROM visits v
            LEFT JOIN visit_announced va ON va.visit_id = v.id
            WHERE (v.end_time IS NULL AND v.updated_at >= :stale)
               OR (v.end_time IS NOT NULL AND v.end_time >= :mem)
            """,
            {"stale": stale, "mem": mem},
        ).fetchall()
        if visits:
            ids = sorted({v["id"] for v in visits})
            member_zones: dict[int, set[str]] = {}
            for m in conn.execute(
                f"SELECT visit_id, zone FROM detections WHERE visit_id IN "
                f"({','.join('?' * len(ids))}) AND zone IS NOT NULL AND zone != ''", ids,
            ).fetchall():
                member_zones.setdefault(m["visit_id"], set()).add(m["zone"])
        for v in visits:
            zones = ", ".join(sorted({z for combo in
                                      ([v["zones"]] if v["zones"] else []) + sorted(member_zones.get(v["id"], ()))
                                      for z in combo.split(",") if z.strip()}))
            if not zones:
                continue
            live = v["end_time"] is None
            out.append({"zone": zones, "species": v["common_name"], "since": v["start_time"],
                        "until": None if live else v["end_time"] + memory_s,
                        "source": "visit" if live else "memory"})
    return out


def monthly_counts(species: str) -> list[int]:
    """All-time sightings of a species per calendar month (January first), the same units
    as everywhere else (visits + heard rows) — the Seasonality card's "at your feeder"
    overlay against the regional curve."""
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT CAST(strftime('%m', start_time, 'unixepoch', 'localtime') AS INTEGER) AS month,
                   {SEEN_COUNT} + {HEARD_COUNT} AS count
            FROM species_sightings detections
            WHERE common_name = ? COLLATE NOCASE
            GROUP BY month
            """,
            (species,),
        ).fetchall()
    counts = {int(r["month"]): int(r["count"] or 0) for r in rows}
    return [counts.get(m, 0) for m in range(1, 13)]
