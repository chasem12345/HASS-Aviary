"""What the probe learns from: every example with its provenance, and the flags on them.

One query serves the probe, the Settings-page example browser and the audit, so all three
agree on what an "example" is: an embedding of a detection (or of another bird in view)
whose label is confirmed into the registry — by default only labels a person typed.
"""

from __future__ import annotations
import sqlite3
import time
from typing import Any

from ._conn import _connect

__all__ = [
    "learning_examples",
    "auto_example_count",
    "set_learning_excluded",
    "set_suspicious",
    "forget_learning",
    "_delete_learning_flags",
    "_remap_learning_flags",
]

_COLS = ("species", "embedding", "detection_id", "subject_idx", "visit_id", "start_time",
         "source_ref", "id_status", "target", "excluded_at", "suspicious_at",
         "suspicious_margin", "suspicious_species")


def _sql(status_clause: str, excluded_clause: str, select: str = "*") -> str:
    return f"""
        SELECT {select} FROM (
            SELECT d.common_name AS species, e.embedding AS embedding, d.id AS detection_id,
                   0 AS subject_idx, d.visit_id AS visit_id, d.start_time AS start_time,
                   d.source_ref AS source_ref, d.id_status AS id_status, e.target AS target,
                   f.excluded_at, f.suspicious_at, f.suspicious_margin, f.suspicious_species
            FROM identification_embeddings e
            JOIN detections d ON d.id = e.detection_id
            JOIN species_confirmed sc ON sc.common_name = d.common_name COLLATE NOCASE
            LEFT JOIN learning_flags f ON f.detection_id = d.id AND f.subject_idx = 0
            WHERE e.model = ? AND d.common_name != 'bird' COLLATE NOCASE
              AND d.id_status {status_clause}{excluded_clause}
            UNION ALL
            SELECT COALESCE(s.manual_name, s.common_name) AS species, s.embedding, s.detection_id,
                   s.idx, d.visit_id, d.start_time, d.source_ref, s.id_status, s.embedding_target,
                   f.excluded_at, f.suspicious_at, f.suspicious_margin, f.suspicious_species
            FROM detection_subjects s
            JOIN detections d ON d.id = s.detection_id
            JOIN species_confirmed sc
              ON sc.common_name = COALESCE(s.manual_name, s.common_name) COLLATE NOCASE
            LEFT JOIN learning_flags f ON f.detection_id = s.detection_id AND f.subject_idx = s.idx
            WHERE s.is_primary = 0 AND s.embedding_model = ? AND s.embedding IS NOT NULL
              AND COALESCE(s.manual_name, s.common_name) IS NOT NULL
              AND s.id_status {status_clause}{excluded_clause}
        )
    """


def learning_examples(model: str, include_auto: bool = False,
                      include_excluded: bool = True) -> list[dict[str, Any]]:
    """Every example the probe could learn from under ``model``, newest first.

    ``include_auto`` admits the identifier's own passing answers ('ok') beside the labels
    a person typed ('manual'). Off by default: learning from unreviewed guesses is how a
    classifier reinforces its own mistakes. ``include_excluded`` keeps rows a person has
    marked "do not learn from" — the browser wants to show them; the probe does not.
    """
    status = "IN ('ok', 'manual')" if include_auto else "= 'manual'"
    excl = "" if include_excluded else " AND f.excluded_at IS NULL"
    with _connect() as conn:
        rows = conn.execute(_sql(status, excl) + " ORDER BY start_time DESC",
                            (model, model)).fetchall()
    out = []
    for r in rows:
        d = {c: r[c] for c in _COLS}
        d["label_source"] = "manual" if d["id_status"] == "manual" else "auto"
        out.append(d)
    return out


def auto_example_count(model: str) -> int:
    """How many automatic ('ok') examples exist that manual-only learning leaves out."""
    with _connect() as conn:
        row = conn.execute(_sql("= 'ok'", "", "COUNT(*) AS n"), (model, model)).fetchone()
    return int(row["n"] if row else 0)


