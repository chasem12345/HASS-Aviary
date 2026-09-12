"""The detections table: upserts from ingest, row fetches, deletes, retain flags, the flat recent feed."""

from __future__ import annotations
import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from ._conn import _connect
from ._sql import _source_clause
from .keepsakes import clear_keepsakes_for_detections
from .subjects import _delete_subjects, _reject_named_subjects
from .visits import _refresh_visit, _settle_visits, _visit_ids_of

__all__ = [
    "upsert_detection",
    "recent_detections",
    "distinct_zones",
    "change_marker",
    "retained_detections",
    "detection_by_id",
    "detection_by_ref",
    "delete_detection",
    "delete_species",
    "overlapping_detections",
    "set_retained",
    "set_kept_export",
    "set_kept_export_file",
    "retained_missing_export",
    "oldest_detection",
    "recent_refs",
]


def upsert_detection(row: dict[str, Any]) -> None:
    """Insert a detection, or update it if (source, source_ref) already exists.

    Frigate sends multiple messages per event (new/update/end); the latest wins so the
    final species and confidence are kept. COALESCE preserves any media flags/refs that
    were already captured but are absent from a later message.

    Side effect on the caller's dict: ``row["id"]`` is filled in, and for a Frigate event
    that a review item has claimed, ``row["visit_id"]`` too — the notification path keys
    its once-per-visit dedupe on it, and the tap-action deep link needs the id.
    """
    # Only Frigate rows carry a zone; default it so BirdNET's row shape (and any older
    # caller) satisfies the named parameters without every builder growing the key.
    defaults = {"zone": None}
    params = {**defaults, **row}
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO detections (
                source, source_ref, common_name, scientific_name, species_code,
                confidence, location, zone, start_time, end_time,
                has_clip, has_snapshot, clip_ref, snapshot_ref, native_id,
                raw_json, created_at
            ) VALUES (
                :source, :source_ref, :common_name, :scientific_name, :species_code,
                :confidence, :location, :zone, :start_time, :end_time,
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
                -- COALESCE, not last-write-wins: entered_zones is cumulative so the end
                -- message carries the full list, but a message WITHOUT zones (or a
                -- BirdNET row shape) must not wipe one already recorded.
                zone            = COALESCE(excluded.zone, detections.zone),
                end_time        = COALESCE(excluded.end_time, detections.end_time),
                has_clip        = MAX(detections.has_clip, excluded.has_clip),
                has_snapshot    = MAX(detections.has_snapshot, excluded.has_snapshot),
                clip_ref        = COALESCE(excluded.clip_ref, detections.clip_ref),
                snapshot_ref    = COALESCE(excluded.snapshot_ref, detections.snapshot_ref),
                native_id       = COALESCE(excluded.native_id, detections.native_id),
                raw_json        = excluded.raw_json
            """,
            params,
        )
        stored = conn.execute(
            "SELECT id, visit_id FROM detections WHERE source = ? AND source_ref = ?",
            (params["source"], params["source_ref"]),
        ).fetchone()
        if stored is None:  # cannot happen after a successful upsert; be defensive
            return
        row["id"] = stored["id"]
        visit_id = stored["visit_id"]
        if params["source"] == "frigate":
            if visit_id is None:
                # The review message may have listed this event before its own first
                # message arrived (or before Aviary stored it) — pick the link up now.
                ref = conn.execute(
                    "SELECT visit_id FROM visit_refs WHERE source_ref = ?",
                    (params["source_ref"],),
                ).fetchone()
                if ref is not None:
                    visit_id = ref["visit_id"]
                    conn.execute("UPDATE detections SET visit_id = ? WHERE id = ?",
                                 (visit_id, stored["id"]))
            if visit_id is not None:
                # A later message can extend the event's end_time past the review item's.
                _refresh_visit(conn, visit_id)
        row["visit_id"] = visit_id


def recent_detections(
    limit: int = 60,
    source: Optional[str] = None,
    species: Optional[str] = None,
    before: Optional[float] = None,
    since: Optional[float] = None,
    zone: Optional[str] = None,
) -> list[dict]:
    """Newest-first detections. ``before`` is a start_time cursor for pagination."""
    params: list = []
    where = "WHERE 1=1"
    where += _source_clause(source, params)
    if species:
        where += " AND common_name = ?"
        params.append(species)
    if zone:
        # zone is comma-joined for multi-zone events ("bird_bath, porch"); match one
        # whole element, delimiter-wrapped, so "bath" cannot match "bird_bath".
        where += " AND ',' || REPLACE(zone, ' ', '') || ',' LIKE '%,' || ? || ',%'"
        params.append(zone.replace(" ", ""))
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


def distinct_zones() -> list[str]:
    """Individual zone names seen so far, for the Recent page's filter dropdown.

    Rows store comma-joined combinations ("bird_bath, porch"); the dropdown wants the
    individual zones, so split and dedupe here rather than in SQL.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT zone FROM detections WHERE zone IS NOT NULL AND zone != ''"
        ).fetchall()
    zones = {z.strip() for (combo,) in rows for z in combo.split(",") if z.strip()}
    return sorted(zones)


