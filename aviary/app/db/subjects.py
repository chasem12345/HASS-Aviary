"""Other birds in view: detection_subjects rows, rejections, species_present."""

from __future__ import annotations
import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from ._conn import _connect
from ._sql import _named_clause

__all__ = [
    "_SUBJECT_COLS",
    "replace_subjects",
    "subjects_for",
    "secondary_subjects_for",
    "set_subject_species_manually",
    "_reject_subject_row",
    "reject_subject",
    "_reject_named_subjects",
    "subject_rejections",
    "_delete_subjects",
    "species_present",
    "detections_with_unidentified_subjects",
    "unidentified_subject_count",
    "confirmed_subject_embeddings",
]


# -------------------------------------------------------------------------- subjects

_SUBJECT_COLS = (
    "detection_id", "idx", "is_primary", "common_name", "scientific_name", "species_code",
    "score", "margin", "candidates", "id_status", "manual_name", "manual_sci",
    "embedding_model", "embedding", "crop_file", "anchored", "n_frames", "created_at",
)


def replace_subjects(detection_id: int, rows: list[dict[str, Any]]) -> None:
    """Replace a detection's subjects wholesale (one transaction).

    Rejections are keyed by idx and survive on purpose: the caller matches old subjects
    to new ones by embedding and rewrites the idx on any it carried over, and drops the
    rest — see identify._carry_forward.
    """
    now = time.time()
    with _connect() as conn:
        conn.execute("DELETE FROM detection_subjects WHERE detection_id = ?", (detection_id,))
        for r in rows:
            values = {c: r.get(c) for c in _SUBJECT_COLS}
            values["detection_id"] = detection_id
            values["created_at"] = values.get("created_at") or now
            values["is_primary"] = 1 if values.get("is_primary") else 0
            values["anchored"] = 0 if values.get("anchored") is False else 1
            conn.execute(
                f"INSERT INTO detection_subjects ({', '.join(_SUBJECT_COLS)}) "
                f"VALUES ({', '.join(':' + c for c in _SUBJECT_COLS)})",
                values,
            )


def subjects_for(detection_id: int) -> list[dict]:
    """All of a detection's subjects, primary first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM detection_subjects WHERE detection_id = ? ORDER BY idx",
            (detection_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def secondary_subjects_for(detection_id: int) -> list[dict]:
    """The OTHER birds in a detection's event, for the card's "also in view" strip.

    ``display_name`` is what the card should call it: the human's label if there is
    one, else the service's answer when it passed the thresholds, else None (show the
    candidates instead).
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT s.*, (
                SELECT GROUP_CONCAT(species, '|') FROM detection_subject_rejections r
                WHERE r.detection_id = s.detection_id AND r.idx = s.idx
            ) AS rejected
            FROM detection_subjects s
            WHERE s.detection_id = ? AND s.is_primary = 0
            ORDER BY s.idx
            """,
            (detection_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["display_name"] = d.get("manual_name") or (
            d.get("common_name") if d.get("id_status") == "ok" else None)
        d["rejected"] = [x for x in (d.get("rejected") or "").split("|") if x]
        out.append(d)
    return out


def set_subject_species_manually(detection_id: int, idx: int, common_name: str,
                                 scientific_name: Optional[str]) -> bool:
    """Name one OTHER bird in an event by hand. False if there is no such subject."""
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE detection_subjects
            SET manual_name = ?, manual_sci = ?, id_status = 'manual'
            WHERE detection_id = ? AND idx = ? AND is_primary = 0
            """,
            (common_name, scientific_name, detection_id, idx),
        )
        return cur.rowcount > 0


def _reject_subject_row(conn, detection_id: int, idx: int, species: str) -> None:
    """Record "not a <species>" for one other-bird and clear its name (status rejected)."""
    conn.execute(
        """
        INSERT OR IGNORE INTO detection_subject_rejections
            (detection_id, idx, species, rejected_at)
        VALUES (?, ?, ?, ?)
        """,
        (detection_id, idx, species, time.time()),
    )
    conn.execute(
        """
        UPDATE detection_subjects
        SET common_name = NULL, scientific_name = NULL, species_code = NULL,
            manual_name = NULL, manual_sci = NULL, id_status = 'rejected'
        WHERE detection_id = ? AND idx = ?
        """,
        (detection_id, idx),
    )


