"""SQLite storage + analytics queries for Aviary.

A single ``detections`` table holds rows from both sources, tagged by ``source``.
There is no cross-source correlation. Connections are opened per-operation (SQLite in
WAL mode handles concurrent readers/one writer well), which keeps the MQTT ingest thread
and the FastAPI event loop cleanly separated.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS detections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT    NOT NULL,               -- 'frigate' | 'birdnet'
    source_ref      TEXT    NOT NULL,               -- frigate event id / birdnet detection id
    common_name     TEXT    NOT NULL DEFAULT 'bird',
    scientific_name TEXT,
    species_code    TEXT,
    confidence      REAL,
    location        TEXT,                            -- frigate camera / birdnet source node
    start_time      REAL    NOT NULL,               -- epoch seconds
    end_time        REAL,
    has_clip        INTEGER NOT NULL DEFAULT 0,
    has_snapshot    INTEGER NOT NULL DEFAULT 0,
    clip_ref        TEXT,
    snapshot_ref    TEXT,
    native_id       TEXT,                            -- source-side id (birdnet DB id / frigate event id)
    raw_json        TEXT,
    created_at      REAL    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_detections_source_ref
    ON detections (source, source_ref);
CREATE INDEX IF NOT EXISTS idx_detections_start_time ON detections (start_time);
CREATE INDEX IF NOT EXISTS idx_detections_common_name ON detections (common_name);

-- Cached per-species reference info (Wikipedia blurb + iNaturalist taxonomy).
CREATE TABLE IF NOT EXISTS species_info (
    common_name     TEXT PRIMARY KEY,
    scientific_name TEXT,
    descriptor      TEXT,      -- short one-liner, e.g. "Species of North American bird"
    extract         TEXT,      -- blurb paragraph
    wiki_url        TEXT,
    family          TEXT,
    "order"         TEXT,
    conservation    TEXT,
    fetched_at      REAL,
    ok              INTEGER NOT NULL DEFAULT 0
);
"""

_db_path: str = ""