def change_marker(source: Optional[str] = None, species: Optional[str] = None) -> dict:
    """Cheap poll target: row count + newest start_time for the given filters."""
    params: list = []
    where = "WHERE 1=1" + _source_clause(source, params)
    if species:
        where += " AND common_name = ?"
        params.append(species)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS total, MAX(start_time) AS newest FROM species_sightings detections {where}",
            params,
        ).fetchone()
    return dict(row) if row else {"total": 0, "newest": None}


def retained_detections() -> list[dict]:
    """Every kept-forever detection, for the Kept catalogue.

    Ordered species A→Z then newest first, so the caller can group by walking the list
    once. No confirmation gating on purpose: the user pinned this exact footage, which
    outranks whether the species has been approved into the registry yet.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM detections
            WHERE retained_at IS NOT NULL
            ORDER BY common_name COLLATE NOCASE ASC, start_time DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


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


def delete_detection(det_id: int) -> Optional[dict]:
    """Delete one detection and tombstone its ref. Returns the deleted row.

    The row carries ``keepsakes`` — any keepsake rows that pointed at it, removed in the
    same transaction — so the API can release their exports and re-pick the species'
    first/latest.
    """
    with _connect() as conn:
        row = conn.execute("SELECT * FROM detections WHERE id = ?", (det_id,)).fetchone()
        if row is None:
            return None
        visit_ids = _visit_ids_of(conn, [det_id])
        conn.execute("DELETE FROM detections WHERE id = ?", (det_id,))
        # Explicit rather than relying on ON DELETE CASCADE: SQLite enforces foreign keys
        # only when PRAGMA foreign_keys is on, and it is off by default on every new
        # connection. Orphaned embeddings would otherwise accumulate silently forever.
        conn.execute("DELETE FROM identification_embeddings WHERE detection_id = ?", (det_id,))
        conn.execute("DELETE FROM identification_rejections WHERE detection_id = ?", (det_id,))
        _delete_subjects(conn, [det_id])
        keepsakes = clear_keepsakes_for_detections(conn, [det_id])
        conn.execute(
            "INSERT OR REPLACE INTO deleted_refs (source, source_ref, deleted_at) VALUES (?, ?, ?)",
            (row["source"], row["source_ref"], time.time()),
        )
        _settle_visits(conn, visit_ids)
    out = dict(row)
    out["keepsakes"] = keepsakes
    return out


def delete_species(common_name: str) -> list[dict]:
    """Delete every detection of a species (case-insensitive), tombstoning each ref.

    Returns the deleted rows (the API uses their refs/native ids for source-side
    deletion).
    """
    with _connect() as conn:
        rows = [
            dict(r) for r in conn.execute(
                "SELECT * FROM detections WHERE common_name = ? COLLATE NOCASE",
                (common_name,),
            ).fetchall()
        ]
        if rows:
            now = time.time()
            visit_ids = _visit_ids_of(conn, [r["id"] for r in rows])
            conn.execute(
                "DELETE FROM detections WHERE common_name = ? COLLATE NOCASE",
                (common_name,),
            )
            _settle_visits(conn, visit_ids)
            conn.executemany(
                "INSERT OR REPLACE INTO deleted_refs (source, source_ref, deleted_at) VALUES (?, ?, ?)",
                [(r["source"], r["source_ref"], now) for r in rows],
            )
            # See the note in delete_detection: ON DELETE CASCADE is not enforced unless
            # PRAGMA foreign_keys is on, which it is not.
            conn.executemany(
                "DELETE FROM identification_embeddings WHERE detection_id = ?",
                [(r["id"],) for r in rows],
            )
            conn.executemany(
                "DELETE FROM identification_rejections WHERE detection_id = ?",
                [(r["id"],) for r in rows],
            )
            _delete_subjects(conn, [r["id"] for r in rows])
            # Other species' keepsakes may sit on these events too (a two-bird event
            # kept for the other bird); those are released by the API, not here.
            orphaned = clear_keepsakes_for_detections(conn, [r["id"] for r in rows])
            for r in rows:
                r["keepsakes"] = [k for k in orphaned if k["detection_id"] == r["id"]]
        # The species may also live on as a named other-bird in someone else's event —
        # those rows count as sightings, so a removal has to rule them out too.
        _reject_named_subjects(conn, common_name)
        # Its own keepsakes go with it (the API releases them at Frigate).
        conn.execute("DELETE FROM species_keepsakes WHERE common_name = ? COLLATE NOCASE",
                     (common_name,))
        conn.execute("DELETE FROM species_info WHERE common_name = ? COLLATE NOCASE", (common_name,))
        conn.execute("DELETE FROM species_season WHERE common_name = ? COLLATE NOCASE", (common_name,))
        conn.execute("DELETE FROM species_audio WHERE common_name = ? COLLATE NOCASE", (common_name,))
        conn.execute("DELETE FROM species_photos WHERE common_name = ? COLLATE NOCASE", (common_name,))
        # Drop the approval too, so a species removed as a misclassification queues for
        # review again if it genuinely turns up later.
        conn.execute("DELETE FROM species_confirmed WHERE common_name = ? COLLATE NOCASE", (common_name,))
    return rows


def overlapping_detections(source_ref: str, start: float, end: float) -> list[dict]:
    """Frigate detections whose time window overlaps [start, end], excluding one ref.

    Backs the zoom gate: another bird on camera during this event means the PTZ may have
    been filming it instead. A row with no end_time is an event still in progress and
    counts as overlapping.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT source_ref, zone, start_time, end_time FROM detections
            WHERE source = 'frigate' AND source_ref != ?
              AND start_time < ? AND (end_time IS NULL OR end_time > ?)
            """,
            (source_ref, end, start),
        ).fetchall()
    return [dict(r) for r in rows]


def set_retained(detection_id: int, retained: bool) -> None:
    """Record whether this event's clip is pinned kept-forever at Frigate."""
    with _connect() as conn:
        conn.execute(
            "UPDATE detections SET retained_at = ? WHERE id = ?",
            (time.time() if retained else None, detection_id),
        )


