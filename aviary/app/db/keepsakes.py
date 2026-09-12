"""Species keepsakes (first/latest sightings) and the sightings timeline behind them."""

from __future__ import annotations
import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from ._conn import _connect

__all__ = [
    "_SIGHTING_COLS",
    "_SIGHTING_WHERE",
    "oldest_sighting",
    "newest_sighting",
    "sightings_timeline",
    "sighting_by_detection",
    "keepsakes_for_species",
    "keepsake_roles",
    "keepsakes_all",
    "keepsake",
    "set_keepsake",
    "set_keepsake_exports",
    "set_keepsake_export_file",
    "clear_keepsake_export",
    "clear_keepsake",
    "clear_keepsakes_for_detections",
    "forget_keepsakes",
    "keepsake_species",
]


# ------------------------------------------------------------------------ keepsakes

# One camera sighting of a species, as the keepsake logic sees it: the parent event's
# row fields plus which bird in it (subject_idx) the species is. From the sightings view,
# so a species named "also in view" counts — its event is what gets kept.
_SIGHTING_COLS = """
    d.id, d.source_ref, d.subject_idx, d.start_time, d.end_time, d.location,
    d.has_clip, d.has_snapshot, d.retained_at, d.common_name
"""


# Only events Frigate itself kept a record of: one that ended with neither clip nor
# snapshot is gone from Frigate moments later and could never be retained.
_SIGHTING_WHERE = "d.source = 'frigate' AND (d.has_clip = 1 OR d.has_snapshot = 1)"


def oldest_sighting(common_name: str) -> Optional[dict]:
    """The species' earliest camera sighting on record, or None."""
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT {_SIGHTING_COLS} FROM species_sightings d
            WHERE {_SIGHTING_WHERE} AND d.common_name = ? COLLATE NOCASE
            ORDER BY d.start_time ASC, d.subject_idx ASC LIMIT 1
            """,
            (common_name,),
        ).fetchone()
    return dict(row) if row else None


def newest_sighting(common_name: str) -> Optional[dict]:
    """The species' most recent camera sighting on record, or None."""
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT {_SIGHTING_COLS} FROM species_sightings d
            WHERE {_SIGHTING_WHERE} AND d.common_name = ? COLLATE NOCASE
            ORDER BY d.start_time DESC, d.subject_idx ASC LIMIT 1
            """,
            (common_name,),
        ).fetchone()
    return dict(row) if row else None


def sightings_timeline(common_name: str) -> list[dict]:
    """Every camera sighting of a species, oldest first — the surviving-first search's
    haystack. Slim rows (id, source_ref, subject_idx, start_time); a busy feeder's
    commonest bird is thousands of rows, which is still small in memory."""
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT d.id, d.source_ref, d.subject_idx, d.start_time FROM species_sightings d
            WHERE {_SIGHTING_WHERE} AND d.common_name = ? COLLATE NOCASE
            ORDER BY d.start_time ASC, d.subject_idx ASC
            """,
            (common_name,),
        ).fetchall()
    return [dict(r) for r in rows]


def sighting_by_detection(det_id: int, common_name: str) -> Optional[dict]:
    """The sighting row for one event AS a given species (None if the event no longer
    features that species — deleted, relabelled, or the subject rejected)."""
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT {_SIGHTING_COLS} FROM species_sightings d
            WHERE {_SIGHTING_WHERE} AND d.id = ? AND d.common_name = ? COLLATE NOCASE
            ORDER BY d.subject_idx ASC LIMIT 1
            """,
            (det_id, common_name),
        ).fetchone()
    return dict(row) if row else None


def keepsakes_for_species(common_name: str) -> dict[str, dict]:
    """{'first': row, 'latest': row} — whichever exist — for one species."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM species_keepsakes WHERE common_name = ? COLLATE NOCASE",
            (common_name,),
        ).fetchall()
    return {r["role"]: dict(r) for r in rows}


