"""Per-species aggregates, confirmation, registry numbering, heroes and name canonicalisation."""

from __future__ import annotations
import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from ._conn import _connect
from ._sql import HEARD_COUNT, SEEN_COUNT_BY_SPECIES, _confirmed_clause, _named_clause, _source_clause

__all__ = [
    "species_stats",
    "confirm_species",
    "unconfirm_species",
    "is_species_confirmed",
    "unconfirmed_count",
    "new_species_count",
    "species_list",
    "distinct_species",
    "species_dex_numbers",
    "registry_stats",
    "HERO_CANDIDATES",
    "species_heroes",
    "scientific_name_for",
    "canonical_species",
    "species_last_times",
]


def species_stats(name: str) -> dict:
    """Aggregate stats for one species (all-time)."""
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT
                {SEEN_COUNT_BY_SPECIES} + {HEARD_COUNT} AS total,
                MIN(start_time)         AS first_seen,
                MAX(start_time)         AS last_seen,
                MAX(confidence)         AS best_confidence,
                {SEEN_COUNT_BY_SPECIES} AS frigate_total,
                {HEARD_COUNT}           AS birdnet_total,
                MIN(CASE WHEN source = 'frigate' THEN start_time END) AS first_frigate,
                MAX(CASE WHEN source = 'frigate' THEN start_time END) AS last_frigate,
                MIN(CASE WHEN source = 'birdnet' THEN start_time END) AS first_birdnet,
                MAX(CASE WHEN source = 'birdnet' THEN start_time END) AS last_birdnet,
                MAX(scientific_name)    AS scientific_name
            FROM species_sightings detections WHERE common_name = ?
            """,
            (name,),
        ).fetchone()
    return dict(row) if row else {}


def confirm_species(common_name: str) -> None:
    """Approve a species into the registry (idempotent; keeps the original timestamp)."""
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO species_confirmed (common_name, confirmed_at) VALUES (?, ?)",
            (common_name, time.time()),
        )


def unconfirm_species(common_name: str) -> bool:
    """Send a species back to the review queue. True if it had been confirmed."""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM species_confirmed WHERE common_name = ? COLLATE NOCASE", (common_name,)
        )
        return cur.rowcount > 0


def is_species_confirmed(common_name: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM species_confirmed WHERE common_name = ? COLLATE NOCASE", (common_name,)
        ).fetchone()
    return row is not None


def unconfirmed_count() -> int:
    """How many detected species are still awaiting review."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM (
                SELECT common_name FROM species_sightings detections
                WHERE common_name != '' AND common_name != 'bird' COLLATE NOCASE
                  AND NOT EXISTS (
                    SELECT 1 FROM species_confirmed sc
                    WHERE sc.common_name = detections.common_name COLLATE NOCASE
                )
                GROUP BY common_name
            )
            """
        ).fetchone()
    return row["c"] if row else 0


def new_species_count(source: Optional[str] = None, since: Optional[float] = None,
                      only_confirmed: bool = False) -> int:
    """Number of species whose *first-ever* detection falls at/after ``since``.

    With ``since`` None (all-time), every species is "new", so this returns the distinct
    species count.
    """
    params: list = []
    src = _source_clause(source, params) + _confirmed_clause(only_confirmed) + _named_clause()
    if since is None:
        with _connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(DISTINCT common_name) AS c FROM species_sightings detections WHERE 1=1{src}",
                params,
            ).fetchone()
        return row["c"] if row else 0
    params.append(since)
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS c FROM (
                SELECT common_name FROM species_sightings detections WHERE 1=1{src}
                GROUP BY common_name HAVING MIN(start_time) >= ?
            )
            """,
            params,
        ).fetchone()
    return row["c"] if row else 0


