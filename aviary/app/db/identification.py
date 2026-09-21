"""Identification results, embeddings, rejections and the unidentified queue."""

from __future__ import annotations
import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from ._conn import _connect
from ._sql import UNNAMED
from .schema import embedding_key_from
from .learning import learning_examples
from .subjects import _delete_subjects, unidentified_subject_count
from .visits import _settle_visits, _visit_ids_of

__all__ = [
    "set_identification",
    "_upsert_detection_embedding",
    "put_detection_embedding",
    "has_embedding",
    "embedding_for",
    "set_frigate_label",
    "embedding_target_for",
    "delete_detection_embedding",
    "manual_rows_missing_embeddings",
    "manual_rows_untargeted",
    "set_species_manually",
    "reject_identification",
    "reset_species",
    "rejections_for",
    "clear_rejections",
    "drop_detection",
    "confirmed_embeddings",
    "reference_embeddings",
    "put_reference_embedding",
    "species_missing_reference_embeddings",
    "species_heard_between",
    "pending_identifications",
    "_UNIDENTIFIED",
    "unidentified_detections",
    "unidentified_counts",
    "uncertain_rows_in_visits",
    "unidentified_count",
    "purge_unidentified",
]


# -------------------------------------------------------------------- identification

def set_identification(
    source: str,
    source_ref: str,
    status: str,
    score: Optional[float] = None,
    margin: Optional[float] = None,
    model: Optional[str] = None,
    embedding: Optional[str] = None,
    set_confidence: bool = False,
    candidates: Optional[str] = None,
    embedding_model: Optional[str] = None,
    probe_weight: Optional[float] = None,
    probe_examples: Optional[int] = None,
    embedding_target: Optional[str] = None,
    confidence: Optional[float] = None,
) -> None:
    """Record the outcome of an external identification attempt.

    Kept separate from ``upsert_detection`` on purpose: that function is on the hot path
    for every MQTT message, whereas this runs once per event. Threading identification
    fields through the row dict would mean every caller that builds a row — including the
    BirdNET path, which has no identification — carrying five columns it does not use.

    ``set_confidence`` forces ``detections.confidence`` to ``score``, deliberately
    bypassing the "keep the best score" merge in ``upsert_detection``. That merge is
    correct while both values mean the same thing, but Frigate's score answers "is this a
    bird" (typically 0.85+) and ours answers "is this a Black-capped Chickadee". Taking
    the maximum would put Frigate's high object score in the field the UI labels as
    species confidence, making an uncertain identification look authoritative.
    ``confidence`` names a different value to write there than ``score``: a confirmation
    that lets Frigate's label stand records the identifier's probability for that label
    as ``id_score`` but Frigate's own classification score as the row's confidence.

    ``model`` (the full model_version) is provenance for the RESULT — after a model or
    vocabulary change it tells you which rows are stale. ``embedding_model`` keys the
    stored EMBEDDING and must be the service's embedding_key, which survives vocabulary
    changes. Defaults to ``model`` reduced via ``embedding_key_from`` for callers that
    only have the old-style combined string.
    """
    if embedding_model is None and model:
        embedding_model = embedding_key_from(model)
    with _connect() as conn:
        conn.execute(
            f"""
            UPDATE detections SET
                id_status = ?, id_score = ?, id_margin = ?, id_model = ?, id_at = ?,
                -- COALESCE so a later status-only update (a retry that failed, say) does
                -- not wipe a shortlist we already have to show the user.
                id_candidates = COALESCE(?, id_candidates),
                -- NOT coalesced: NULL means "the probe abstained on this attempt", and a
                -- reroll where it abstained must not inherit the previous run's numbers.
                id_probe_weight = ?, id_probe_examples = ?
                {", confidence = ?" if set_confidence else ""}
            WHERE source = ? AND source_ref = ?
            """,
            (status, score, margin, model, time.time(), candidates,
             probe_weight, probe_examples)
            + (((confidence if confidence is not None else score),) if set_confidence else ())
            + (source, source_ref),
        )
        if embedding and embedding_model:
            row = conn.execute(
                "SELECT id FROM detections WHERE source = ? AND source_ref = ?",
                (source, source_ref),
            ).fetchone()
            if row:
                _upsert_detection_embedding(conn, row["id"], embedding_model, embedding,
                                            embedding_target)


def _upsert_detection_embedding(conn, detection_id: int, model: str, embedding: str,
                                target: Optional[str] = None) -> None:
    """One embedding per detection. ``target`` is the species the frame was chosen FOR
    (the service winner on a normal identify, the person's label on a re-embed); a live
    re-identify legitimately resets it."""
    conn.execute(
        """
        INSERT INTO identification_embeddings
            (detection_id, model, embedding, created_at, target)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(detection_id) DO UPDATE SET
            model = excluded.model,
            embedding = excluded.embedding,
            created_at = excluded.created_at,
            target = excluded.target
        """,
        (detection_id, model, embedding, time.time(), target),
    )