def init_db(db_path: str) -> None:
    """Configure the module and create the schema."""
    global _db_path
    _db_path = db_path
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.executescript(_SCHEMA)
        # Additive migrations for databases created by older versions.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(detections)")}
        if "native_id" not in cols:
            conn.execute("ALTER TABLE detections ADD COLUMN native_id TEXT")


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(_db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_detection(row: dict[str, Any]) -> None:
    """Insert a detection, or update it if (source, source_ref) already exists.

    Frigate sends multiple messages per event (new/update/end); the latest wins so the
    final species and confidence are kept. COALESCE preserves any media flags/refs that
    were already captured but are absent from a later message.
    """
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO detections (
                source, source_ref, common_name, scientific_name, species_code,
                confidence, location, start_time, end_time,
                has_clip, has_snapshot, clip_ref, snapshot_ref, native_id,
                raw_json, created_at
            ) VALUES (
                :source, :source_ref, :common_name, :scientific_name, :species_code,
                :confidence, :location, :start_time, :end_time,
                :has_clip, :has_snapshot, :clip_ref, :snapshot_ref, :native_id,
                :raw_json, :created_at
            )
            ON CONFLICT(source, source_ref) DO UPDATE SET
                -- Never downgrade a classified species back to generic 'bird' when a
                -- later message arrives without a sub_label.
                common_name     = CASE
                                      WHEN excluded.common_name = 'bird'
                                           AND detections.common_name != 'bird'
                                      THEN detections.common_name
                                      ELSE excluded.common_name
                                  END,
                scientific_name = COALESCE(excluded.scientific_name, detections.scientific_name),
                species_code    = COALESCE(excluded.species_code, detections.species_code),
                -- Scalar MAX() is NULL if either side is NULL; keep the best non-NULL score.
                confidence      = COALESCE(
                                      MAX(detections.confidence, excluded.confidence),
                                      detections.confidence, excluded.confidence
                                  ),
                location        = excluded.location,
                end_time        = COALESCE(excluded.end_time, detections.end_time),
                has_clip        = MAX(detections.has_clip, excluded.has_clip),
                has_snapshot    = MAX(detections.has_snapshot, excluded.has_snapshot),
                clip_ref        = COALESCE(excluded.clip_ref, detections.clip_ref),
                snapshot_ref    = COALESCE(excluded.snapshot_ref, detections.snapshot_ref),
                native_id       = COALESCE(excluded.native_id, detections.native_id),
                raw_json        = excluded.raw_json
            """,
            row,
        )


# --------------------------------------------------------------------------- queries

def _source_clause(source: Optional[str], params: list) -> str:
    if source in ("frigate", "birdnet"):
        params.append(source)
        return " AND source = ?"
    return ""


def recent_detections(
    limit: int = 60,
    source: Optional[str] = None,
    species: Optional[str] = None,
    before: Optional[float] = None,
    since: Optional[float] = None,
) -> list[dict]:
    """Newest-first detections. ``before`` is a start_time cursor for pagination."""
    params: list = []
    where = "WHERE 1=1"
    where += _source_clause(source, params)
    if species:
        where += " AND common_name = ?"
        params.append(species)
    if before is not None:
        where += " AND start_time < ?"
        params.append(before)
    if since is not None:
        where += " AND start_time >= ?"
        params.append(since)
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM detections {where} ORDER BY start_time DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def change_marker(source: Optional[str] = None, species: Optional[str] = None) -> dict:
    """Cheap poll target: row count + newest start_time for the given filters."""
    params: list = []
    where = "WHERE 1=1" + _source_clause(source, params)
    if species:
        where += " AND common_name = ?"
        params.append(species)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS total, MAX(start_time) AS newest FROM detections {where}",
            params,
        ).fetchone()
    return dict(row) if row else {"total": 0, "newest": None}


def species_stats(name: str) -> dict:
    """Aggregate stats for one species (all-time)."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*)                AS total,
                MIN(start_time)         AS first_seen,
                MAX(start_time)         AS last_seen,
                MAX(confidence)         AS best_confidence,
                SUM(source = 'frigate') AS frigate_total,
                SUM(source = 'birdnet') AS birdnet_total,
                MIN(CASE WHEN source = 'frigate' THEN start_time END) AS first_frigate,
                MAX(CASE WHEN source = 'frigate' THEN start_time END) AS last_frigate,
                MIN(CASE WHEN source = 'birdnet' THEN start_time END) AS first_birdnet,
                MAX(CASE WHEN source = 'birdnet' THEN start_time END) AS last_birdnet,
                MAX(scientific_name)    AS scientific_name
            FROM detections WHERE common_name = ?
            """,
            (name,),
        ).fetchone()
    return dict(row) if row else {}


def new_species_count(source: Optional[str] = None, since: Optional[float] = None) -> int:
    """Number of species whose *first-ever* detection falls at/after ``since``.

    With ``since`` None (all-time), every species is "new", so this returns the distinct
    species count.
    """
    params: list = []
    src = _source_clause(source, params)
    if since is None:
        with _connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(DISTINCT common_name) AS c FROM detections WHERE 1=1{src}",
                params,
            ).fetchone()
        return row["c"] if row else 0
    params.append(since)
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS c FROM (
                SELECT common_name FROM detections WHERE 1=1{src}
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
) -> list[dict]:
    """Per-species aggregates for the species index.

    ``only_new`` keeps just species whose first-ever detection is at/after ``since``
    (first_seen/count are all-time). Otherwise, when ``since`` is given, results are
    limited to species active within the window (count is the in-window count).
    """
    params: list = []
    where = "WHERE 1=1" + _source_clause(source, params)
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
               COUNT(*)             AS count,
               MIN(start_time)      AS first_seen,
               MAX(start_time)      AS last_seen,
               SUM(source = 'frigate') AS frigate_total,
               SUM(source = 'birdnet') AS birdnet_total,
               MIN(CASE WHEN source = 'frigate' THEN start_time END) AS first_frigate,
               MAX(CASE WHEN source = 'frigate' THEN start_time END) AS last_frigate,
               MIN(CASE WHEN source = 'birdnet' THEN start_time END) AS first_birdnet,
               MAX(CASE WHEN source = 'birdnet' THEN start_time END) AS last_birdnet
        FROM detections {where}
        GROUP BY common_name{having}
        ORDER BY {order}
    """
    if only_new and since is not None:
        params.append(since)
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def distinct_species(source: Optional[str] = None) -> list[str]:
    params: list = []
    where = "WHERE 1=1" + _source_clause(source, params)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT common_name FROM detections {where} ORDER BY common_name",
            params,
        ).fetchall()
    return [r["common_name"] for r in rows]