def species_list(
    source: Optional[str] = None,
    since: Optional[float] = None,
    only_new: bool = False,
    only_confirmed: bool = False,
    only_unconfirmed: bool = False,
) -> list[dict]:
    """Per-species aggregates for the species index.

    ``only_new`` keeps just species whose first-ever detection is at/after ``since``
    (first_seen/count are all-time). Otherwise, when ``since`` is given, results are
    limited to species active within the window (count is the in-window count).

    ``only_unconfirmed`` inverts the confirmation filter to build the review queue; it
    ignores ``only_confirmed``, since asking for both would always be empty.
    """
    params: list = []
    where = "WHERE 1=1" + _source_clause(source, params) + _named_clause()
    if only_unconfirmed:
        where += (" AND NOT EXISTS (SELECT 1 FROM species_confirmed sc"
                  " WHERE sc.common_name = detections.common_name COLLATE NOCASE)")
    else:
        where += _confirmed_clause(only_confirmed)
    having = ""
    if only_new and since is not None:
        having = " HAVING MIN(start_time) >= ?"
    elif since is not None:
        where += " AND start_time >= ?"
        params.append(since)
    order = "first_seen DESC" if only_new else "count DESC, last_seen DESC"
    sql = f"""
        SELECT common_name,
               MAX(scientific_name) AS scientific_name,
               {SEEN_COUNT_BY_SPECIES} + {HEARD_COUNT} AS count,
               MIN(start_time)      AS first_seen,
               MAX(start_time)      AS last_seen,
               {SEEN_COUNT_BY_SPECIES} AS frigate_total,
               {HEARD_COUNT}           AS birdnet_total,
               MIN(CASE WHEN source = 'frigate' THEN start_time END) AS first_frigate,
               MAX(CASE WHEN source = 'frigate' THEN start_time END) AS last_frigate,
               MIN(CASE WHEN source = 'birdnet' THEN start_time END) AS first_birdnet,
               MAX(CASE WHEN source = 'birdnet' THEN start_time END) AS last_birdnet
        FROM species_sightings detections {where}
        GROUP BY common_name{having}
        ORDER BY {order}
    """
    if only_new and since is not None:
        params.append(since)
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def distinct_species(source: Optional[str] = None, include_subjects: bool = True) -> list[str]:
    """Every species name. ``include_subjects=False`` restricts to species that have been
    the tracked bird of an event (or heard); the default also counts other birds in view,
    which is what "new species" notifications key on since 0.35.0."""
    params: list = []
    where = "WHERE 1=1" + _source_clause(source, params) + _named_clause()
    table = "species_sightings detections" if include_subjects else "detections"
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT common_name FROM {table} {where} ORDER BY common_name",
            params,
        ).fetchall()
    return [r["common_name"] for r in rows]


def species_dex_numbers(only_confirmed: bool = False) -> dict[str, int]:
    """Registry number per species: 1-based order of first-ever detection.

    Derived rather than stored, so there's no counter to keep in sync with deletions.
    Removing a species renumbers the ones after it, which is fine — the number is a
    display flourish for the Dex theme, not an identifier anything keys on.

    Unconfirmed species get no number at all while the gate is on: the templates already
    render ``No.???`` for a missing one, so a species earns its entry by being approved.
    """
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT common_name,
                   ROW_NUMBER() OVER (ORDER BY MIN(start_time), common_name) AS dex_no
            FROM species_sightings detections
            WHERE 1=1{_confirmed_clause(only_confirmed)}{_named_clause()}
            GROUP BY common_name
            """
        ).fetchall()
    return {r["common_name"]: r["dex_no"] for r in rows}


def registry_stats(only_confirmed: bool = False) -> dict:
    """Species counts for the Dex completion readout.

    ``seen`` (on camera, the "caught" analogue) is the subset of ``total`` that has at
    least one Frigate detection; ``heard`` likewise for BirdNET-Go. A species can be
    both. There is no master checklist, so ``total`` is only what's been detected.

    ``unconfirmed`` is the review queue's size and is deliberately NOT filtered — it
    counts what's missing from the registry, so it's the one figure that has to look past
    the gate.
    """
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS total,
                   SUM(seen)  AS seen,
                   SUM(heard) AS heard
            FROM (
                SELECT MAX(source = 'frigate') AS seen,
                       MAX(source = 'birdnet') AS heard
                FROM species_sightings detections
                WHERE 1=1{_confirmed_clause(only_confirmed)}{_named_clause()}
                GROUP BY common_name
            )
            """
        ).fetchone()
    pending = unconfirmed_count() if only_confirmed else 0
    if not row:
        return {"total": 0, "seen": 0, "heard": 0, "unconfirmed": pending}
    return {
        "total": row["total"] or 0,
        "seen": row["seen"] or 0,
        "heard": row["heard"] or 0,
        "unconfirmed": pending,
    }


# How many of a species' newest camera sightings the hero picker gets to choose from.
# Enough to skip past a run of subject-only sightings without a stored crop; small enough
# that the species index (fifty-odd species) stays one cheap query.
HERO_CANDIDATES = 8