def reject_subject(detection_id: int, idx: int, species: str) -> bool:
    """"That other bird is not a <species>": remember it and clear the subject's name.

    No service call — the subject's own candidates are already stored, so the UI can
    offer the next one. Returns False if there is no such subject.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM detection_subjects WHERE detection_id = ? AND idx = ? "
            "AND is_primary = 0",
            (detection_id, idx),
        ).fetchone()
        if row is None:
            return False
        _reject_subject_row(conn, detection_id, idx, species)
        return True


def _reject_named_subjects(conn, common_name: str) -> int:
    """Rule ``common_name`` out for every other-bird currently named as it.

    Part of removing a species: the sightings view would otherwise resurrect it from the
    subject rows the moment its tracked detections were gone. Returns how many were cleared.
    """
    rows = conn.execute(
        """
        SELECT detection_id, idx FROM detection_subjects
        WHERE is_primary = 0
          AND COALESCE(manual_name, common_name) = ? COLLATE NOCASE
        """,
        (common_name,),
    ).fetchall()
    for r in rows:
        _reject_subject_row(conn, r["detection_id"], r["idx"], common_name)
    return len(rows)


def subject_rejections(detection_id: int) -> dict[int, list[str]]:
    """{idx: [rejected species...]} for one detection's subjects."""
    out: dict[int, list[str]] = {}
    with _connect() as conn:
        for r in conn.execute(
            "SELECT idx, species FROM detection_subject_rejections WHERE detection_id = ?",
            (detection_id,),
        ):
            out.setdefault(r["idx"], []).append(r["species"])
    return out


def _delete_subjects(conn: sqlite3.Connection, det_ids: list[int]) -> None:
    if not det_ids:
        return
    ids = [(i,) for i in det_ids]
    conn.executemany("DELETE FROM detection_subjects WHERE detection_id = ?", ids)
    conn.executemany("DELETE FROM detection_subject_rejections WHERE detection_id = ?", ids)


def species_present(detection_ids: list[int]) -> dict[int, list[dict]]:
    """Every species present in each detection's event: the primary (the detections row
    itself) plus any OTHER bird that was confidently identified or labelled by hand.

    {detection_id: [{common_name, scientific_name, score, idx, source}]} with the primary
    first. One query for many detections, because the visit card asks for a whole visit.
    """
    if not detection_ids:
        return {}
    marks = ",".join("?" for _ in detection_ids)
    out: dict[int, list[dict]] = {i: [] for i in detection_ids}
    with _connect() as conn:
        for r in conn.execute(
            f"""
            SELECT id, common_name, scientific_name, id_score AS score
            FROM detections WHERE id IN ({marks}) {_named_clause()}
            """,
            detection_ids,
        ):
            out[r["id"]].append({"common_name": r["common_name"],
                                 "scientific_name": r["scientific_name"],
                                 "score": r["score"], "idx": 0, "source": "primary"})
        for r in conn.execute(
            f"""
            SELECT detection_id, idx, COALESCE(manual_name, common_name) AS name,
                   COALESCE(manual_sci, scientific_name) AS sci, score
            FROM detection_subjects
            WHERE detection_id IN ({marks}) AND is_primary = 0
              AND id_status IN ('ok', 'manual')
              AND COALESCE(manual_name, common_name) IS NOT NULL
            ORDER BY detection_id, idx
            """,
            detection_ids,
        ):
            out[r["detection_id"]].append({"common_name": r["name"],
                                           "scientific_name": r["sci"],
                                           "score": r["score"], "idx": r["idx"],
                                           "source": "subject"})
    return out


def detections_with_unidentified_subjects(limit: int = 100,
                                          before: Optional[float] = None) -> list[dict]:
    """Detections whose PRIMARY has a name but some other bird in view does not."""
    params: list = []
    where = ("WHERE EXISTS (SELECT 1 FROM detection_subjects s WHERE s.detection_id = "
             "detections.id AND s.is_primary = 0 AND s.id_status IN "
             "('low_confidence', 'rejected'))")
    if before is not None:
        where += " AND start_time < ?"
        params.append(before)
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM detections {where} ORDER BY start_time DESC LIMIT ?", params
        ).fetchall()
    return [dict(r) for r in rows]


def unidentified_subject_count() -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM detection_subjects WHERE is_primary = 0 "
            "AND id_status IN ('low_confidence', 'rejected')"
        ).fetchone()
    return int(row["c"] or 0) if row else 0


def confirmed_subject_embeddings(model: str) -> list[tuple[str, str]]:
    """(species, embedding) for every OTHER-bird subject the probe may learn from.

    Confident answers and human labels only, and only for species already confirmed
    into the registry — the same bar ``confirmed_embeddings`` holds primaries to.
    Primaries are never read from this table (they live in identification_embeddings),
    so the two lists never double count.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT COALESCE(s.manual_name, s.common_name) AS name, s.embedding AS embedding
            FROM detection_subjects s
            JOIN species_confirmed sc
              ON sc.common_name = COALESCE(s.manual_name, s.common_name) COLLATE NOCASE
            WHERE s.is_primary = 0 AND s.id_status IN ('ok', 'manual')
              AND s.embedding_model = ? AND s.embedding IS NOT NULL
              AND COALESCE(s.manual_name, s.common_name) IS NOT NULL
            """,
            (model,),
        ).fetchall()
    return [(r["name"], r["embedding"]) for r in rows]