def put_detection_embedding(detection_id: int, model: str, embedding: str,
                            target: Optional[str] = None) -> None:
    """Store an embedding for a detection outside the identification flow.

    Backs the manual-label backfill and re-embed: a detection whose identification failed
    has no embedding row, so a manual label on it would teach the probe nothing without
    this; a relabelled one has a frame chosen for the wrong species.
    """
    with _connect() as conn:
        _upsert_detection_embedding(conn, detection_id, model, embedding, target)


def embedding_target_for(detection_id: int) -> tuple[bool, Optional[str]]:
    """(has an embedding, which species its frame was chosen for — None for legacy rows)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT target FROM identification_embeddings WHERE detection_id = ?",
            (detection_id,),
        ).fetchone()
    return (row is not None, row["target"] if row else None)


def delete_detection_embedding(detection_id: int) -> None:
    """Drop a detection's stored example (its label stays). Used when a relabel makes the
    stored frame — chosen for a different species — the wrong thing to learn from."""
    with _connect() as conn:
        conn.execute("DELETE FROM identification_embeddings WHERE detection_id = ?",
                     (detection_id,))


def has_embedding(detection_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM identification_embeddings WHERE detection_id = ?",
            (detection_id,),
        ).fetchone()
    return row is not None


def embedding_for(detection_id: int) -> Optional[tuple[str, str]]:
    """(embedding key, base64 embedding) stored for a detection, or None.

    Backs the local confirm decision: when Frigate's label arrives for a row the
    identifier already looked at (and found uncertain), the stored vector is enough to
    ask the probe whether the learned birds disagree — no second GPU pass needed.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT model, embedding FROM identification_embeddings WHERE detection_id = ?",
            (detection_id,),
        ).fetchone()
    return (row["model"], row["embedding"]) if row else None


def set_frigate_label(source: str, source_ref: str, label: Optional[str],
                      score: Optional[float]) -> bool:
    """Record Frigate's own classification on a row without touching the species.

    For a label that lands AFTER the row was dispatched or decided: provenance only.
    The score keeps the best value seen, as ``upsert_detection`` does. True when a row
    was updated.
    """
    if not label:
        return False
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE detections SET
                frigate_label = ?,
                frigate_score = COALESCE(MAX(frigate_score, ?), frigate_score, ?)
            WHERE source = ? AND source_ref = ?
            """,
            (label, score, score, source, source_ref),
        )
        return bool(cur.rowcount)


def manual_rows_missing_embeddings(limit: int = 50) -> list[dict]:
    """Manually-labelled Frigate detections that never got an embedding stored.

    These are labels that currently teach the probe nothing: the identification failed
    (or predates embeddings), so there is no vector for the confirmed name to train.
    Newest first — recent events are the ones whose media Frigate still has.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT d.* FROM detections d
            LEFT JOIN identification_embeddings e ON e.detection_id = d.id
            WHERE d.id_status = 'manual' AND d.source = 'frigate'
              AND e.detection_id IS NULL
            ORDER BY d.start_time DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def manual_rows_untargeted(limit: int = 50) -> list[dict]:
    """Manually-labelled Frigate detections whose stored frame was not chosen for their
    label: legacy rows (target NULL) and rows whose target is a different species.
    Newest first, for the same retention reason as ``manual_rows_missing_embeddings``.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT d.* FROM detections d
            JOIN identification_embeddings e ON e.detection_id = d.id
            WHERE d.id_status = 'manual' AND d.source = 'frigate'
              AND (e.target IS NULL OR e.target != d.common_name COLLATE NOCASE)
            ORDER BY d.start_time DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def set_species_manually(detection_id: int, common_name: str,
                         scientific_name: Optional[str] = None,
                         species_code: Optional[str] = None) -> Optional[dict]:
    """Name a detection by hand. Returns the updated row.

    Written directly rather than through ``upsert_detection`` because that function's merge
    rules exist to reconcile repeated messages from a source, and none of them apply here:
    a person typing a species is the most authoritative input there is, and it must be able
    to overwrite whatever the model decided — including replacing a name with a different
    one, which the "never downgrade" rule would otherwise fight.

    Confidence is cleared rather than set to 1.0: the field means "how sure was the
    classifier", and a human answer has no place on that scale. ``id_status = 'manual'``
    is what records that this was a person.
    """
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE detections SET
                common_name = ?, scientific_name = ?, species_code = ?,
                confidence = NULL, id_status = 'manual', id_at = ?
            WHERE id = ?
            """,
            (common_name, scientific_name, species_code, time.time(), detection_id),
        )
        if not cur.rowcount:
            return None
        row = conn.execute("SELECT * FROM detections WHERE id = ?", (detection_id,)).fetchone()
    return dict(row) if row else None