def set_kept_export(detection_id: int, export_id: Optional[str],
                    file: Optional[str] = None) -> None:
    """Record (or clear, with both None) the Frigate export of the zoomed window."""
    with _connect() as conn:
        conn.execute(
            "UPDATE detections SET kept_export_id = ?, kept_export_file = ? WHERE id = ?",
            (export_id, file, detection_id),
        )


def set_kept_export_file(detection_id: int, file: str) -> None:
    """Fill in the export's served filename once the async export has finished."""
    with _connect() as conn:
        conn.execute(
            "UPDATE detections SET kept_export_file = ? WHERE id = ?",
            (file, detection_id),
        )


def retained_missing_export() -> list[dict]:
    """Kept Frigate rows with no zoomed export yet — the one-time backfill's worklist.

    The pair check (does this camera have a partner?) is the caller's: camera pairing
    is configuration, not data.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM detections
            WHERE retained_at IS NOT NULL AND source = 'frigate'
              AND kept_export_id IS NULL
              AND start_time IS NOT NULL AND end_time IS NOT NULL
            """
        ).fetchall()
    return [dict(r) for r in rows]


def oldest_detection(common_name: str, source: str) -> Optional[dict]:
    """The species' earliest detections row from one source — the audio fallback for a
    species never seen on camera (``oldest_sighting`` is camera-only by design)."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM detections
            WHERE source = ? AND common_name = ? COLLATE NOCASE
            ORDER BY start_time ASC LIMIT 1
            """,
            (source, common_name),
        ).fetchone()
    return dict(row) if row else None


def recent_refs(since: float) -> list[tuple[str, str]]:
    """(source, source_ref) pairs of recent detections, to pre-mark them announced."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT source, source_ref FROM detections WHERE start_time >= ?",
            (since,),
        ).fetchall()
    return [(r["source"], r["source_ref"]) for r in rows]
