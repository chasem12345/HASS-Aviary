"""Visits (Frigate review items), their member links and announcements, and the grouped feed."""

from __future__ import annotations
import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from ._conn import _connect
from ._sql import _named_clause

__all__ = [
    "_refresh_visit",
    "upsert_visit",
    "_visit_ids_of",
    "_settle_visits",
    "claim_visit_announcement",
    "seed_visit_announcements",
    "visit_by_id",
    "visit_by_review_id",
    "visit_members",
    "visits_with_members",
    "_zone_like",
    "feed_page",
    "reset_visits",
    "visit_stats",
]


# ---------------------------------------------------------------------------- visits

def _refresh_visit(conn: sqlite3.Connection, visit_id: int) -> None:
    """Recompute a visit's bounds from Frigate's review bounds and its members.

    The start is the earliest of the two, the end the latest — Frigate closes a review
    item on its own clock and the last tracked object can outlive it by seconds. A visit
    Frigate still has open (no review end) stays open regardless of member ends, so the
    card reads "in progress" for exactly as long as Frigate's Review page does.
    """
    conn.execute(
        """
        UPDATE visits SET
            start_time = MIN(review_start, COALESCE(
                (SELECT MIN(start_time) FROM detections WHERE visit_id = visits.id),
                review_start)),
            end_time = CASE WHEN review_end IS NULL THEN NULL ELSE MAX(review_end, COALESCE(
                (SELECT MAX(COALESCE(end_time, start_time)) FROM detections
                 WHERE visit_id = visits.id),
                review_end)) END
        WHERE id = ?
        """,
        (visit_id,),
    )


def upsert_visit(row: dict[str, Any]) -> int:
    """Store or update a Frigate review item and link its listed events. Returns visits.id.

    ``row`` is ``ingest.build_review_row``'s shape; ``refs`` is the review item's
    ``data.detections`` list, which grows over the item's lifetime — every message
    re-stamps the full list, so a member missed on one message is caught on the next.
    Members that already exist as detections are linked here; members that arrive later
    link themselves from ``visit_refs`` in ``upsert_detection``.
    """
    refs = [str(r) for r in (row.get("refs") or []) if r]
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO visits (
                review_id, camera, start_time, end_time, review_start, review_end,
                severity, zones, raw_json, created_at, updated_at
            ) VALUES (
                :review_id, :camera, :start_time, :end_time, :start_time, :end_time,
                :severity, :zones, :raw_json, :now, :now
            )
            ON CONFLICT(review_id) DO UPDATE SET
                camera       = excluded.camera,
                review_start = MIN(visits.review_start, excluded.review_start),
                -- A later message never un-ends a visit; an `update` after `end` does
                -- not happen in practice, but a replayed/backfilled item must not reopen.
                review_end   = COALESCE(excluded.review_end, visits.review_end),
                severity     = COALESCE(excluded.severity, visits.severity),
                zones        = COALESCE(excluded.zones, visits.zones),
                raw_json     = COALESCE(excluded.raw_json, visits.raw_json),
                updated_at   = excluded.updated_at
            """,
            {
                "review_id": row["review_id"],
                "camera": (row.get("camera") or "").lower(),
                "start_time": float(row["start_time"]),
                "end_time": row.get("end_time"),
                "severity": row.get("severity"),
                "zones": row.get("zones"),
                "raw_json": row.get("raw_json"),
                "now": now,
            },
        )
        visit_id = conn.execute(
            "SELECT id FROM visits WHERE review_id = ?", (row["review_id"],)
        ).fetchone()["id"]
        if refs:
            conn.executemany(
                "INSERT OR REPLACE INTO visit_refs (source_ref, visit_id) VALUES (?, ?)",
                [(ref, visit_id) for ref in refs],
            )
            marks = ",".join("?" for _ in refs)
            conn.execute(
                f"""
                UPDATE detections SET visit_id = ?
                WHERE source = 'frigate' AND source_ref IN ({marks})
                  AND (visit_id IS NULL OR visit_id != ?)
                """,
                [visit_id, *refs, visit_id],
            )
            # A member that already HAD a species when it got linked was announced on
            # its own (per-event) before the review item listed it — or was labelled by
            # hand, or backfilled, none of which should notify again. Record those
            # species as announced for the visit so a later sibling stays silent.
            conn.execute(
                f"""
                INSERT OR IGNORE INTO visit_announced
                    (visit_id, common_name, detection_id, announced_at)
                SELECT visit_id, common_name, id, ?
                FROM detections
                WHERE visit_id = ? AND source_ref IN ({marks})
                  {_named_clause()}
                """,
                [now, visit_id, *refs],
            )
        _refresh_visit(conn, visit_id)
    return visit_id


def _visit_ids_of(conn: sqlite3.Connection, det_ids: list[int]) -> list[int]:
    """Visits the given detections belong to. Read BEFORE deleting them (the members
    must still exist to be looked up); pair with ``_settle_visits`` afterwards."""
    if not det_ids:
        return []
    marks = ",".join("?" for _ in det_ids)
    rows = conn.execute(
        f"SELECT DISTINCT visit_id FROM detections WHERE id IN ({marks}) "
        f"AND visit_id IS NOT NULL",
        det_ids,
    ).fetchall()
    return [r["visit_id"] for r in rows]


def _settle_visits(conn: sqlite3.Connection, visit_ids: list[int]) -> None:
    """After members were deleted: shrink bounds, forget announcements for species that
    no longer have a member, and drop visits left with no members at all.

    The announcement cleanup mirrors ``_forget_if_gone`` for species: a deleted
    misclassification must not leave "already announced" behind, or the real bird that
    turns up in the same visit a minute later would be silent.
    """
    for vid in visit_ids:
        remaining = conn.execute(
            "SELECT COUNT(*) AS c FROM detections WHERE visit_id = ?", (vid,)
        ).fetchone()["c"]
        if not remaining:
            conn.execute("DELETE FROM visits WHERE id = ?", (vid,))
            conn.execute("DELETE FROM visit_refs WHERE visit_id = ?", (vid,))
            conn.execute("DELETE FROM visit_announced WHERE visit_id = ?", (vid,))
            continue
        conn.execute(
            """
            DELETE FROM visit_announced
            WHERE visit_id = ? AND NOT EXISTS (
                SELECT 1 FROM detections d
                WHERE d.visit_id = visit_announced.visit_id
                  AND d.common_name = visit_announced.common_name COLLATE NOCASE
            )
            """,
            (vid,),
        )
        _refresh_visit(conn, vid)


def claim_visit_announcement(visit_id: int, common_name: str,
                             detection_id: Optional[int]) -> bool:
    """Atomically mark (visit, species) announced. True only for the first claimant."""
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO visit_announced
                (visit_id, common_name, detection_id, announced_at)
            VALUES (?, ?, ?, ?)
            """,
            (visit_id, common_name, detection_id, time.time()),
        )
        return cur.rowcount == 1