def reject_identification(detection_id: int, species: str) -> None:
    """Record that a species is the wrong answer for this specific detection."""
    if not (species or "").strip() or species.strip().lower() == "bird":
        return  # "bird" is the absence of an answer, not an answer to reject
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO identification_rejections
                (detection_id, species, rejected_at) VALUES (?, ?, ?)
            """,
            (detection_id, species.strip(), time.time()),
        )


def reset_species(detection_id: int) -> None:
    """Strip a detection back to an unnamed 'bird'.

    Needed before re-identifying a rejected answer. ``upsert_detection`` deliberately
    refuses to downgrade a named species back to 'bird' — that rule protects against a
    later Frigate message arriving without a sub_label — so if the reroll came back below
    threshold and never re-stored the row, the name the user just rejected would stay on
    screen. Clearing it first means a failed reroll correctly leaves the detection
    unidentified and in the review queue.
    """
    with _connect() as conn:
        conn.execute(
            """
            UPDATE detections SET
                common_name = 'bird', scientific_name = NULL,
                species_code = NULL, confidence = NULL
            WHERE id = ?
            """,
            (detection_id,),
        )


def rejections_for(detection_id: int) -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT species FROM identification_rejections WHERE detection_id = ?",
            (detection_id,),
        ).fetchall()
    return [r["species"] for r in rows]


def clear_rejections(detection_id: int) -> None:
    """Start over on a detection whose rejections have painted it into a corner."""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM identification_rejections WHERE detection_id = ?", (detection_id,)
        )


def drop_detection(source: str, source_ref: str) -> None:
    """Remove a row without tombstoning it.

    Distinct from ``delete_detection``, which records a tombstone because the user chose
    to remove something. This is for a row Aviary itself stored provisionally and then
    decided against (an identification that resolved to a blacklisted species). A
    tombstone would be wrong: it would permanently block re-import of an event the user
    might later un-blacklist.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM detections WHERE source = ? AND source_ref = ?",
            (source, source_ref),
        ).fetchone()
        if row is None:
            return
        visit_ids = _visit_ids_of(conn, [row["id"]])
        conn.execute("DELETE FROM detections WHERE id = ?", (row["id"],))
        conn.execute(
            "DELETE FROM identification_embeddings WHERE detection_id = ?", (row["id"],)
        )
        conn.execute(
            "DELETE FROM identification_rejections WHERE detection_id = ?", (row["id"],)
        )
        _delete_subjects(conn, [row["id"]])
        _settle_visits(conn, visit_ids)


def confirmed_embeddings(model: str, include_auto: bool = False) -> list[tuple[str, str]]:
    """(species, embedding) for every example the probe learns from under ``model``.

    Thin wrapper over ``learning_examples`` (db/learning.py), which carries provenance
    and flags; kept for callers and tests that only want the pairs. Manual labels only
    unless ``include_auto``: the whole point is to learn from labels a person stands
    behind, and an unreviewed automatic guess would teach the classifier its own
    mistakes. Examples a person has excluded from learning are left out.
    """
    return [(r["species"], r["embedding"])
            for r in learning_examples(model, include_auto=include_auto, include_excluded=False)]


def reference_embeddings(model: str) -> list[tuple[str, str]]:
    """(species, embedding) for the bootstrap reference photos."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT common_name AS name, embedding FROM species_reference_embeddings WHERE model = ?",
            (model,),
        ).fetchall()
    return [(r["name"], r["embedding"]) for r in rows]


def put_reference_embedding(common_name: str, position: int, model: str,
                            embedding: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO species_reference_embeddings
                (common_name, position, model, embedding, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(common_name, position, model) DO UPDATE SET
                embedding = excluded.embedding, created_at = excluded.created_at
            """,
            (common_name, position, model, embedding, time.time()),
        )


def species_missing_reference_embeddings(model: str, limit: int = 50) -> list[dict]:
    """Cached reference photos with no embedding yet, for the given model.

    Drives the bootstrap: photos are already in ``species_photos`` (fetched so a human can
    compare a detection against known pictures), so this only has to find the gaps.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT p.common_name, p.position, p.file_url, p.thumb_url
            FROM species_photos p
            WHERE p.ok = 1 AND p.file_url IS NOT NULL AND p.file_url != ''
              AND p.common_name != 'bird' COLLATE NOCASE
              AND NOT EXISTS (
                  SELECT 1 FROM species_reference_embeddings r
                  WHERE r.common_name = p.common_name COLLATE NOCASE
                    AND r.position = p.position AND r.model = ?
              )
            ORDER BY p.common_name, p.position
            LIMIT ?
            """,
            (model, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def species_heard_between(start: float, end: float) -> list[str]:
    """Distinct species BirdNET-Go heard in a time window.

    Used as a prior for visual identification: a bird whose song was picked up by the
    microphone two minutes ago is genuinely more likely to be the one in the picture.
    BirdNET only — a Frigate row in the window is another *visual* guess, and feeding
    those back in would just reinforce whatever the model already tends to say.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT common_name FROM detections
            WHERE source = 'birdnet' AND start_time BETWEEN ? AND ?
              AND common_name != '' AND common_name != 'bird'
            """,
            (start, end),
        ).fetchall()
    return [r["common_name"] for r in rows]


