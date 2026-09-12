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