def species_heroes(names: list[str], start: Optional[float] = None,
                   end: Optional[float] = None) -> dict[str, list[dict]]:
    """Per species, the camera sightings a hero image may come from — newest first.

    The ``HERO_CANDIDATES`` newest Frigate sightings of each species, plus every one that
    is kept (the user's 📌 or a keepsake) because a kept event's media is the one thing
    Frigate is guaranteed to still have. Rows carry ``subject_idx`` so the picker (see
    ``heroes.pick``) can prefer the bird's OWN crop: a species that was only "also in
    view" in another bird's event inherits that event's snapshot here, and showing that
    would be showing the other bird. ``start``/``end`` bound the sightings (the recap's
    window); kept rows are only included when they fall inside it too.
    """
    if not names:
        return {}
    marks = ",".join("?" * len(names))
    window = ""
    params: list = list(names)
    if start is not None:
        window += " AND d.start_time >= ?"
        params.append(start)
    if end is not None:
        window += " AND d.start_time < ?"
        params.append(end)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            WITH ranked AS (
                SELECT d.common_name, d.id, d.source_ref, d.subject_idx, d.start_time,
                       d.has_snapshot, d.retained_at,
                       EXISTS (SELECT 1 FROM species_keepsakes k
                               WHERE k.detection_id = d.id) AS is_keepsake,
                       ROW_NUMBER() OVER (PARTITION BY d.common_name
                                          ORDER BY d.start_time DESC, d.subject_idx) AS rn
                FROM species_sightings d
                WHERE d.source = 'frigate' AND d.common_name IN ({marks}){window}
            )
            SELECT * FROM ranked
            WHERE rn <= {int(HERO_CANDIDATES)} OR retained_at IS NOT NULL OR is_keepsake
            ORDER BY common_name, start_time DESC, subject_idx
            """,
            params,
        ).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["common_name"], []).append(dict(r))
    return out


def scientific_name_for(common_name: str) -> Optional[str]:
    """Latest known scientific name for a species (case-insensitive; any source)."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT scientific_name FROM species_sightings detections
            WHERE common_name = ? COLLATE NOCASE
              AND scientific_name IS NOT NULL AND scientific_name != ''
            ORDER BY start_time DESC LIMIT 1
            """,
            (common_name,),
        ).fetchone()
    return row["scientific_name"] if row else None


def canonical_species(name: str) -> Optional[dict]:
    """Canonical ``{common_name, scientific_name}`` for an incoming species label.

    Handles cross-source naming drift: a label that matches another species'
    scientific name maps to that species' common name (Frigate's classifier emits
    scientific names while BirdNET-Go emits common names); otherwise a
    case-insensitive match adopts the spelling already stored, preferring rows that
    carry a scientific name (BirdNET's proper-cased entries). None when unknown.
    """
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT common_name, scientific_name FROM detections
            WHERE scientific_name = ? COLLATE NOCASE
              AND common_name != ''
              AND LOWER(common_name) != LOWER(scientific_name)
            ORDER BY start_time DESC LIMIT 1
            """,
            (name,),
        ).fetchone()
        if row is None:
            row = conn.execute(
                """
                SELECT common_name, scientific_name FROM detections
                WHERE common_name = ? COLLATE NOCASE
                ORDER BY (scientific_name IS NOT NULL) DESC, start_time DESC
                LIMIT 1
                """,
                (name,),
            ).fetchone()
    return dict(row) if row else None


# A Frigate fragment not yet linked to a visit that started within this many seconds of
# another is the same stay (Frigate's review cutoff is 30 s): a burst of tracked objects
# whose review message has not arrived yet must not read as "last seen 4 seconds ago".
_UNLINKED_SIBLING_S = 60.0


def species_last_times(common_name: str, source: str, source_ref: str,
                       visit_id: Optional[int] = None,
                       start_time: Optional[float] = None) -> dict:
    """The species' most recent detection time — overall and per source — excluding
    one row (already upserted), every other member of its visit, and any unlinked,
    not-yet-announced Frigate fragment within a minute of it.

    Feeds the notification blueprint's per-species cooldown: 'how long has this
    species been quiet before this detection?', split by source so a camera cooldown
    isn't fed by audio detections (and vice versa). Siblings in the same visit are the
    same stay, not a previous one — without excluding them, the second tracked object of
    one visit would report the species as "last seen 4 seconds ago"; a sibling whose
    review link has not arrived yet is excluded by time instead — but only while it is
    unannounced (still being identified), so it cannot swallow the stay's one real
    notification. Once a fragment HAS announced it counts: every later fragment of the
    burst then reads "seconds ago" and the blueprint's cooldown drops it. (0.36.0 excluded
    announced fragments too, so each one read the gap to the previous stay and a burst of
    four fragments sent four notifications.) Reads the sightings
    view, so a species known only as an *other bird in view* has a last-seen time and
    is not "first sighting" forever.
    """
    empty = {"any": None, "seen": None, "heard": None}
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT MAX(start_time) AS any_t,
                   MAX(CASE WHEN source = 'frigate' THEN start_time END) AS seen_t,
                   MAX(CASE WHEN source = 'birdnet' THEN start_time END) AS heard_t
            FROM species_sightings
            WHERE common_name = ? COLLATE NOCASE
              AND NOT (source = ? AND source_ref = ?)
              AND (? IS NULL OR visit_id IS NULL OR visit_id != ?)
              AND NOT (source = 'frigate' AND visit_id IS NULL AND announced_at IS NULL
                       AND ? IS NOT NULL AND ABS(start_time - ?) <= ?)
            """,
            (common_name, source, source_ref, visit_id, visit_id,
             start_time, start_time, _UNLINKED_SIBLING_S),
        ).fetchone()
    if not row:
        return empty
    return {"any": row["any_t"], "seen": row["seen_t"], "heard": row["heard_t"]}