def pending_identifications(limit: int = 500) -> list[dict]:
    """Detections stuck in 'pending' or 'confirming', oldest first — requeued at startup.

    A restart mid-flight (add-on update, host reboot) otherwise strands these forever:
    the MQTT ``end`` message that would have triggered them is long gone. A row's
    ``id_status`` says which kind of run it was waiting on.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM detections
            WHERE id_status IN ('pending', 'confirming')
            ORDER BY start_time ASC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# A detection with no species. Deliberately defined by the name rather than by id_status:
# this is exactly the set _named_clause() keeps out of the species registry, so the tab and
# the registry can never disagree about what counts as identified. It also catches rows
# that predate identification, which have no status at all.
_UNIDENTIFIED = f"common_name = '{UNNAMED}' COLLATE NOCASE"


def unidentified_detections(limit: int = 100, before: Optional[float] = None,
                            include_pending: bool = False) -> list[dict]:
    """Detections still waiting for a species, newest first.

    ``include_pending`` covers rows in flight to the identification service. Off by
    default: they resolve within seconds and are reported as a count instead, so the list
    stays a to-do rather than a progress bar.
    """
    params: list = []
    where = f"WHERE {_UNIDENTIFIED}"
    if not include_pending:
        where += " AND (id_status IS NULL OR id_status != 'pending')"
    if before is not None:
        where += " AND start_time < ?"
        params.append(before)
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM detections {where} ORDER BY start_time DESC LIMIT ?", params
        ).fetchall()
    return [dict(r) for r in rows]


def uncertain_rows_in_visits(since: float) -> list[dict]:
    """Review-queue rows that belong to a visit, oldest first — the ones a settled
    sibling might name (identify.inherit_backlog)."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM detections
            WHERE id_status = 'low_confidence' AND visit_id IS NOT NULL AND start_time >= ?
            ORDER BY start_time
            """,
            (since,),
        ).fetchall()
    return [dict(r) for r in rows]


def unidentified_counts() -> dict:
    """Actionable vs in-flight counts for the tab badge and header."""
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT
                SUM(CASE WHEN id_status = 'pending' THEN 1 ELSE 0 END)  AS pending,
                SUM(CASE WHEN id_status IS NULL OR id_status != 'pending'
                         THEN 1 ELSE 0 END)                              AS actionable
            FROM detections WHERE {_UNIDENTIFIED}
            """
        ).fetchone()
    return {
        "pending": int((row["pending"] if row else 0) or 0),
        # What the badge shows: things you can actually do something about.
        "actionable": int((row["actionable"] if row else 0) or 0),
        # Other birds in view that still need a name — a separate queue, because the
        # detection itself already has a species and must not re-enter the main one.
        "subjects": unidentified_subject_count(),
    }


def unidentified_count() -> int:
    return unidentified_counts()["actionable"]


def purge_unidentified(older_than: float) -> list[str]:
    """Drop stale unidentifiable rows. Returns the removed rows' source_refs.

    These are kept deliberately (with Frigate's own classifier off, dropping them would
    mean no record a bird was ever there) but they must not grow without bound on a busy
    feeder. No tombstone is written: this is housekeeping, not a user deletion, and
    tombstoning would stop a future backfill from re-importing a row that a better model
    could since identify.

    Refs, not a count: the caller owns per-detection files (stored crops) that must be
    removed alongside the rows.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, source_ref FROM detections
            WHERE id_status IN ('failed', 'low_confidence') AND start_time < ?
              -- A clip the user pinned as kept-forever must keep its row too: the
              -- video would survive at Frigate while the card vanished here.
              AND retained_at IS NULL
            """,
            (older_than,),
        ).fetchall()
        if not rows:
            return []
        ids = [(r["id"],) for r in rows]
        visit_ids = _visit_ids_of(conn, [r["id"] for r in rows])
        conn.executemany("DELETE FROM detections WHERE id = ?", ids)
        conn.executemany(
            "DELETE FROM identification_embeddings WHERE detection_id = ?", ids
        )
        conn.executemany(
            "DELETE FROM identification_rejections WHERE detection_id = ?", ids
        )
        _delete_subjects(conn, [r["id"] for r in rows])
        _settle_visits(conn, visit_ids)
    return [r["source_ref"] for r in rows]