def seed_visit_announcements(since: float) -> int:
    """Pre-mark every named species of recent/open visits as announced.

    Startup only — the persisted twin of ``ingest.seed_notify_state``'s recent-refs
    pre-marking, so upgrading (or restarting) mid-visit doesn't re-notify for birds that
    were announced before the restart. Returns the number of rows added.
    """
    with _connect() as conn:
        cur = conn.execute(
            f"""
            INSERT OR IGNORE INTO visit_announced
                (visit_id, common_name, detection_id, announced_at)
            SELECT d.visit_id, d.common_name, MIN(d.id), ?
            FROM detections d JOIN visits v ON v.id = d.visit_id
            WHERE (v.end_time IS NULL OR v.end_time >= ?)
              {_named_clause('d')}
            GROUP BY d.visit_id, d.common_name
            """,
            (time.time(), since),
        )
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def visit_by_id(visit_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM visits WHERE id = ?", (visit_id,)).fetchone()
    return dict(row) if row else None


def visit_by_review_id(review_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM visits WHERE review_id = ?", (review_id,)
        ).fetchone()
    return dict(row) if row else None


def visit_members(visit_id: int) -> list[dict]:
    """A visit's detections, oldest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM detections WHERE visit_id = ? ORDER BY start_time",
            (visit_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def visits_with_members(visit_ids: list[int]) -> dict[int, list[dict]]:
    """Members for many visits in one query, keyed by visit id, oldest first."""
    if not visit_ids:
        return {}
    marks = ",".join("?" for _ in visit_ids)
    out: dict[int, list[dict]] = {vid: [] for vid in visit_ids}
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM detections WHERE visit_id IN ({marks}) ORDER BY start_time",
            visit_ids,
        ).fetchall()
    for r in rows:
        out.setdefault(r["visit_id"], []).append(dict(r))
    return out


def _zone_like(column: str, params: list, zone: str) -> str:
    """Delimiter-safe match of one zone inside a comma-joined list column."""
    params.append(zone.replace(" ", ""))
    return f" ',' || REPLACE(COALESCE({column}, ''), ' ', '') || ',' LIKE '%,' || ? || ',%'"


def feed_page(
    limit: int = 60,
    source: Optional[str] = None,
    species: Optional[str] = None,
    before: Optional[float] = None,
    since: Optional[float] = None,
    zone: Optional[str] = None,
) -> list[dict]:
    """Newest-first feed items: visits (with members) plus ungrouped detections.

    The Recent page's unit of display. A Frigate event that belongs to a visit is shown
    only inside that visit; BirdNET rows and Frigate events with no review item (older
    than Frigate's review retention, or from before review items existed) appear as their
    own cards, exactly as before. ``before``/``since`` are start_time cursors, valid for
    both kinds because a visit's start_time is a real timestamp too.

    ``species`` matches the tracked bird OR a named other bird in view (see the
    species_sightings view), so a species page lists every visit the bird took part in.

    Visit dicts carry ``kind="visit"`` and ``members``; detection dicts are unchanged.
    """
    v_params: list = []
    v_where = "WHERE 1=1"
    d_params: list = []
    d_where = "WHERE 1=1"
    include_visits = source in (None, "frigate")
    if source == "frigate":
        d_where += " AND source = 'frigate' AND visit_id IS NULL"
    elif source == "birdnet":
        d_where += " AND source = 'birdnet'"
    else:
        d_where += " AND (source != 'frigate' OR visit_id IS NULL)"
    # Footage Frigate has expired leaves the feed unless the event has its own crop: a
    # card with nothing to show is noise, but the bird's picture is still worth a card.
    # Direct pages (detection, visit, Kept) and every aggregate still see these rows.
    d_where += " AND (source != 'frigate' OR media_expired_at IS NULL OR has_crop = 1)"
    v_where += (" AND EXISTS (SELECT 1 FROM detections m WHERE m.visit_id = v.id"
                " AND (m.media_expired_at IS NULL OR m.has_crop = 1))")
    if species:
        # A visit is about a species if ANY member's event contains it — as the tracked
        # bird or as a named other bird (the sightings view spells out "named" once); a
        # visit-less event qualifies on the same terms.
        v_where += (" AND EXISTS (SELECT 1 FROM species_sightings ss WHERE ss.visit_id = v.id"
                    " AND ss.common_name = ? COLLATE NOCASE)")
        v_params.append(species)
        d_where += (" AND EXISTS (SELECT 1 FROM species_sightings ss WHERE ss.id = detections.id"
                    " AND ss.common_name = ? COLLATE NOCASE)")
        d_params.append(species)
    if zone:
        v_where += (" AND (" + _zone_like("v.zones", v_params, zone)
                    + " OR EXISTS (SELECT 1 FROM detections m WHERE m.visit_id = v.id AND"
                    + _zone_like("m.zone", v_params, zone) + "))")
        d_where += " AND" + _zone_like("zone", d_params, zone)
    if before is not None:
        v_where += " AND v.start_time < ?"
        v_params.append(before)
        d_where += " AND start_time < ?"
        d_params.append(before)
    if since is not None:
        v_where += " AND v.start_time >= ?"
        v_params.append(since)
        d_where += " AND start_time >= ?"
        d_params.append(since)

    with _connect() as conn:
        dets = [dict(r) for r in conn.execute(
            f"SELECT * FROM detections {d_where} ORDER BY start_time DESC LIMIT ?",
            [*d_params, limit],
        ).fetchall()]
        visits: list[dict] = []
        if include_visits:
            visits = [dict(r) for r in conn.execute(
                f"SELECT v.* FROM visits v {v_where} ORDER BY v.start_time DESC LIMIT ?",
                [*v_params, limit],
            ).fetchall()]
    for v in visits:
        v["kind"] = "visit"
    members = visits_with_members([v["id"] for v in visits])
    for v in visits:
        v["members"] = members.get(v["id"], [])
    # A visit with no members (all deleted, or its events never imported) has nothing to
    # show; hide rather than render an empty shell. _settle_visits removes these on the
    # deletion paths, so this is belt-and-braces for import ordering.
    visits = [v for v in visits if v["members"]]
    items = sorted(visits + dets, key=lambda x: x["start_time"], reverse=True)
    return items[:limit]


def reset_visits() -> None:
    """Forget every visit (for a full re-import from Frigate's review API)."""
    with _connect() as conn:
        conn.execute("UPDATE detections SET visit_id = NULL WHERE visit_id IS NOT NULL")
        conn.execute("DELETE FROM visits")
        conn.execute("DELETE FROM visit_refs")
        conn.execute("DELETE FROM visit_announced")


def visit_stats() -> dict:
    """Counts for the startup log: visits, grouped and ungrouped Frigate events."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT (SELECT COUNT(*) FROM visits) AS visits,
                   SUM(visit_id IS NOT NULL) AS grouped,
                   SUM(visit_id IS NULL) AS ungrouped
            FROM detections WHERE source = 'frigate'
            """
        ).fetchone()
    return {"visits": row["visits"] or 0, "grouped": row["grouped"] or 0,
            "ungrouped": row["ungrouped"] or 0}
