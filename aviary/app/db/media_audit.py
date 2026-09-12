"""Frigate media audit: expiry marks, crop flags, tombstones."""

from __future__ import annotations
import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from ._conn import _connect

__all__ = [
    "frigate_media_rows",
    "recent_frigate_count",
    "apply_media_audit",
    "set_has_crop",
    "mark_crops_present",
    "tombstoned_refs",
]


# ---------------------------------------------------------------------- media audit

def frigate_media_rows(before: float) -> list[dict]:
    """Every finished Frigate event older than ``before`` with what we believe about its
    media — the audit's worklist. Slim rows; a large history is still a few MB."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, source_ref, has_clip, has_snapshot, media_expired_at
            FROM detections
            WHERE source = 'frigate' AND end_time IS NOT NULL AND start_time < ?
            """,
            (before,),
        ).fetchall()
    return [dict(r) for r in rows]


def recent_frigate_count(since: float) -> int:
    """Frigate rows newer than ``since`` — the audit's sanity check against an empty
    listing (Frigate that reports no bird events while we ingested some today is not
    to be believed)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM detections WHERE source = 'frigate' AND start_time >= ?",
            (since,),
        ).fetchone()
    return int(row["n"] or 0)


def apply_media_audit(expired_ids: list[int], synced: dict[int, tuple[int, int]],
                      restored: dict[int, tuple[int, int]]) -> None:
    """One transaction: zero the flags and stamp the expired rows; sync flags on rows
    Frigate still has (partial expiry); clear the stamp on rows Frigate lists with media
    again. ``synced``/``restored`` map id -> (has_clip, has_snapshot) as Frigate says."""
    now = time.time()
    with _connect() as conn:
        if expired_ids:
            conn.executemany(
                """
                UPDATE detections SET has_clip = 0, has_snapshot = 0,
                       media_expired_at = COALESCE(media_expired_at, ?)
                WHERE id = ?
                """,
                [(now, i) for i in expired_ids],
            )
        if synced:
            conn.executemany(
                "UPDATE detections SET has_clip = ?, has_snapshot = ? WHERE id = ?",
                [(int(c), int(s), i) for i, (c, s) in synced.items()],
            )
        if restored:
            conn.executemany(
                """
                UPDATE detections SET has_clip = ?, has_snapshot = ?, media_expired_at = NULL
                WHERE id = ?
                """,
                [(int(c), int(s), i) for i, (c, s) in restored.items()],
            )


def set_has_crop(source_ref: str, present: bool = True) -> None:
    """Record that the identifier stored (or a caller removed) this event's own crop."""
    with _connect() as conn:
        conn.execute(
            "UPDATE detections SET has_crop = ? WHERE source = 'frigate' AND source_ref = ?",
            (1 if present else 0, source_ref),
        )


def mark_crops_present(refs: list[str]) -> int:
    """One-time catch-up for rows that predate the has_crop column: flag every event
    whose primary crop file exists. Returns how many rows were flagged."""
    if not refs:
        return 0
    total = 0
    with _connect() as conn:
        for i in range(0, len(refs), 500):
            chunk = refs[i:i + 500]
            marks = ",".join("?" * len(chunk))
            cur = conn.execute(
                f"UPDATE detections SET has_crop = 1 WHERE source = 'frigate' AND has_crop = 0"
                f" AND source_ref IN ({marks})",
                chunk,
            )
            total += cur.rowcount or 0
    return total


def tombstoned_refs() -> list[tuple[str, str]]:
    """All (source, source_ref) pairs the user has deleted."""
    with _connect() as conn:
        rows = conn.execute("SELECT source, source_ref FROM deleted_refs").fetchall()
    return [(r["source"], r["source_ref"]) for r in rows]