def keepsake_roles(detection_id: int) -> list[str]:
    """Which keepsake roles ('first', 'latest') this event holds, for any species.

    Template helper (one indexed lookup per Frigate card) and the release guard: an
    event that is still someone's keepsake keeps its Frigate flag when the user unpins.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT role FROM species_keepsakes WHERE detection_id = ? ORDER BY role",
            (detection_id,),
        ).fetchall()
    return [r["role"] for r in rows]


def keepsakes_all() -> list[dict]:
    """Every keepsake with its event's fields, species A→Z then first before latest —
    the Kept page's shelf. LEFT JOIN so a keepsake whose event vanished (a race with a
    delete) still lists and can be cleaned up rather than silently disappearing."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT k.*, d.source_ref, d.location, d.end_time, d.has_clip, d.has_snapshot,
                   d.scientific_name, d.retained_at,
                   COALESCE(s.manual_sci, s.scientific_name) AS subject_sci
            FROM species_keepsakes k
            LEFT JOIN detections d ON d.id = k.detection_id
            LEFT JOIN detection_subjects s
                   ON s.detection_id = k.detection_id AND s.idx = k.subject_idx
                  AND k.subject_idx > 0
            ORDER BY k.common_name COLLATE NOCASE ASC,
                     CASE k.role WHEN 'first' THEN 0 ELSE 1 END
            """
        ).fetchall()
    return [dict(r) for r in rows]


def keepsake(common_name: str, role: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM species_keepsakes WHERE common_name = ? COLLATE NOCASE AND role = ?",
            (common_name, role),
        ).fetchone()
    return dict(row) if row else None


def set_keepsake(common_name: str, role: str, detection_id: int, subject_idx: int,
                 start_time: float) -> None:
    """Point a species' role at an event (replacing whatever held it). Export columns
    reset: a new event needs its own exports."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO species_keepsakes
                (common_name, role, detection_id, subject_idx, start_time, kept_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(common_name, role) DO UPDATE SET
                detection_id = excluded.detection_id, subject_idx = excluded.subject_idx,
                start_time = excluded.start_time, kept_at = excluded.kept_at,
                export_id = NULL, export_file = NULL, zoom_export_id = NULL,
                zoom_export_file = NULL, export_status = NULL, export_tried_at = NULL
            """,
            (common_name, role, detection_id, int(subject_idx or 0), start_time, time.time()),
        )


def set_keepsake_exports(common_name: str, role: str, export_id: Optional[str],
                         zoom_export_id: Optional[str], status: Optional[str]) -> None:
    """Record the outcome of the export attempt(s) for a keepsake. ``export_tried_at``
    is stamped regardless, so a window Frigate no longer has recordings for is not
    re-requested on every reconcile — only by the startup pass."""
    with _connect() as conn:
        conn.execute(
            """
            UPDATE species_keepsakes
            SET export_id = ?, zoom_export_id = ?, export_status = ?, export_tried_at = ?,
                export_file = NULL, zoom_export_file = NULL
            WHERE common_name = ? COLLATE NOCASE AND role = ?
            """,
            (export_id, zoom_export_id, status, time.time(), common_name, role),
        )


def set_keepsake_export_file(common_name: str, role: str, zoom: bool, file: Optional[str]) -> None:
    """Fill in (or clear) a keepsake export's served filename once Frigate finished it."""
    col = "zoom_export_file" if zoom else "export_file"
    with _connect() as conn:
        conn.execute(
            f"UPDATE species_keepsakes SET {col} = ? WHERE common_name = ? COLLATE NOCASE AND role = ?",
            (file, common_name, role),
        )


def clear_keepsake_export(common_name: str, role: str, zoom: bool) -> None:
    """Forget an export Frigate no longer has (deleted in its Export UI, or a failed job)."""
    cols = ("zoom_export_id = NULL, zoom_export_file = NULL" if zoom
            else "export_id = NULL, export_file = NULL")
    with _connect() as conn:
        conn.execute(
            f"UPDATE species_keepsakes SET {cols} WHERE common_name = ? COLLATE NOCASE AND role = ?",
            (common_name, role),
        )


def clear_keepsake(common_name: str, role: str) -> Optional[dict]:
    """Drop one keepsake row; returns it so the caller can release it at Frigate."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM species_keepsakes WHERE common_name = ? COLLATE NOCASE AND role = ?",
            (common_name, role),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "DELETE FROM species_keepsakes WHERE common_name = ? COLLATE NOCASE AND role = ?",
            (common_name, role),
        )
    return dict(row)


def clear_keepsakes_for_detections(conn: sqlite3.Connection, det_ids: list[int]) -> list[dict]:
    """Remove every keepsake pointing at these events (same transaction as the delete
    that orphaned them). Returns the removed rows: their exports still exist at Frigate
    and the caller decides what to release."""
    if not det_ids:
        return []
    marks = ",".join("?" * len(det_ids))
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM species_keepsakes WHERE detection_id IN ({marks})", det_ids,
    ).fetchall()]
    if rows:
        conn.execute(f"DELETE FROM species_keepsakes WHERE detection_id IN ({marks})", det_ids)
    return rows


def forget_keepsakes(common_name: str) -> list[dict]:
    """Drop a species' keepsakes (the species itself is being removed). Returns them."""
    with _connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM species_keepsakes WHERE common_name = ? COLLATE NOCASE",
            (common_name,),
        ).fetchall()]
        conn.execute("DELETE FROM species_keepsakes WHERE common_name = ? COLLATE NOCASE",
                     (common_name,))
    return rows


def keepsake_species() -> list[str]:
    """Species that hold at least one keepsake."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT common_name FROM species_keepsakes ORDER BY common_name"
        ).fetchall()
    return [r["common_name"] for r in rows]
