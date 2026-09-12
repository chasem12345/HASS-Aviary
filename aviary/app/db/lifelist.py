"""Life list on iNaturalist: one observation per species."""

from __future__ import annotations
import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from ._conn import _connect

__all__ = [
    "inat_get",
    "inat_set",
    "inat_forget",
    "inat_all",
]


# ------------------------------------------------------------------ iNaturalist

def inat_get(common_name: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM species_inat WHERE common_name = ? COLLATE NOCASE", (common_name,),
        ).fetchone()
    return dict(row) if row else None


def inat_set(row: dict) -> None:
    """Upsert the species' iNaturalist state. Missing keys are stored as NULL — a
    'failed' row deliberately carries no observation."""
    data = {k: row.get(k) for k in (
        "common_name", "observation_id", "observation_url", "taxon_id", "detection_id",
        "subject_idx", "observed_at", "media", "status", "error", "posted_at")}
    data["subject_idx"] = int(data.get("subject_idx") or 0)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO species_inat (common_name, observation_id, observation_url, taxon_id,
                detection_id, subject_idx, observed_at, media, status, error, posted_at)
            VALUES (:common_name, :observation_id, :observation_url, :taxon_id, :detection_id,
                :subject_idx, :observed_at, :media, :status, :error, :posted_at)
            ON CONFLICT(common_name) DO UPDATE SET
                observation_id = excluded.observation_id, observation_url = excluded.observation_url,
                taxon_id = excluded.taxon_id, detection_id = excluded.detection_id,
                subject_idx = excluded.subject_idx, observed_at = excluded.observed_at,
                media = excluded.media, status = excluded.status, error = excluded.error,
                posted_at = excluded.posted_at
            """,
            data,
        )


def inat_forget(common_name: str) -> Optional[dict]:
    """Drop the local link (never the observation). Returns the row that was there."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM species_inat WHERE common_name = ? COLLATE NOCASE", (common_name,),
        ).fetchone()
        conn.execute("DELETE FROM species_inat WHERE common_name = ? COLLATE NOCASE", (common_name,))
    return dict(row) if row else None


def inat_all() -> dict[str, dict]:
    """Every POSTED species, keyed by lowercased common name (index markers, counts)."""
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM species_inat WHERE status = 'posted'").fetchall()
    return {r["common_name"].lower(): dict(r) for r in rows}