def latest_snapshot_refs(names: list[str]) -> dict[str, str]:
    """Newest Frigate snapshot event-ref per species, for thumbnails."""
    if not names:
        return {}
    marks = ",".join("?" * len(names))
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT common_name, snapshot_ref, MAX(start_time)
            FROM detections
            WHERE source = 'frigate' AND has_snapshot = 1 AND snapshot_ref IS NOT NULL
              AND common_name IN ({marks})
            GROUP BY common_name
            """,
            names,
        ).fetchall()
    return {r["common_name"]: r["snapshot_ref"] for r in rows}


def scientific_name_for(common_name: str) -> Optional[str]:
    """Latest known scientific name for a species (case-insensitive; any source)."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT scientific_name FROM detections
            WHERE common_name = ? COLLATE NOCASE
              AND scientific_name IS NOT NULL AND scientific_name != ''
            ORDER BY start_time DESC LIMIT 1
            """,
            (common_name,),
        ).fetchone()
    return row["scientific_name"] if row else None


def detection_by_id(det_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM detections WHERE id = ?", (det_id,)).fetchone()
    return dict(row) if row else None


def detection_by_ref(source: str, source_ref: str) -> Optional[dict]:
    """Current row for an upsert key — later source messages update the same row."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM detections WHERE source = ? AND source_ref = ?",
            (source, source_ref),
        ).fetchone()
    return dict(row) if row else None


def get_species_info(common_name: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM species_info WHERE common_name = ?", (common_name,)
        ).fetchone()
    return dict(row) if row else None


def put_species_info(row: dict[str, Any]) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO species_info (
                common_name, scientific_name, descriptor, extract, wiki_url,
                family, "order", conservation, fetched_at, ok
            ) VALUES (
                :common_name, :scientific_name, :descriptor, :extract, :wiki_url,
                :family, :order, :conservation, :fetched_at, :ok
            )
            ON CONFLICT(common_name) DO UPDATE SET
                scientific_name = excluded.scientific_name,
                descriptor      = excluded.descriptor,
                extract         = excluded.extract,
                wiki_url        = excluded.wiki_url,
                family          = excluded.family,
                "order"         = excluded."order",
                conservation    = excluded.conservation,
                fetched_at      = excluded.fetched_at,
                ok              = excluded.ok
            """,
            row,
        )


def summary_stats(source: Optional[str] = None, since: Optional[float] = None) -> dict:
    params: list = []
    where = "WHERE 1=1"
    where += _source_clause(source, params)
    if since is not None:
        where += " AND start_time >= ?"
        params.append(since)
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT
                COUNT(*)                     AS total,
                COUNT(DISTINCT common_name)  AS species,
                SUM(source = 'frigate')      AS frigate_total,
                SUM(source = 'birdnet')      AS birdnet_total
            FROM detections {where}
            """,
            params,
        ).fetchone()
    return dict(row) if row else {}


def top_species(
    limit: int = 10,
    source: Optional[str] = None,
    since: Optional[float] = None,
) -> list[dict]:
    params: list = []
    where = "WHERE 1=1"
    where += _source_clause(source, params)
    if since is not None:
        where += " AND start_time >= ?"
        params.append(since)
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT common_name, scientific_name,
                   COUNT(*) AS count, MAX(start_time) AS last_seen,
                   SUM(source = 'birdnet') AS heard,
                   SUM(source = 'frigate') AS seen,
                   MAX(CASE WHEN source = 'frigate' THEN start_time END) AS last_frigate,
                   MAX(CASE WHEN source = 'birdnet' THEN start_time END) AS last_birdnet
            FROM detections {where}
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
                   COUNT(*) AS count
            FROM detections {where}
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
                   COUNT(*) AS count
            FROM detections {where}
            GROUP BY hour
            ORDER BY hour
            """,
            params,
        ).fetchall()
    counts = {int(r["hour"]): r["count"] for r in rows}
    return [{"hour": h, "count": counts.get(h, 0)} for h in range(24)]
