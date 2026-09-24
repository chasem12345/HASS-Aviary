"""Learned "not a bird" examples (0.37.0), and the detection state they produce."""

from __future__ import annotations
import time
from typing import Optional

from ._conn import _connect
from ._sql import UNNAMED

__all__ = [
    "NOT_BIRD",
    "not_bird_add",
    "not_bird_list",
    "not_bird_remove",
    "not_bird_vectors",
    "mark_not_bird",
    "not_bird_detections",
    "drop_subject",
]

# detections.id_status for a crop that matched a learned "not a bird" example. The row is
# kept (unnamed, out of every species count, the feed and the Unidentified queue) so it can
# be audited and overruled by naming it, and it expires with the other unidentified rows.
NOT_BIRD = "not_bird"


def not_bird_add(model: str, embedding: str, *, source_ref: Optional[str] = None,
                 camera: Optional[str] = None, label: Optional[str] = None,
                 crop_file: Optional[str] = None) -> int:
    """Store one negative example. Returns its id."""
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO not_bird_examples
                (model, embedding, source_ref, camera, label, crop_file, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (model, embedding, source_ref, camera, label, crop_file, time.time()),
        )
        return int(cur.lastrowid)


def not_bird_list() -> list[dict]:
    """Every negative example, newest first, without the vectors."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, model, source_ref, camera, label, crop_file, created_at
            FROM not_bird_examples ORDER BY created_at DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def not_bird_remove(example_id: int) -> Optional[dict]:
    """Forget one negative example. Returns the removed row (for its crop file)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, crop_file FROM not_bird_examples WHERE id = ?", (example_id,)
        ).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM not_bird_examples WHERE id = ?", (example_id,))
    return dict(row)


def not_bird_vectors(model: str) -> list[tuple[int, str]]:
    """(id, base64 embedding) of the examples comparable under ``model``."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, embedding FROM not_bird_examples WHERE model = ?", (model,)
        ).fetchall()
    return [(int(r["id"]), r["embedding"]) for r in rows]


def mark_not_bird(source: str, source_ref: str, similarity: float) -> None:
    """Record that a detection's crop is a learned "not a bird".

    The name goes back to the generic 'bird' — a confirmation run may have written
    Frigate's provisional label onto the row — which by itself keeps it out of every
    species aggregate. ``frigate_label`` stays as provenance.
    """
    with _connect() as conn:
        conn.execute(
            """
            UPDATE detections
            SET common_name = ?, scientific_name = NULL, species_code = NULL,
                id_status = ?, id_score = ?
            WHERE source = ? AND source_ref = ?
            """,
            (UNNAMED, NOT_BIRD, similarity, source, source_ref),
        )


def not_bird_detections(limit: int = 100, before: Optional[float] = None) -> list[dict]:
    """Detections set aside as not-a-bird, newest first — the audit list."""
    params: list = [NOT_BIRD]
    where = "WHERE id_status = ?"
    if before is not None:
        where += " AND start_time < ?"
        params.append(before)
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM detections {where} ORDER BY start_time DESC LIMIT ?", params
        ).fetchall()
    return [dict(r) for r in rows]


def drop_subject(detection_id: int, idx: int) -> bool:
    """Remove one OTHER bird from an event (it was not a bird at all)."""
    if idx <= 0:
        return False
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM detection_subjects WHERE detection_id = ? AND idx = ? AND is_primary = 0",
            (detection_id, idx),
        )
        conn.execute(
            "DELETE FROM detection_subject_rejections WHERE detection_id = ? AND idx = ?",
            (detection_id, idx),
        )
        conn.execute(
            "DELETE FROM learning_flags WHERE detection_id = ? AND subject_idx = ?",
            (detection_id, idx),
        )
    return cur.rowcount > 0