def set_learning_excluded(detection_id: int, subject_idx: int, excluded: bool) -> None:
    """Mark one example as not-to-be-learned-from (or put it back). The row itself stays."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO learning_flags (detection_id, subject_idx, excluded_at)
            VALUES (?, ?, ?)
            ON CONFLICT(detection_id, subject_idx) DO UPDATE SET excluded_at = excluded.excluded_at
            """,
            (detection_id, subject_idx, time.time() if excluded else None),
        )


def set_suspicious(rows: list[tuple[int, int, float, str]]) -> None:
    """Replace the audit's verdicts wholesale: (detection_id, subject_idx, margin, species)."""
    now = time.time()
    with _connect() as conn:
        conn.execute("UPDATE learning_flags SET suspicious_at = NULL, suspicious_margin = NULL, "
                     "suspicious_species = NULL")
        conn.execute("DELETE FROM learning_flags WHERE excluded_at IS NULL")
        conn.executemany(
            """
            INSERT INTO learning_flags
                (detection_id, subject_idx, suspicious_at, suspicious_margin, suspicious_species)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(detection_id, subject_idx) DO UPDATE SET
                suspicious_at = excluded.suspicious_at,
                suspicious_margin = excluded.suspicious_margin,
                suspicious_species = excluded.suspicious_species
            """,
            [(d, i, now, m, sp) for d, i, m, sp in rows],
        )


def _delete_learning_flags(conn: sqlite3.Connection, det_ids: list[int]) -> None:
    if det_ids:
        conn.executemany("DELETE FROM learning_flags WHERE detection_id = ?",
                         [(i,) for i in det_ids])


def _remap_learning_flags(conn: sqlite3.Connection, det_id: int,
                          carried: list[tuple[int, int]]) -> None:
    """Move other-bird flags to their carried-over idx; drop those nothing carried.

    The detection's own flag (subject_idx 0) is untouched: the primary is the same bird
    before and after a re-identify.
    """
    rows = conn.execute(
        "SELECT * FROM learning_flags WHERE detection_id = ? AND subject_idx > 0", (det_id,)
    ).fetchall()
    conn.execute("DELETE FROM learning_flags WHERE detection_id = ? AND subject_idx > 0", (det_id,))
    mapping = dict(carried)
    for r in rows:
        new_idx = mapping.get(r["subject_idx"])
        if new_idx is None:
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO learning_flags
                (detection_id, subject_idx, excluded_at, suspicious_at, suspicious_margin,
                 suspicious_species)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (det_id, new_idx, r["excluded_at"], r["suspicious_at"], r["suspicious_margin"],
             r["suspicious_species"]),
        )


def forget_learning() -> dict:
    """Drop every learned example — the probe's whole memory of your birds.

    Removes the stored embeddings of detections and of other birds in view, and the
    audit's verdicts. Everything else stays: the names on the cards, the species
    registry, the history, and any "do not learn from this one" marks (those are
    decisions about specific detections and still apply if an example comes back).
    Reference-photo embeddings stay too — they are not teaching, they are the bootstrap.
    The startup re-harvest then rebuilds examples from hand-named cards whose media
    Frigate still has, choosing each frame for the label.
    """
    with _connect() as conn:
        dets = conn.execute("SELECT COUNT(*) AS n FROM identification_embeddings").fetchone()["n"]
        subs = conn.execute("SELECT COUNT(*) AS n FROM detection_subjects "
                            "WHERE embedding IS NOT NULL").fetchone()["n"]
        conn.execute("DELETE FROM identification_embeddings")
        conn.execute("UPDATE detection_subjects SET embedding = NULL, embedding_model = NULL, "
                     "embedding_target = NULL")
        conn.execute("UPDATE learning_flags SET suspicious_at = NULL, suspicious_margin = NULL, "
                     "suspicious_species = NULL")
        conn.execute("DELETE FROM learning_flags WHERE excluded_at IS NULL")
    return {"detections": int(dets), "subjects": int(subs)}
